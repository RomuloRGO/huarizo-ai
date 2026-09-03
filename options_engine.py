"""Motor de decisiones de opciones para Huarizo AI (funciones puras).

Selección de contratos 1-leg direccional (long call / long put), position
sizing basado en riesgo y planes de salida TP/SL/time-stop.
Fail-closed: ante datos insuficientes o inválidos retorna None / qty 0.
"""

import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

DEFAULT_OPTIONS_CONFIG: Dict[str, Any] = {
    "dte_min": 7,
    "dte_max": 45,
    "target_delta": 0.40,
    "max_spread_pct": 10.0,
    "min_open_interest": 100,
    # Sizing is risk-based: the budget is the loss taken if the stop fires,
    # NOT the premium paid. A long option's premium is not immobilized
    # capital the way a stock position is -- the stop caps the real loss.
    "risk_per_trade_pct": 1.5,
    # Absolute ceiling for the single-contract floor rule: if one contract's
    # loss-at-stop still fits under this, trade 1 instead of rejecting.
    "max_risk_per_trade_pct": 3.0,
    "max_contracts": 10,
    "take_profit_pct": 50.0,
    "stop_loss_pct": 30.0,
    "min_dte_exit": 5,
    # ── Reglas de trader de opciones (2026-09-02) ────────────────────────────
    # Antes de estas claves el agente era un selector de indicadores de la
    # ACCION que luego compraba una call/put: la cadena le daba IV, theta y
    # todos los vencimientos y no usaba ninguno de los tres. Ahora decide con
    # ellos. Medido en vivo sobre SPY (3978 contratos): iv y theta vienen
    # poblados en 3247 de ellos (82%), asi que las reglas son aplicables.
    #
    # R1 · Ventana DTE con PREFERENCIA, no solo filtro. Sin preferencia el
    # selector ordena por |delta - target| y cae siempre en el vencimiento mas
    # cercano que cumple: en la practica DTE 7, el peor sitio para una opcion
    # larga (theta maxima, cero tiempo para que la tesis madure).
    "dte_sweet_min": 21,
    "dte_acceptable_min": 14,
    # R2 · No comprar prima cara: IV del contrato vs volatilidad realizada del
    # subyacente. Se salta el filtro si no hay RV disponible (no hay datos).
    "max_iv_rv_ratio": 1.25,
    # R3 · Coste diario de mantener la posicion como % de la prima pagada.
    # A 7 DTE una ATM quema ~7%/dia; a 21 DTE ~2.4%/dia; a 30 DTE ~1.7%/dia.
    # 3.0%/dia deja fuera la basura corta y admite el tramo >= 21 DTE.
    "max_theta_pct": 3.0,
    # R5 · Estructura de plazos. Si el vencimiento cercano esta mas caro que el
    # lejano, el mercado esta precioando un evento (earnings, macro). Entrar
    # ahi con una long es comprar la vol inflada y comer el crush despues.
    "max_iv_term_ratio": 1.15,
}

_CONFIG_KEYS = frozenset(DEFAULT_OPTIONS_CONFIG.keys())


def merge_config(overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Fusiona overrides sobre defaults con whitelist estricta de claves."""
    cfg = dict(DEFAULT_OPTIONS_CONFIG)
    if overrides:
        for k, v in overrides.items():
            if k in _CONFIG_KEYS:
                cfg[k] = v
    return cfg


def _parse_expiry(expiry: str) -> Optional[date]:
    try:
        return datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid * 100.0


def _safe_int(val) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def _passes_liquidity(c: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    if _spread_pct(float(c["bid"]), float(c["ask"])) > cfg["max_spread_pct"]:
        return False
    vol = _safe_int(c.get("volume"))
    oi = _safe_int(c.get("open_interest"))
    # El feed indicativo de Alpaca puede reportar volumen 0/stale en contratos líquidos: OI solo califica.
    return vol > 0 or oi >= cfg["min_open_interest"]


def _finite_float(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _median(values) -> Optional[float]:
    vals = []
    for v in values:
        f = _finite_float(v)
        if f is not None:
            vals.append(f)
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def realized_volatility(closes, window: int = 30) -> Optional[float]:
    """Volatilidad realizada anualizada a partir de retornos logaritmicos.

    Es la referencia contra la que se juzga el IV del contrato (regla R2):
    comprar prima cuando IV >> RV realizada es pagar de mas por la misma
    cantidad de movimiento esperado. Se usa una ventana de 30 sesiones para
    que sea comparable a un contrato de ~30 DTE, que es el tramo que R1
    prefiere. Retorna None si no hay datos suficientes (minimo 10 retornos).
    """
    try:
        series = [_finite_float(c) for c in closes]
    except TypeError:
        return None
    series = [c for c in series if c is not None and c > 0]
    if window and window > 0:
        series = series[-(int(window) + 1):]
    if len(series) < 11:
        return None
    rets = [math.log(cur / prev) for prev, cur in zip(series, series[1:])]
    if len(rets) < 10:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if not math.isfinite(var) or var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(252.0)


def _theta_pct(c: Dict[str, Any]) -> Optional[float]:
    """Decaimiento diario como % de la prima. Alpaca reporta theta por accion/dia."""
    theta = _finite_float(c.get("theta"))
    if theta is None:
        return None
    bid = _finite_float(c.get("bid"))
    ask = _finite_float(c.get("ask"))
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return abs(theta) / mid * 100.0


def _dte_tier(dte: int, cfg: Dict[str, Any]) -> int:
    """R1: preferencia DENTRO de la ventana. 0 dulce · 1 aceptable · 2 ultimo recurso."""
    if dte >= int(cfg["dte_sweet_min"]):
        return 0
    if dte >= int(cfg["dte_acceptable_min"]):
        return 1
    return 2


def iv_term_structure(candidates: List[Dict[str, Any]],
                      cfg: Dict[str, Any]) -> Dict[str, Any]:
    """R5: IV del tramo corto vs el tramo largo de la cadena.

    Una estructura INVERTIDA (el vencimiento cercano mas caro que el lejano) es
    la forma que tiene el mercado de precioar un evento fechado. Una opcion
    larga comprada ahi paga la vol inflada y se come el vol crush al dia
    siguiente del evento, aunque la direccion sea correcta.

    Devuelve {'applied', 'inverted', 'near_iv', 'far_iv', 'ratio'}.
    `applied=False` cuando falta alguno de los dos tramos (no se puede juzgar).
    """
    near = [c for c in candidates if int(c.get("dte", 0)) <= int(cfg["dte_acceptable_min"])]
    far = [c for c in candidates if int(c.get("dte", 0)) >= int(cfg["dte_sweet_min"])]
    near_iv = _median([c.get("iv") for c in near])
    far_iv = _median([c.get("iv") for c in far])
    if near_iv is None or far_iv is None or near_iv <= 0 or far_iv <= 0:
        return {"applied": False, "inverted": False, "near_iv": near_iv,
                "far_iv": far_iv, "ratio": None}
    ratio = near_iv / far_iv
    return {
        "applied": True,
        "inverted": ratio > float(cfg["max_iv_term_ratio"]),
        "near_iv": round(near_iv, 4),
        "far_iv": round(far_iv, 4),
        "ratio": round(ratio, 3),
    }


PREMIUM_STRATEGIES = {"cash_secured_put": "put", "covered_call": "call"}


def parse_target_strike(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def select_premium_contract(
    chain: List[Dict[str, Any]],
    strategy: str,
    target_strike,
    config: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Pick the most liquid contract near a target strike. Buying power is not consulted."""
    want_type = PREMIUM_STRATEGIES.get(strategy)
    if not want_type:
        return None
    target = parse_target_strike(target_strike)
    if target is None:
        return None
    cfg = merge_config(config)
    today = today or date.today()
    target_delta = float(cfg["target_delta"])

    candidates: List[Dict[str, Any]] = []
    for c in chain or []:
        occ = c.get("occ_symbol")
        if not occ:
            continue
        if c.get("type") != want_type:
            continue
        exp = _parse_expiry(c.get("expiry", ""))
        if exp is None:
            continue
        dte = (exp - today).days
        if not (cfg["dte_min"] <= dte <= cfg["dte_max"]):
            continue
        try:
            strike_f = float(c.get("strike"))
            bid = float(c.get("bid") or 0.0)
            ask = float(c.get("ask") or 0.0)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(strike_f) and math.isfinite(bid) and math.isfinite(ask)):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        if not _passes_liquidity(c, cfg):
            continue
        raw_delta = c.get("delta")
        try:
            delta_f = float(raw_delta)
            if not math.isfinite(delta_f):
                delta_gap = float("inf")
            else:
                delta_gap = abs(abs(delta_f) - target_delta)
        except (TypeError, ValueError):
            delta_f = None
            delta_gap = float("inf")
        spread = _spread_pct(bid, ask)
        candidates.append({
            "occ_symbol": occ,
            "type": want_type,
            "strike": strike_f,
            "expiry": c.get("expiry"),
            "dte": dte,
            "bid": bid,
            "ask": ask,
            "delta": delta_f if delta_f is not None else raw_delta,
            "iv": c.get("iv"),
            "volume": _safe_int(c.get("volume")),
            "open_interest": _safe_int(c.get("open_interest")),
            "spread_pct": round(spread, 2),
            "_rank": (abs(strike_f - target), delta_gap, -_safe_int(c.get("open_interest")), spread),
        })

    if not candidates:
        return None
    best = min(candidates, key=lambda row: row["_rank"])
    best.pop("_rank", None)
    return best


def _finite_positive(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def compute_csp_metrics(strike, bid, contracts=1) -> Optional[Dict[str, Any]]:
    strike_f = _finite_positive(strike)
    bid_f = _finite_positive(bid)
    n_raw = _finite_positive(contracts)
    if strike_f is None or bid_f is None or n_raw is None:
        return None
    n = int(n_raw)
    if n <= 0:
        return None
    premium_received = bid_f * 100.0 * n
    cash_collateral = strike_f * 100.0 * n
    return {
        "premium_per_share": bid_f,
        "premium_received": premium_received,
        "cash_collateral": cash_collateral,
        "effective_entry": strike_f - bid_f,
        "max_loss_approx": cash_collateral - premium_received,
        "premium_yield": premium_received / cash_collateral,
    }


def validate_csp_collateral(strike, buying_power, contracts=1) -> Dict[str, Any]:
    strike_f = _finite_positive(strike)
    try:
        n = int(float(contracts))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = 0
    required = (strike_f * 100.0 * n) if strike_f is not None and n > 0 else 0.0

    try:
        bp = float(buying_power)
    except (TypeError, ValueError):
        bp = float("nan")
    bp_valid = math.isfinite(bp) and bp >= 0
    bp_val = bp if bp_valid else 0.0
    per_contract = (strike_f * 100.0) if strike_f is not None else 0.0
    max_c = int(math.floor(bp_val / per_contract)) if bp_valid and per_contract > 0 else 0
    sufficient = bool(bp_valid and n > 0 and strike_f is not None and bp_val >= required)

    reason = ""
    if not sufficient:
        strike_disp = strike_f if strike_f is not None else 0
        reason = (
            f"Selected put at strike {strike_disp:g} requires ${required:,.2f} of collateral; "
            f"buying power available is ${bp_val:,.2f}"
        )
    return {
        "sufficient": sufficient,
        "required_collateral": required,
        "buying_power": bp_val,
        "max_contracts_by_buying_power": max_c,
        "reason": reason,
    }


def covered_call_contracts(shares, short_calls=0) -> Dict[str, Any]:
    UNAVAILABLE = "Covered-call coverage unavailable: could not read option positions"
    try:
        shares_f = float(shares)
    except (TypeError, ValueError):
        shares_f = float("nan")
    shares_owned = shares_f if math.isfinite(shares_f) else 0.0

    if short_calls is None:
        return {
            "contracts": 0,
            "shares_owned": shares_owned,
            "short_calls": None,
            "reason": UNAVAILABLE,
        }

    try:
        short_n = int(float(short_calls))
    except (TypeError, ValueError):
        short_n = 0

    if not math.isfinite(shares_f) or shares_f <= 0:
        return {
            "contracts": 0,
            "shares_owned": shares_owned,
            "short_calls": short_n,
            "reason": "",
        }

    gross = int(math.floor(max(shares_f, 0.0) / 100.0))
    n = max(gross - abs(short_n), 0)
    return {
        "contracts": n,
        "shares_owned": shares_f,
        "short_calls": short_n,
        "reason": "",
    }


def compute_covered_call_metrics(strike, bid, spot, contracts) -> Optional[Dict[str, Any]]:
    strike_f = _finite_positive(strike)
    bid_f = _finite_positive(bid)
    spot_f = _finite_positive(spot)
    n_raw = _finite_positive(contracts)
    if strike_f is None or bid_f is None or spot_f is None or n_raw is None:
        return None
    n = int(n_raw)
    if n <= 0:
        return None
    premium_received = bid_f * 100.0 * n
    notional = spot_f * 100.0 * n
    return {
        "premium_per_share": bid_f,
        "premium_received": premium_received,
        "premium_yield": premium_received / notional,
        "effective_exit": strike_f + bid_f,
        "max_upside_from_current": (strike_f - spot_f) + bid_f,
    }


def evaluate_options_filters(
    chain: List[Dict[str, Any]],
    spot: float,
    direction: str,
    config: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
    realized_vol: Optional[float] = None,
) -> Dict[str, Any]:
    """Decisión completa del selector: contrato + motivo + métricas.

    Además de los filtros clásicos (tipo, ventana DTE, liquidez) aplica las
    reglas de trader de opciones R1/R2/R3/R5 sobre datos que la cadena ya
    traía y que antes se ignoraban. Devuelve siempre un dict con forma:

        {'ok': bool, 'contract': dict|None, 'reason': str, 'stats': {...}}

    `stats` existe para que el rechazo sea EXPLICABLE en los logs y en la UI:
    un agente que dice "no opero" sin decir por qué no se puede auditar.
    """
    def _fail(reason: str, stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"ok": False, "contract": None, "reason": reason,
                "stats": stats or {}}

    if direction not in ("long_call", "long_put"):
        return _fail(f"Unsupported direction '{direction}'")
    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return _fail("Non-numeric underlying price")
    if not math.isfinite(spot_f) or spot_f <= 0:
        return _fail("Non-positive underlying price")
    cfg = merge_config(config)
    today = today or date.today()
    want_type = "call" if direction == "long_call" else "put"

    candidates: List[Dict[str, Any]] = []
    for c in chain or []:
        occ = c.get("occ_symbol")
        if not occ:
            continue
        if c.get("type") != want_type:
            continue
        exp = _parse_expiry(c.get("expiry", ""))
        if exp is None:
            continue
        dte = (exp - today).days
        if not (cfg["dte_min"] <= dte <= cfg["dte_max"]):
            continue
        try:
            strike_f = float(c.get("strike"))
            bid = float(c.get("bid") or 0.0)
            ask = float(c.get("ask") or 0.0)
        except (TypeError, ValueError):
            continue
        if ask <= 0 or bid < 0 or ask < bid:
            continue
        if not _passes_liquidity(c, cfg):
            continue
        candidates.append({
            "occ_symbol": occ,
            "type": want_type,
            "strike": strike_f,
            "expiry": c.get("expiry"),
            "dte": dte,
            "bid": bid,
            "ask": ask,
            "delta": c.get("delta"),
            "iv": c.get("iv"),
            "theta": c.get("theta"),
            "volume": _safe_int(c.get("volume")),
            "open_interest": _safe_int(c.get("open_interest")),
            "spread_pct": round(_spread_pct(bid, ask), 2),
        })

    stats: Dict[str, Any] = {
        "chain_size": len(chain or []),
        "candidates": len(candidates),
        "realized_vol": round(realized_vol, 4) if isinstance(realized_vol, (int, float)) else None,
    }
    if not candidates:
        return _fail("No contract passes DTE/liquidity filters", stats)

    # R5 · Estructura de plazos: rechazo a nivel de SIMBOLO, no de contrato.
    term = iv_term_structure(candidates, cfg)
    stats["term"] = term
    if term["applied"] and term["inverted"]:
        return _fail(
            f"IV term structure inverted ({term['near_iv']:.3f} near vs "
            f"{term['far_iv']:.3f} far, ratio {term['ratio']}): an event is "
            f"priced in, buying premium here means eating the vol crush",
            stats,
        )

    # Contratos que PODEMOS valorar: IV y theta utilizables.
    priced = [c for c in candidates
              if _finite_float(c.get("iv")) is not None and _theta_pct(c) is not None]
    stats["priced"] = len(priced)

    target = float(cfg["target_delta"])

    def _delta_gap(c: Dict[str, Any]) -> Optional[float]:
        d = _finite_float(c.get("delta"))
        if d is None:
            return None
        if direction == "long_call" and d <= 0:
            return None
        if direction == "long_put" and d >= 0:
            return None
        return abs(abs(d) - target)

    def _rank(c: Dict[str, Any]) -> tuple:
        gap = _delta_gap(c)
        return (_dte_tier(c["dte"], cfg),
                999.0 if gap is None else gap,
                abs(c["strike"] - spot_f))

    if priced:
        # Tenemos los datos para juzgar como traders de opciones: se juzga.
        reject_theta = []
        reject_ivrv = []
        for c in priced:
            tp = _theta_pct(c)
            iv = _finite_float(c.get("iv"))
            if tp is not None and tp > float(cfg["max_theta_pct"]):
                reject_theta.append((c["occ_symbol"], round(tp, 2)))
                continue
            if (isinstance(realized_vol, (int, float)) and math.isfinite(realized_vol)
                    and realized_vol > 0 and iv is not None
                    and iv > realized_vol * float(cfg["max_iv_rv_ratio"])):
                reject_ivrv.append((c["occ_symbol"], round(iv / realized_vol, 2)))
                continue
            c["theta_pct"] = round(tp, 2)
            c["iv_rv_ratio"] = (round(iv / realized_vol, 2)
                                if isinstance(realized_vol, (int, float))
                                and realized_vol > 0 and iv is not None else None)
        survivors = [c for c in priced if "theta_pct" in c]
        stats["rejected_theta"] = reject_theta
        stats["rejected_iv_rv"] = reject_ivrv
        stats["survivors"] = len(survivors)
        if not survivors:
            worst = reject_theta[0] if reject_theta else None
            if worst:
                return _fail(
                    f"Every priced contract bleeds more than "
                    f"{cfg['max_theta_pct']}% of premium per day "
                    f"(cheapest decay {min(t for _, t in reject_theta)}%/day)",
                    stats,
                )
            ratio = reject_ivrv[0][1] if reject_ivrv else None
            return _fail(
                f"Implied volatility is rich vs realized "
                f"({realized_vol:.3f}): cheapest contract trades at "
                f"{ratio}x realized vol (cap {cfg['max_iv_rv_ratio']}x)",
                stats,
            )

        best = min(survivors, key=_rank)
        best["dte_tier"] = _dte_tier(best["dte"], cfg)
        stats["dte_tier"] = best["dte_tier"]
        stats["theta_pct"] = best.get("theta_pct")
        stats["iv_rv_ratio"] = best.get("iv_rv_ratio")
        return {"ok": True, "contract": best, "reason": "", "stats": stats}

    # Degradación documentada: el feed no trajo IV/theta utilizables para este
    # simbolo. Se opera igual con delta+liquidez (comportamiento histórico) en
    # lugar de fingir un juicio que no podemos hacer, y queda registrado.
    stats["greeks_degraded"] = True
    with_delta = [c for c in candidates if _delta_gap(c) is not None]
    best = min(with_delta, key=_rank, default=None)
    if best is None:
        # Fallback ATM: strike más cercano al spot entre los líquidos
        best = min(candidates, key=lambda c: abs(c["strike"] - spot_f))
        stats["atm_fallback"] = True
    best["dte_tier"] = _dte_tier(best["dte"], cfg)
    stats["dte_tier"] = best["dte_tier"]
    return {"ok": True, "contract": best, "reason": "", "stats": stats}


def select_option_contract(
    chain: List[Dict[str, Any]],
    spot: float,
    direction: str,
    config: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
    realized_vol: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Selecciona el mejor contrato 1-leg (wrapper: ver `evaluate_options_filters`).

    Mantiene la firma histórica (Optional[Dict]) para no romper a los llamadores
    ni a los tests. Los motivos de rechazo viven en `evaluate_options_filters`.
    """
    return evaluate_options_filters(
        chain, spot, direction, config, today=today, realized_vol=realized_vol,
    )["contract"]


def _zero_sizing(risk_budget_usd: float = 0.0) -> Dict[str, Any]:
    """Fail-closed sizing payload: no trade."""
    return {
        "qty": 0,
        "premium_usd": 0.0,
        "risk_usd": 0.0,
        "risk_budget_usd": round(risk_budget_usd, 2),
        "floor_applied": False,
    }


def size_position(
    ask: float,
    equity: float,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Risk-based sizing: the budget is the loss taken if the stop fires.

    risk_per_contract = ask x 100 x stop_loss_pct / 100
    qty               = min(floor(risk_budget / risk_per_contract), max_contracts)

    A long option's premium is not immobilized capital the way a stock
    position is, so budgeting on premium paid understates the position.
    Loss-at-stop is the number the risk limit should apply to.

    Floor rule: if the computed qty is 0 but a single contract's loss-at-stop
    still fits under max_risk_per_trade_pct% of equity, trade 1 contract and
    flag floor_applied. Fail-closed on invalid or non-finite inputs.
    """
    cfg = merge_config(config)
    try:
        ask_f = float(ask)
        eq_f = float(equity)
    except (TypeError, ValueError):
        return _zero_sizing()
    if not math.isfinite(ask_f) or not math.isfinite(eq_f):
        return _zero_sizing()
    if ask_f <= 0 or eq_f <= 0:
        return _zero_sizing()

    risk_budget_usd = eq_f * float(cfg["risk_per_trade_pct"]) / 100.0
    max_risk_usd = eq_f * float(cfg["max_risk_per_trade_pct"]) / 100.0
    cost_per_contract = ask_f * 100.0
    risk_per_contract = cost_per_contract * float(cfg["stop_loss_pct"]) / 100.0
    if risk_per_contract <= 0 or risk_budget_usd <= 0:
        return _zero_sizing(risk_budget_usd)

    qty = int(math.floor(risk_budget_usd / risk_per_contract))
    qty = min(qty, int(cfg["max_contracts"]))
    floor_applied = False
    if qty < 1:
        if risk_per_contract <= max_risk_usd:
            qty = 1
            floor_applied = True
        else:
            return _zero_sizing(risk_budget_usd)

    return {
        "qty": qty,
        "premium_usd": round(qty * cost_per_contract, 2),
        "risk_usd": round(qty * risk_per_contract, 2),
        "risk_budget_usd": round(risk_budget_usd, 2),
        "floor_applied": floor_applied,
    }


def build_exit_plan(
    entry_ask: float,
    expiry_str: str,
    config: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """TP/SL como primas absolutas + time-stop DTE antes del vencimiento.

    Fail-closed: entry inválido o no finito -> primas None (plan no disponible),
    min_exit_date sigue calculándose si el expiry es parseable.
    """
    cfg = merge_config(config)
    exp = _parse_expiry(expiry_str)
    today = today or date.today()
    # Nota: si el expiry standalone ya pasó, min_exit puede quedar en el pasado;
    # el pipeline de selección garantiza DTE >= dte_min (7) > min_dte_exit (5).
    if exp is None:
        min_exit = today.isoformat()
    else:
        min_exit = (exp - timedelta(days=int(cfg["min_dte_exit"]))).strftime("%Y-%m-%d")

    try:
        entry = float(entry_ask)
    except (TypeError, ValueError):
        entry = float("nan")
    if not math.isfinite(entry) or entry <= 0:
        return {"tp_premium": None, "sl_premium": None, "min_exit_date": min_exit}

    return {
        "tp_premium": round(entry * (1.0 + float(cfg["take_profit_pct"]) / 100.0), 4),
        "sl_premium": round(entry * (1.0 - float(cfg["stop_loss_pct"]) / 100.0), 4),
        "min_exit_date": min_exit,
    }


def resolve_direction(signal: Optional[str]) -> Optional[str]:
    """Mapea la señal del torneo técnico a una dirección de opciones 1-leg.

    STRONG BUY / ACCUMULATE -> long_call · SELL TAKE PROFIT / REDUCE -> long_put.
    """
    if not signal:
        return None
    s = str(signal).strip().upper()
    if s in ("STRONG BUY", "ACCUMULATE"):
        return "long_call"
    if s in ("SELL / TAKE PROFIT", "REDUCE"):
        return "long_put"
    return None
