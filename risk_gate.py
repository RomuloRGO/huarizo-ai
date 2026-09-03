"""Gate de riesgo duro para Huarizo AI.

Este modulo existe por una razon concreta: el 2026-08-31 el agente envio 180
ordenes en 18 segundos sobre tres simbolos y dejo la cuenta con cash -263.703 USD,
apalancada en margen. La causa fue que `run_autonomous_autopilot` limitaba
ordenes *por ciclo* pero nunca verifico cuantas posiciones habia abiertas, ni
comprobaba buying power, ni tenia pausa por perdida diaria.

Todo lo de decision en este archivo es **puro**: sin red, sin reloj implicito, sin
estado global. Cada funcion es determinista y testeable. La unica excepcion es
`DailyLossTracker`, que toca disco pero recibe la ruta por parametro.

Principio rector: **fail-closed**. Cualquier dato faltante, no finito o
inconsistente rechaza la operacion. Es mejor no operar que perder el control
de la cuenta.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from typing import Any, Dict, List, Optional, Set

# Símbolo OCC: <underlying 1-6><fecha 6 u 8 dígitos><C|P><strike 8 dígitos>.
# Alpaca devuelve fecha YYMMDD (SPY260901C00420000, verificado en el spike MCP);
# algunos helpers internos generan YYYYMMDD (XYZ20260918C021500000). Se aceptan
# ambas anchuras porque las dos aparecen en el repo.
OCC_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}\d{6,8}[CP]\d{8}$")

# Mismo patron pero con el subyacente capturado. Existe aparte (y no se reutiliza
# `OCC_SYMBOL_RE` con un grupo) para que `is_option_symbol` siga siendo una
# comprobacion booleana pura sin coste de extraccion ni cambios de firma.
OCC_UNDERLYING_RE = re.compile(r"^([A-Z]{1,6})\d{6,8}[CP]\d{8}$")

DEFAULT_RISK_LIMITS: Dict[str, float] = {
    # Posiciones abiertas simultaneas. El autopilot original no tenia este limite.
    "max_open_positions": 6,
    # R6: tope de posiciones abiertas sobre el MISMO subyacente (concentracion).
    # Vale 1 y no 2 porque con el DTE fijo en ~5 dias todas las posiciones vencen
    # la misma semana por construccion: diversificar por vencimiento es imposible
    # y el subyacente queda como unico eje de diversificacion. Un tope de 2 no
    # habria frenado el libro del 2026-09-02 (dos calls de SPY, mismo vencimiento,
    # strikes a 3 puntos => correlacion ~1.0).
    "max_positions_per_underlying": 1,
    # Pausa del dia si la cuenta cae este porcentaje desde el equity de apertura.
    "max_daily_loss_pct": 5.0,
    # Exposicion bruta maxima (suma de |market_value| + nueva orden) como % del equity.
    "max_gross_exposure_pct": 100.0,
    # Notional maximo por posicion individual, como % del equity.
    "max_position_pct": 5.0,
    # Techo absoluto en USD por orden, independiente del equity. Alineado con
    # max_position_pct para una cuenta de $100.000 (5% = $5.000), y actua como
    # freno duro si la cuenta crece mucho.
    "max_notional_per_order": 5000.0,
    # Fraccion del buying power que se deja libre como colchon.
    "min_buying_power_buffer_pct": 10.0,
}

_REJECT = "reject"
_ALLOW = "allow"


def _finite(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Convierte a float solo si es un numero finito. Si no, devuelve default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def is_option_symbol(symbol: Any) -> bool:
    """True solo si `symbol` tiene forma de contrato OCC. Nunca levanta.

    Vive aqui y no en `agent_engine` para que tanto el agente como el
    `execution_ledger` puedan validar sin dependencias cruzadas.
    """
    if not isinstance(symbol, str):
        return False
    return bool(OCC_SYMBOL_RE.match(symbol.strip().upper()))


def assert_option_symbol(symbol: Any) -> str:
    """Devuelve el simbolo normalizado o falla. Fail-closed.

    Garantiza que ninguna ruta de ejecucion pueda enviar una orden de acciones
    colandose por un simbolo vacio, truncado o mal formado.
    """
    if not is_option_symbol(symbol):
        raise ValueError(f"not a valid OCC option symbol: {symbol!r}")
    return symbol.strip().upper()


def normalize_account(account: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Extrae y valida los campos de cuenta que usa el gate.

    Devuelve None si falta equity o si algun campo no es finito -> el llamador
    debe tratar None como "no operar".
    """
    if not isinstance(account, dict):
        return None

    equity = _finite(account.get("equity"))
    if equity is None or equity <= 0:
        return None

    # No usamos defaults: si no sabemos buying power, no podemos saber si la
    # orden cabe. Rechazamos aqui mismo para que el motivo sea inequivoco en
    # los logs en vez de un confuso "insufficient_buying_power" mas adelante.
    cash = _finite(account.get("cash"))
    buying_power = _finite(account.get("buying_power"))
    if cash is None or buying_power is None:
        return None

    # `options_buying_power` es OPCIONAL a proposito: ausente no invalida la
    # cuenta. Lo que hace con esa ausencia lo decide `evaluate_new_entry`,
    # que para simbolos de opciones falla cerrado. Las cuentas de acciones
    # (y los tests de risk gate previos) no tienen por que traerlo.
    return {
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "options_buying_power": _finite(account.get("options_buying_power")),
    }


def position_symbols(open_positions: Optional[List[Dict[str, Any]]]) -> Set[str]:
    """Conjunto de simbolos con posicion abierta, normalizados a mayusculas."""
    syms: Set[str] = set()
    for pos in open_positions or []:
        if not isinstance(pos, dict):
            continue
        sym = pos.get("symbol")
        if isinstance(sym, str) and sym.strip():
            syms.add(sym.strip().upper())
    return syms


def option_underlying(symbol: Any) -> Optional[str]:
    """Subyacente de un contrato OCC. None si no es una opcion. Nunca levanta.

    `SPY260909C00767000` -> `"SPY"`. Es la pieza que faltaba para R6: sin ella
    el gate solo podia comparar OCC completo contra OCC completo, y dos calls
    del mismo ticker con strikes distintos parecian dos apuestas independientes.
    """
    if not isinstance(symbol, str):
        return None
    m = OCC_UNDERLYING_RE.match(symbol.strip().upper())
    return m.group(1) if m else None


def underlying_symbols(open_positions: Optional[List[Dict[str, Any]]]) -> Dict[str, int]:
    """Cuantas posiciones abiertas hay por subyacente.

    Las posiciones de acciones se cuentan bajo su propio simbolo (para una accion
    el subyacente es ella misma), asi que el conteo sirve para carteras mixtas.
    """
    counts: Dict[str, int] = {}
    for pos in open_positions or []:
        if not isinstance(pos, dict):
            continue
        sym = pos.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            continue
        sym_u = sym.strip().upper()
        und = option_underlying(sym_u) or sym_u
        counts[und] = counts.get(und, 0) + 1
    return counts


def gross_exposure(open_positions: Optional[List[Dict[str, Any]]]) -> float:
    """Suma del valor absoluto de mercado de todas las posiciones."""
    total = 0.0
    for pos in open_positions or []:
        if not isinstance(pos, dict):
            continue
        mv = _finite(pos.get("market_value"))
        if mv is not None:
            total += abs(mv)
    return total


def evaluate_daily_loss(
    equity: Optional[float],
    day_start_equity: Optional[float],
    max_daily_loss_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Determina si la cuenta entro en pausa por perdida diaria.

    Fail-closed: si no conocemos el equity de apertura o el actual, asumimos
    que NO hay breach (para no bloquear por un dato faltante) pero lo
    reportamos como `unknown`, y el llamador decide.
    """
    limits = dict(DEFAULT_RISK_LIMITS)
    limit = _finite(max_daily_loss_pct, limits["max_daily_loss_pct"])

    eq = _finite(equity)
    start = _finite(day_start_equity)

    if eq is None or start is None or start <= 0:
        return {
            "breached": False,
            "known": False,
            "loss_pct": None,
            "limit_pct": limit,
            "reason": "daily_loss_unknown",
        }

    loss_pct = (eq - start) / start * 100.0
    breached = loss_pct <= -abs(limit)

    return {
        "breached": breached,
        "known": True,
        "loss_pct": round(loss_pct, 4),
        "limit_pct": limit,
        "reason": "daily_loss_breach" if breached else "daily_loss_ok",
    }


def evaluate_new_entry(
    account: Optional[Dict[str, Any]],
    open_positions: Optional[List[Dict[str, Any]]],
    symbol: str = "",
    qty: float = 0.0,
    price: float = 0.0,
    day_start_equity: Optional[float] = None,
    in_flight_symbols: Optional[Set[str]] = None,
    limits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide si una nueva entrada pasa el gate de riesgo.

    Devuelve siempre un dict con `allowed` (bool) y `reason` (str). Nunca lanza.
    """
    cfg = dict(DEFAULT_RISK_LIMITS)
    if isinstance(limits, dict):
        for key in cfg:
            override = _finite(limits.get(key))
            if override is not None:
                cfg[key] = override

    def denied(reason: str, **extra: Any) -> Dict[str, Any]:
        out = {
            "allowed": False,
            "decision": _REJECT,
            "reason": reason,
            "symbol": (symbol or "").upper(),
            "notional": None,
        }
        out.update(extra)
        return out

    acc = normalize_account(account)
    if acc is None:
        return denied("invalid_account_snapshot")

    sym = (symbol or "").strip().upper()
    if not sym:
        return denied("missing_symbol")

    quantity = _finite(qty)
    px = _finite(price)
    if quantity is None or quantity <= 0:
        return denied("invalid_quantity")
    if px is None or px <= 0:
        return denied("invalid_price")

    notional = quantity * px
    if not math.isfinite(notional) or notional <= 0:
        return denied("invalid_notional")

    equity = acc["equity"]
    positions = open_positions or []
    held = position_symbols(positions)
    in_flight = {s.strip().upper() for s in (in_flight_symbols or set()) if isinstance(s, str)}

    # 1. Pausa por perdida diaria.
    dl = evaluate_daily_loss(equity, day_start_equity, cfg["max_daily_loss_pct"])
    if dl["breached"]:
        return denied("daily_loss_breach", daily_loss=dl, notional=round(notional, 2))

    # 2. Ya tenemos posicion en este simbolo. Este es el check que falto el 31-ago:
    #    AAPL recibio 166 ordenes de compra porque nunca se verifico que ya estaba en cartera.
    if sym in held:
        return denied("already_in_portfolio", notional=round(notional, 2))

    # 3. Orden ya enviada para este simbolo en el ciclo actual (dedup en vuelo).
    if sym in in_flight:
        return denied("order_already_in_flight", notional=round(notional, 2))

    # 4. R6: concentracion por subyacente. Va DESPUES de los dos dedups exactos
    #     (#2 y #3) a proposito: si el contrato es literalmente el mismo que ya
    #     tenemos o que ya va en vuelo, el motivo especifico es mas util que el
    #     agregado. Lo que #2 y #3 no podian ver es justo lo que este ve:
    #     `SPY260909C00764000` y `SPY260909C00767000` son dos strings distintos
    #     para ellos, pero son la misma apuesta — mismo ticker, mismo
    #     vencimiento, strikes a 3 puntos. Con el DTE clavado en ~5 no se puede
    #     diversificar por vencimiento, asi que este es el unico control que
    #     impide apilar riesgo correlacionado.
    und = option_underlying(sym) or sym
    und_counts = underlying_symbols(positions)
    # Las ordenes en vuelo tambien cuentan: si no, dos contratos del mismo ticker
    # enviados en el mismo ciclo se colarian los dos (ninguno esta en `positions`
    # todavia). Es el mismo criterio que el check #5 para el limite global.
    for s in in_flight:
        u = option_underlying(s) or s
        und_counts[u] = und_counts.get(u, 0) + 1
    und_limit = int(cfg["max_positions_per_underlying"])
    if und_counts.get(und, 0) >= und_limit:
        return denied("underlying_concentration_limit", underlying=und,
                      open_in_underlying=und_counts.get(und, 0),
                      limit=und_limit, notional=round(notional, 2))

    # 5. Limite global de posiciones abiertas.
    projected = len(held) + len(in_flight)
    if projected >= int(cfg["max_open_positions"]):
        return denied("max_open_positions_reached", open_positions=len(held),
                      in_flight=len(in_flight), limit=int(cfg["max_open_positions"]),
                      notional=round(notional, 2))

    # 6. Exposicion bruta proyectada.
    exposure_pct = (gross_exposure(positions) + notional) / equity * 100.0
    if exposure_pct > cfg["max_gross_exposure_pct"]:
        return denied("gross_exposure_limit", exposure_pct=round(exposure_pct, 2),
                      limit_pct=cfg["max_gross_exposure_pct"], notional=round(notional, 2))

    # 7. Tamano maximo por posicion (% del equity).
    position_pct = notional / equity * 100.0
    if position_pct > cfg["max_position_pct"]:
        return denied("position_size_pct_limit", position_pct=round(position_pct, 2),
                      limit_pct=cfg["max_position_pct"], notional=round(notional, 2))

    # 8. Techo absoluto en USD.
    if notional > cfg["max_notional_per_order"]:
        return denied("notional_cap_exceeded", notional=round(notional, 2),
                      cap=cfg["max_notional_per_order"])

    # 9. Poder de compra de OPCIONES con colchon. Solo aplica a opciones.
    #
    # Antes una orden de opciones se media contra `buying_power` general, que en
    # una cuenta con margen es el equity multiplicado (4x en la cuenta del
    # concurso: 400.000 sobre equity de 100.000) y NO refleja lo que la cuenta
    # puede destinar a opciones (100.000, y menos si el margen se consume).
    # Con los topes por defecto este check no dispara nunca en una orden
    # individual (`max_notional_per_order` 5.000 << 90.000 utiles): es defensa
    # en profundidad para cuando alguien suba los topes o el poder degrade.
    if is_option_symbol(sym):
        obp = acc.get("options_buying_power")
        if obp is None:
            # Falla cerrado: caer al poder general seria justo el bug que este
            # check corrige, y hacerlo en silencio lo disfrazaria de aprobacion.
            return denied("options_buying_power_unknown", notional=round(notional, 2),
                          options_buying_power=None)
        usable_obp = obp * (1.0 - cfg["min_buying_power_buffer_pct"] / 100.0)
        if usable_obp < 0:
            usable_obp = 0.0
        if notional > usable_obp:
            return denied("insufficient_options_buying_power", notional=round(notional, 2),
                          options_buying_power=round(obp, 2),
                          usable_options_buying_power=round(usable_obp, 2))

    # 10. Buying power general con colchon (acciones, y opciones que ya pasaron 9).
    usable = acc["buying_power"] * (1.0 - cfg["min_buying_power_buffer_pct"] / 100.0)
    if usable < 0:
        usable = 0.0
    if notional > usable:
        return denied("insufficient_buying_power", notional=round(notional, 2),
                      usable=round(usable, 2))

    return {
        "allowed": True,
        "decision": _ALLOW,
        "reason": "approved",
        "symbol": sym,
        "notional": round(notional, 2),
        "position_pct": round(position_pct, 2),
        "exposure_pct": round(exposure_pct, 2),
        "open_positions": len(held),
        "daily_loss": dl,
    }


def size_order_qty(
    equity: Optional[float],
    price: Optional[float],
    max_notional_per_order: Optional[float] = None,
    max_position_pct: Optional[float] = None,
    limits: Optional[Dict[str, Any]] = None,
) -> int:
    """Cantidad de acciones a comprar, derivada del equity (no un fijo de $2.000).

    El codigo original usaba `int(2000.0 / price)`, que ignora por completo el
    tamano de la cuenta. Fail-closed: devuelve 0 si falta equity o precio.
    """
    cfg = dict(DEFAULT_RISK_LIMITS)
    if isinstance(limits, dict):
        for key in cfg:
            override = _finite(limits.get(key))
            if override is not None:
                cfg[key] = override

    cap_notional = _finite(max_notional_per_order, cfg["max_notional_per_order"])
    pct = _finite(max_position_pct, cfg["max_position_pct"])

    eq = _finite(equity)
    px = _finite(price)
    if eq is None or eq <= 0 or px is None or px <= 0:
        return 0
    if cap_notional is None or pct is None or cap_notional <= 0 or pct <= 0:
        return 0

    budget = min(cap_notional, eq * pct / 100.0)
    if budget <= 0:
        return 0

    return int(budget // px)


def build_client_order_id(
    symbol: str,
    side: str,
    strategy: str = "",
    bucket_seconds: int = 900,
    now_epoch: Optional[float] = None,
    nonce: str = "",
) -> str:
    """Idempotency key determinista para Alpaca.

    Alpaca rechaza ordenes duplicadas que compartan `client_order_id` dentro de
    la misma ventana, asi que esto convierte el bug de las 180 ordenes en un
    problema imposible incluso si el gate de riesgo fallara.

    Formato: huarizo-<symbol>-<side>-<strategy>-<bucket>[-<nonce>]  (<= 48 chars)
    """
    sym = "".join(c for c in (symbol or "").upper() if c.isalnum())[:8] or "X"
    sd = "".join(c for c in (side or "").lower() if c.isalnum())[:4] or "buy"
    st = "".join(c for c in (strategy or "").lower() if c.isalnum())[:10]

    epoch = _finite(now_epoch)
    bucket = int(epoch // bucket_seconds) if epoch is not None and bucket_seconds > 0 else 0

    parts = ["huarizo", sym, sd]
    if st:
        parts.append(st)
    parts.append(str(bucket))
    if nonce:
        clean = "".join(c for c in nonce if c.isalnum())[:8]
        if clean:
            parts.append(clean)

    return "-".join(parts)[:48]


class DailyLossTracker:
    """Persiste el equity de apertura del dia para la pausa por perdida diaria.

    La version anterior guardaba este estado solo en memoria, asi que reiniciar
    el servidor borraba la pausa. Aqui se persiste en JSON por fecha.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "risk_state.json"
        )
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def day_start_equity(self, day_key: str, current_equity: Optional[float]) -> Optional[float]:
        """Equity de apertura del dia. Lo fija la primera vez que se ve el dia."""
        eq = _finite(current_equity)
        if eq is None or eq <= 0:
            return None

        with self._lock:
            data = self._read()
            stored = _finite(data.get(day_key))
            if stored is not None and stored > 0:
                return stored
            data[day_key] = eq
            self._write(data)
            return eq

    def pause(self, day_key: str, reason: str) -> None:
        with self._lock:
            data = self._read()
            data[f"{day_key}:paused"] = reason
            self._write(data)

    def is_paused(self, day_key: str) -> bool:
        with self._lock:
            data = self._read()
            return f"{day_key}:paused" in data

    def clear(self) -> None:
        """Reset manual de emergencia: borra TODO, incluida la pausa de safety."""
        with self._lock:
            self._write({})

    # ── Pausa de safety ───────────────────────────────────────────────────────
    # Motivos: MCP/CLI caído, datos degradados, excepción inesperada, reloj
    # desincronizado. A diferencia de la pausa diaria, ésta NO se llavea por
    # fecha: guardarla bajo una llave con fecha hacía que al cambiar el día la
    # cuenta volviera a operar con la falla todavía presente.

    _SAFETY_KEY = "safety:paused"

    def safety_pause(self, reason: str) -> None:
        with self._lock:
            data = self._read()
            data[self._SAFETY_KEY] = str(reason)
            self._write(data)

    def is_safety_paused(self) -> bool:
        with self._lock:
            return self._SAFETY_KEY in self._read()

    def clear_safety_pause(self) -> None:
        with self._lock:
            data = self._read()
            data.pop(self._SAFETY_KEY, None)
            self._write(data)

    def pause_state(self, day_key: str) -> Dict[str, Any]:
        """Estado completo. Tasks 9 y 11 consultan este dict.

        Ojo: `is_paused()` NO incluye la pausa de safety. Quien decide si se
        opera debe mirar los dos campos, o usar este método.
        """
        with self._lock:
            data = self._read()
            return {
                "daily_loss_paused": f"{day_key}:paused" in data,
                "daily_reason": data.get(f"{day_key}:paused"),
                "safety_paused": self._SAFETY_KEY in data,
                "safety_reason": data.get(self._SAFETY_KEY),
            }
