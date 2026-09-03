"""Validación de la cuenta de concurso. Pura: sin red, sin MCP, sin Flask.

El concurso de Alpaca exige una cuenta de paper NUEVA que arranca en 100.000
USD. Este módulo es la cerradura que impide armar el bot contra otra cuenta
(la anterior estaba contaminada: equity 176k, cash -155k, posiciones heredadas).

## Dos validaciones, no una

La tentación —y el diseño original del plan— es una sola función que rechaza
si `abs(equity - 100000) > 1%`. ESO ES UNA TRAMPA: el equity se mueve en cuanto
se opera. Tras una sesión la cuenta está legítimamente en 101.500 o 98.000,
fuera de la banda, y el bot ya no podría volver a encenderse. Fallaría justo
cuando más se necesita: después de que un drawdown dispare una pausa, la
recuperación quedaría bloqueada para siempre.

Por eso hay dos momentos distintos:

* `validate_contest_account()` — ONBOARDING. Estricto y UNA sola vez. Prueba
  que la cuenta es nueva: equity en banda, cash >= 0, sin posiciones, opciones
  aprobadas, cuenta activa y desbloqueada.

* `validate_armed_account()` — CONTINUO. Corre en cada habilitación contra el
  registro que dejó el onboarding. Exige IDENTIDAD y SALUD; el equity puede
  derivar libremente (solo se exige > 0). Nunca re-afirma "equity ≈ 100k".

## Recuperación ante pérdida del registro

Si el archivo de onboarding se pierde o se corrompe, se degrada a "no armado"
y volvería a correr el onboarding estricto, que rechazaría un equity ya
derivado. Para no dejar al bot muerto de forma permanente por un archivo
perdido, el umbral de onboarding es configurable con
`HUARIZO_CONTEST_BASELINE_EQUITY` (ver `expected_start_equity()` en app.py):
re-onboarding es una acción deliberada y auditada del operador, no una
relajación automática del chequeo.
"""
import json
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

# Equity de arranque que exige el concurso.
EXPECTED_START_EQUITY = 100000.0
# Banda de frescura: +/-1% sobre 100.000 => [99.000, 101.000].
EQUITY_TOLERANCE_PCT = 0.01
# Nivel 2 es el mínimo para comprar calls/puts al descubierto (1 = covered).
# El concurso exige operar opciones: sin esto el bot nace muerto.
MIN_OPTIONS_APPROVED_LEVEL = 2
# Banderas reales de bloqueo del payload de Alpaca.
BLOCK_FLAGS = ("trading_blocked", "account_blocked", "trade_suspended_by_user")
# Tolerancia para considerar que NO hay posiciones (ruido de float).
POSITION_TOLERANCE = 1e-6

DEFAULT_ONBOARDING_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "account_onboarding.json"
)

_lock = threading.Lock()


# ── Motivos (contrato estable: los consume la UI y los tests) ────────────────
#
# ok                          válido
# invalid_input               no es un dict utilizable
# missing_equity              falta `equity`
# invalid_equity              `equity` no es numérico/finito
# insolvent                   equity <= 0
# account_blocked             usable=False o bandera de bloqueo
# account_not_active          status != ACTIVE
# status_unknown              no viene `status`: no se puede afirmar salud
# equity_out_of_range         (solo onboarding) fuera de +/-1% de la base
# missing_cash / negative_cash
# positions_unknown           no viene ningún market value: no se afirma frescura
# existing_positions          (solo onboarding) ya tiene posiciones abiertas
# options_level_missing       falta el nivel de opciones
# options_level_insufficient  nivel < 2
# not_onboarded               (solo armado) no hay registro previo
# identity_unknown            (solo armado) el snapshot no trae identificador
# account_mismatch            (solo armado) es OTRA cuenta


def _finite(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _identity_values(source: Any, keys: Tuple[str, ...]) -> Set[str]:
    if not isinstance(source, dict):
        return set()
    found = set()
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
    return found


def _options_level(snapshot: Dict[str, Any]) -> Optional[int]:
    raw = snapshot.get("options_approved_level",
                       snapshot.get("options_trading_level"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _has_options_level(snapshot: Dict[str, Any]) -> bool:
    return ("options_approved_level" in snapshot
            or "options_trading_level" in snapshot)


def _account_health(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Bloqueo y status. Devuelve `{}` si la cuenta está sana."""
    if snapshot.get("usable") is False:
        return {"valid": False, "reason": "account_blocked",
                "detail": {"blocked_reason": snapshot.get("blocked_reason")}}
    for flag in BLOCK_FLAGS:
        if snapshot.get(flag):
            return {"valid": False, "reason": "account_blocked",
                    "detail": {"flag": flag}}
    if "status" not in snapshot:
        return {"valid": False, "reason": "status_unknown", "detail": {}}
    status = snapshot.get("status")
    if not isinstance(status, str) or status.strip().upper() != "ACTIVE":
        return {"valid": False, "reason": "account_not_active",
                "detail": {"status": status}}
    return {}


def _options_verdict(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    level = _options_level(snapshot)
    if level is None:
        return {"valid": False, "reason": "options_level_missing",
                "detail": {"min_required": MIN_OPTIONS_APPROVED_LEVEL}}
    if level < MIN_OPTIONS_APPROVED_LEVEL:
        return {"valid": False, "reason": "options_level_insufficient",
                "detail": {"level": level,
                           "min_required": MIN_OPTIONS_APPROVED_LEVEL}}
    return {}


def _verdict(reason: str = "ok", **detail: Any) -> Dict[str, Any]:
    return {"valid": reason == "ok", "reason": reason, "detail": detail}


# ── 1. ONBOARDING (estricto, una vez) ────────────────────────────────────────

def validate_contest_account(snapshot: Any,
                             expected_start_equity: float = EXPECTED_START_EQUITY
                             ) -> Dict[str, Any]:
    """¿Es esta una cuenta NUEVA y dedicada al concurso?

    Estricto a propósito: equity en banda, cash >= 0, CERO posiciones, opciones
    aprobadas, cuenta activa. Solo se corre una vez; para re-habilitar existe
    `validate_armed_account`.
    """
    if not isinstance(snapshot, dict):
        return _verdict("invalid_input")

    base = _finite(expected_start_equity)
    if base is None or base <= 0:
        base = EXPECTED_START_EQUITY

    equity = _finite(snapshot.get("equity"))
    if equity is None:
        return _verdict("missing_equity" if "equity" not in snapshot
                        else "invalid_equity")
    if equity <= 0:
        return _verdict("insolvent", equity=equity)

    health = _account_health(snapshot)
    if health:
        return health

    if abs(equity - base) > base * EQUITY_TOLERANCE_PCT:
        return _verdict("equity_out_of_range", equity=equity,
                        expected_start_equity=base,
                        tolerance_pct=EQUITY_TOLERANCE_PCT)

    cash = _finite(snapshot.get("cash"))
    if cash is None:
        return _verdict("missing_cash")
    if cash < 0:
        return _verdict("negative_cash", cash=cash)

    long_mv = _finite(snapshot.get("long_market_value"))
    short_mv = _finite(snapshot.get("short_market_value"))
    if long_mv is None and short_mv is None:
        # Sin ningún market value no se puede afirmar que la cuenta esté vacía,
        # y "vacía" es la señal de frescura más fuerte que tenemos.
        return _verdict("positions_unknown")
    if any(abs(v) > POSITION_TOLERANCE
           for v in (long_mv, short_mv) if v is not None):
        return _verdict("existing_positions", long_market_value=long_mv,
                        short_market_value=short_mv)

    options = _options_verdict(snapshot)
    if options:
        return options

    return _verdict("ok", equity=equity, cash=cash)


# ── 2. CONTINUO (cada habilitación, ya armado) ───────────────────────────────

def validate_armed_account(snapshot: Any,
                           onboarding: Any) -> Dict[str, Any]:
    """¿Sigue siendo la cuenta que armamos y sigue operable?

    NO re-afirma equity ≈ 100k: tras una sesión vale 98.500 o 101.500 y ambas
    son legítimas. Exige identidad + salud + opciones; el equity solo debe ser
    > 0. Es el chequeo que evita el "nunca se puede re-encender" del plan.
    """
    if not isinstance(snapshot, dict):
        return _verdict("invalid_input")

    if not isinstance(onboarding, dict) or not onboarding.get("account_id"):
        return _verdict("not_onboarded")

    snapshot_ids = _identity_values(
        snapshot, ("account_id", "id", "account_number"))
    if not snapshot_ids:
        return _verdict("identity_unknown")
    saved_ids = _identity_values(onboarding, ("account_id", "account_number"))
    if not (snapshot_ids & saved_ids):
        return _verdict("account_mismatch",
                        onboarded=sorted(saved_ids),
                        found=sorted(snapshot_ids))

    equity = _finite(snapshot.get("equity"))
    if equity is None:
        return _verdict("missing_equity" if "equity" not in snapshot
                        else "invalid_equity")
    if equity <= 0:
        return _verdict("insolvent", equity=equity)

    health = _account_health(snapshot)
    if health:
        return health

    options = _options_verdict(snapshot)
    if options:
        return options

    return _verdict("ok", equity=equity)


# ── 3. Persistencia del onboarding ───────────────────────────────────────────
#
# Misma convención que `risk_gate.DailyLossTracker`: JSON en disco, lock de
# hilo, escritura atómica y degradación silenciosa si el archivo no existe o
# está corrupto. Un archivo ilegible significa "no armado", nunca una excepción.

def build_onboarding_record(snapshot: Dict[str, Any],
                            expected_start_equity: float = EXPECTED_START_EQUITY
                            ) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        snapshot = {}
    equity = _finite(snapshot.get("equity"))
    account_id = snapshot.get("account_id") or snapshot.get("id")
    return {
        "account_id": str(account_id).strip() if account_id else None,
        "account_number": (str(snapshot["account_number"]).strip()
                           if snapshot.get("account_number") else None),
        "baseline_equity": equity,
        "expected_start_equity": _finite(expected_start_equity) or EXPECTED_START_EQUITY,
        "armed_at": datetime.now(timezone.utc).isoformat(),
    }


def load_onboarding(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Registro de onboarding, o None si no hay / no se puede leer."""
    path = path or DEFAULT_ONBOARDING_PATH
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
    if not isinstance(data, dict) or not data.get("account_id"):
        return None
    return data


def save_onboarding(path: Optional[str], record: Dict[str, Any]) -> bool:
    """Escribe el registro de forma atómica. False si no se pudo escribir."""
    if not path or not isinstance(record, dict):
        return False
    tmp = f"{path}.tmp"
    with _lock:
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            return False
