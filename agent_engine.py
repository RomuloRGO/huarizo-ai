"""Cerebro del Agente IA y Torneo de Estrategias para Huarizo AI.

Integra:
  1. Torneo de Estrategias Automático (Multi-Strategy Tournament).
  2. Fusión de Análisis Técnico, Dividendos de Corporate Actions y Sentimiento de Benzinga.
  3. Síntesis de Tesis de Inversión (LLM Gemini / Fallback Determinista).
  4. Calibración de Bracket Orders Inteligentes (Entry, Take-Profit, Stop-Loss).
"""

import os
import json
import time
import requests
import pandas as pd
from datetime import date
from typing import Dict, List, Any, Optional, Set

from technical_engine import compute_technical_indicators
from pattern_engine import detect_candlestick_patterns, detect_chart_patterns
from backtest_engine import run_backtest, STRATEGY_PRESETS
from risk_gate import (
    DEFAULT_RISK_LIMITS,
    DailyLossTracker,
    OCC_SYMBOL_RE,
    assert_option_symbol,
    build_client_order_id,
    evaluate_daily_loss,
    evaluate_new_entry,
    is_option_symbol,
    size_order_qty,
)
import llm_provider
from options_engine import (
    merge_config,
    evaluate_options_filters,
    realized_volatility,
    select_option_contract,
    size_position,
    build_exit_plan,
    resolve_direction,
    PREMIUM_STRATEGIES,
    parse_target_strike,
    select_premium_contract,
    compute_csp_metrics,
    validate_csp_collateral,
    covered_call_contracts,
    compute_covered_call_metrics,
)

# ── Política options-only (hackathon Alpaca 2026) ─────────────────────────────
# El concurso exige opciones y el autopilot de acciones protagonizó el incidente
# del 2026-08-31 (180 órdenes en 18 s). Por defecto queda APAGADO: hay que
# habilitarlo explícitamente con HUARIZO_STOCK_AUTOPILOT=true. Fail-closed.
STOCK_AUTOPILOT_ENABLED = os.getenv("HUARIZO_STOCK_AUTOPILOT", "false").lower() == "true"

# `is_option_symbol` / `assert_option_symbol` viven en `risk_gate` (módulo puro)
# y se re-exportan aquí: el agente los usa y las Tasks 3 y 4 los consumen.
__all__ = ["HuarizoAgent", "STOCK_AUTOPILOT_ENABLED",
           "is_option_symbol", "assert_option_symbol", "OCC_SYMBOL_RE"]


class HuarizoAgent:
    """Agente de trading multi-estrategia autónomo sobre Alpaca Markets."""

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        risk_limits: Optional[Dict[str, Any]] = None,
        risk_state_path: Optional[str] = None,
    ):
        self.gemini_api_key = gemini_api_key or os.getenv("GEMINI_API_KEY", "")
        cfg = dict(DEFAULT_RISK_LIMITS)
        if isinstance(risk_limits, dict):
            cfg.update({k: v for k, v in risk_limits.items() if k in cfg})
        self.risk_limits = cfg
        # La pausa por pérdida diaria se persistía solo en memoria: reiniciar el
        # servidor la borraba. Ahora vive en disco.
        self.risk_tracker = DailyLossTracker(risk_state_path)

    def run_strategy_tournament(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Ejecuta un torneo de backtests entre todos los presets para seleccionar el óptimo."""
        tournament_results = []
        for strat_key in STRATEGY_PRESETS.keys():
            res = run_backtest(df, strategy=strat_key)
            if res.get("available"):
                tournament_results.append({
                    "strategy": strat_key,
                    "name": res["strategy_name"],
                    "win_rate": res["win_rate"],
                    "profit_factor": res["profit_factor"],
                    "max_drawdown": res["max_drawdown_pct"],
                    "total_return": res["total_return_pct"],
                    "composite_score": res["composite_score"],
                    "total_trades": res["total_trades"]
                })

        # Ordenar por Score Compuesto descendente
        tournament_results.sort(key=lambda x: x["composite_score"], reverse=True)
        winner = tournament_results[0] if tournament_results else None

        return {
            "available": winner is not None,
            "tournament": tournament_results,
            "winner": winner
        }

    def _generate_llm_thesis(
        self,
        ticker: str,
        price: float,
        trend: str,
        rsi: float,
        stoch: Dict[str, Any],
        pattern: Optional[str],
        news: List[Dict[str, Any]],
        winning_strat: Dict[str, Any],
        signal: str
    ) -> Dict[str, Any]:
        """Generates an explanatory investment thesis via Google Gemini or fallback.

        Returns `thesis_source` ("llm" | "deterministic") and `thesis_error`
        (None | "no_gemini_key" | "<Tipo>: <mensaje>") on EVERY path: un juez
        tiene que poder distinguir una tesis de Gemini de una plantilla, así
        que el motivo del fallback nunca se descarta.

        Quién decide si hay LLM: `llm_provider.available()`, no este módulo.
        Durante meses el gate fue `if self.gemini_api_key:`, que era correcto
        con una sola ruta. Con Vertex (Application Default Credentials, sin
        clave) esa condición es falsa en el caso justo que se quiere cubrir:
        `GEMINI_API_KEY` vacío pero Vertex operativo. Adivinar la disponibilidad
        desde aquí duplica reglas que ya vive en `llm_provider` y se desactualiza
        en silencio, así que aquí solo se pregunta.
        """
        thesis_error: Optional[str] = None

        if llm_provider.available(self.gemini_api_key):
            try:
                # El transporte vive en `llm_provider`: Vertex (ADC, sin clave),
                # el SDK nuevo (google-genai) y el legacy, con modelo
                # configurable y tope diario para no pasarse del credito. Sin
                # ruta, sin cupo o con el ADC caducado levanta LLMError; aqui
                # solo se parsea la respuesta.
                news_summary = " | ".join([n.get("headline", "") for n in news[:3]])

                # Métricas no medidas se declaran como tal. Interpolar el None
                # crudo mandaba "Profit Factor: Nonex" al modelo, que podía
                # devolverlo como si fuera un dato real.
                _wr = winning_strat.get("win_rate")
                _pf = winning_strat.get("profit_factor")
                _wr_txt = f"{_wr}%" if _wr is not None else "not measured"
                _pf_txt = f"{_pf}x" if _pf is not None else "not measured (undefined: no losing trades)"
                if _wr is None and _pf is None:
                    _track = "no validated backtest metrics were produced for this asset"
                else:
                    _track = f"Win Rate: {_wr_txt}, Profit Factor: {_pf_txt}"

                prompt = f"""
                You are Huarizo AI, a quantitative trading agent for Alpaca Markets.
                Analyze the following data for {ticker}:
                - Price: ${price:.2f} USD
                - Technical Regime: {trend}, RSI: {rsi:.1f}, Stochastic: %K={stoch.get('k')}, %D={stoch.get('d')} ({stoch.get('cross')})
                - Candlestick Pattern: {pattern or 'No relevant pattern'}
                - Backtest Tournament Winning Strategy: {winning_strat.get('name')} ({_track})
                - Benzinga News: {news_summary}
                - Determined Signal: {signal}

                Generate JSON with:
                1. "headline": short, punchy verdict sentence.
                2. "thesis_points": list of 3 clear, direct points (technical, strategy/tournament, fundamentals/news).
                3. "risk_level": "LOW", "MEDIUM", or "HIGH".
                Write ALL text in ENGLISH. Return ONLY the JSON without markdown.
                """
                result = llm_provider.complete(
                    prompt,
                    api_key=self.gemini_api_key,
                    cache_key=ticker,
                )
                txt = result["text"].replace("```json", "").replace("```", "").strip()
                return {
                    **json.loads(txt),
                    "thesis_source": "llm",
                    "thesis_error": None,
                    "llm_provider": result.get("provider"),
                    "llm_model": result.get("model"),
                }
            except Exception as e:
                # Nunca en silencio: se registra y se cae al fallback, pero el
                # motivo viaja en `thesis_error` hasta la respuesta de la API.
                # `LLMError` ya se imprime como "<tipo>: <mensaje>".
                thesis_error = str(e)

        # High-quality deterministic fallback
        strat_name = winning_strat.get("name", "Quantitative Strategy")
        # Desde la Task 6 `profit_factor` puede ser None (indeterminado: cero
        # pérdidas). `dict.get(key, default)` NO aplica el default cuando la
        # clave existe con valor None, y usar 65.0 / 1.8 como default afirmaba
        # un track record jamás medido. Desconocido se queda desconocido.
        win_rate = winning_strat.get("win_rate")
        pf = winning_strat.get("profit_factor")

        thesis_p1 = f"Technical structure in a {trend} regime with RSI at {rsi:.1f} and Stochastic in {stoch.get('cross')} ({stoch.get('zone')})."

        if win_rate is None and pf is None:
            thesis_p2 = (f"The backtest tournament selected '{strat_name}' as the best available fit, "
                         "but no validated win rate or profit factor was produced for this asset: "
                         "the edge is unproven.")
        else:
            parts = []
            if win_rate is not None:
                parts.append(f"{win_rate}% Win Rate")
            if pf is not None:
                parts.append(f"{pf}x Profit Factor")
            else:
                parts.append("Profit Factor n/d (undefined: no losing trades in the sample)")
            thesis_p2 = (f"The backtest tournament selected '{strat_name}' as the optimal fit "
                         f"for this asset with {' and '.join(parts)}.")

        if news:
            thesis_p3 = f"Active corporate sentiment on Benzinga: '{news[0]['headline'][:70]}...'."
        else:
            thesis_p3 = "Volatility and institutional liquidity conditions are adequate for execution on Alpaca."

        # Riesgo: sin win rate validado no hay evidencia para prometer LOW, así
        # que lo desconocido se queda en MEDIUM (fail-conservative).
        risk_level = "LOW" if signal in ("STRONG BUY", "ACCUMULATE") and win_rate is not None and win_rate >= 70 else "MEDIUM"

        return {
            "headline": f"Signal {signal}: Opportunity identified under {strat_name}",
            "thesis_points": [thesis_p1, thesis_p2, thesis_p3],
            "risk_level": risk_level,
            "thesis_source": "deterministic",
            # `no_gemini_key` es el token de "ninguna ruta de LLM disponible"
            # (ni clave de Gemini ni Vertex activo). Se conserva tal cual porque
            # la UI y los tests lo comparan por igualdad; si alguna vez se
            # quiere distinguir, hay que cambiarlo en los dos sitios a la vez.
            "thesis_error": thesis_error if thesis_error is not None else "no_gemini_key",
        }

    def analyze(self, ticker: str, service: Any) -> Dict[str, Any]:
        """Ejecuta el análisis integral del Agente sobre un ticker en Alpaca."""
        ticker = ticker.upper()
        
        # 1. Obtener datos de Alpaca
        bars = service.get_stock_bars(ticker, days=250)
        if bars.empty or len(bars) < 30:
            return {"available": False, "reason": f"Insufficient Alpaca data for {ticker}"}

        snapshot = service.get_stock_snapshot(ticker)
        news = service.get_stock_news(ticker, limit=4)
        dividends = service.get_dividends(ticker, days=365)

        # Volatilidad realizada del subyacente: es la referencia con la que el
        # selector de opciones juzga si el IV del contrato esta caro (regla R2).
        # Sale de las MISMAS barras que ya pidio el analisis tecnico, asi que
        # no cuesta ni una llamada a la API. None si no alcanzan los datos.
        try:
            realized_vol = realized_volatility(bars["Close"], window=30)
        except Exception:
            realized_vol = None

        # 2. Motores Técnicos y Patrones
        tech = compute_technical_indicators(bars)
        candles = detect_candlestick_patterns(bars)
        charts = detect_chart_patterns(bars)

        # 3. Torneo de Estrategias
        tournament_res = self.run_strategy_tournament(bars)
        # Sin ganador del torneo NO hay métricas: 65.0 / 1.8 eran números
        # inventados que después la tesis afirmaba como si fueran medidos.
        winning_strat = tournament_res.get("winner") or {
            "strategy": "trend_following",
            "name": "Trend Following Master",
            "win_rate": None,
            "profit_factor": None
        }

        # 4. Evaluación de Reglas de Decisión
        curr_price = tech["price"]
        trend_regime = tech["trend"]["regime"]
        rsi_val = tech["rsi"]["value"]
        stoch = tech["stochastic"]
        atr_val = tech["volatility"]["atr_20"]
        latest_candle = candles.get("latest")
        pattern_name = latest_candle.get("pattern") if latest_candle else None
        candle_type = latest_candle.get("type") if latest_candle else "neutral"

        # Lógica de Decisión Cuantitativa
        score = 50  # Base neutral

        # Factor Tendencia
        if "BULLISH" in trend_regime:
            score += 20
        elif "BEARISH" in trend_regime:
            score -= 20

        # Factor Momentum / Estocástico
        if stoch["cross"] == "BULLISH_CROSS" or stoch["zone"] == "OVERSOLD (<20)":
            score += 15
        elif stoch["cross"] == "BEARISH_CROSS" and stoch["zone"] == "OVERBOUGHT (>80)":
            score -= 15

        # Factor RSI
        if rsi_val < 35:
            score += 10
        elif rsi_val > 70:
            score -= 10

        # Factor Vela
        if candle_type == "bullish":
            score += 10
        elif candle_type == "bearish":
            score -= 10

        # Determinación de Señal
        if score >= 75:
            signal = "STRONG BUY"
            action_badge = "success"
        elif score >= 60:
            signal = "ACCUMULATE"
            action_badge = "info"
        elif score <= 35:
            signal = "SELL / TAKE PROFIT"
            action_badge = "danger"
        elif score <= 45:
            signal = "REDUCE"
            action_badge = "warning"
        else:
            signal = "HOLD"
            action_badge = "secondary"

        # 5. Calibración de Bracket Order
        if signal in ("STRONG BUY", "ACCUMULATE"):
            take_profit = round(curr_price + (3.0 * atr_val), 2)
            stop_loss = round(max(0.01, curr_price - (2.0 * atr_val)), 2)
        elif signal in ("SELL / TAKE PROFIT", "REDUCE"):
            take_profit = round(curr_price, 2)
            stop_loss = round(curr_price + (2.0 * atr_val), 2)
        else:
            take_profit = round(curr_price + (2.0 * atr_val), 2)
            stop_loss = round(max(0.01, curr_price - (1.5 * atr_val)), 2)

        # 6. Generación de Tesis Explicativa
        thesis_data = self._generate_llm_thesis(
            ticker=ticker,
            price=curr_price,
            trend=trend_regime,
            rsi=rsi_val,
            stoch=stoch,
            pattern=pattern_name,
            news=news,
            winning_strat=winning_strat,
            signal=signal
        )

        # 7. Formatear serie de velas para el gráfico interactivo
        candles_series = []
        for idx, row in bars.tail(120).iterrows():
            # Extraer fecha correctamente (columna 't', 'timestamp', 'Date', etc.)
            raw_time = row.get("t", row.get("timestamp", row.get("Date", row.get("date", idx))))
            if hasattr(raw_time, "strftime"):
                t_str = raw_time.strftime("%Y-%m-%d")
            else:
                t_str = str(raw_time)[:10]

            o_val = row.get("Open", row.get("open", 0.0))
            h_val = row.get("High", row.get("high", 0.0))
            l_val = row.get("Low", row.get("low", 0.0))
            c_val = row.get("Close", row.get("close", 0.0))
            v_val = row.get("Volume", row.get("volume", 0.0))
            
            if t_str and len(t_str) >= 8 and o_val > 0:
                candles_series.append({
                    "time": t_str,
                    "open": round(float(o_val), 2),
                    "high": round(float(h_val), 2),
                    "low": round(float(l_val), 2),
                    "close": round(float(c_val), 2),
                    "volume": float(v_val)
                })

        # Deduplicar por fecha si fuera necesario y ordenar
        unique_candles = {}
        for c in candles_series:
            unique_candles[c["time"]] = c
        sorted_candles = sorted(unique_candles.values(), key=lambda x: x["time"])

        return {
            "available": True,
            "symbol": ticker,
            "price": curr_price,
            "change_pct": snapshot.get("change_pct", 0.0),
            "realized_vol": realized_vol,
            "signal": signal,
            "confidence_score": min(98, max(50, score)),
            "action_badge": action_badge,
            "headline": thesis_data.get("headline", ""),
            "thesis_points": thesis_data.get("thesis_points", []),
            "risk_level": thesis_data.get("risk_level", "MEDIUM"),
            # Transparencia: si la tesis vino de Gemini o del fallback (y por qué).
            "thesis_source": thesis_data.get("thesis_source", "deterministic"),
            "thesis_error": thesis_data.get("thesis_error"),
            # Qué ruta y qué modelo la escribieron, o None si fue plantilla.
            # Sin esto la respuesta dice "vino del LLM" sin decir de cuál: no se
            # puede demostrar que se usa Vertex ni distinguir 3.8 de 2.5. Van
            # siempre presentes (aunque sean None) para que el UI no tenga que
            # adivinar si la clave existe.
            "llm_provider": thesis_data.get("llm_provider"),
            "llm_model": thesis_data.get("llm_model"),
            "technical": tech,
            "patterns": {
                "candle": latest_candle,
                "chart": charts.get("active")
            },
            "tournament": tournament_res,
            "winning_strategy": winning_strat,
            # Confirmación por volumen. Está también dentro de
            # `technical.volume`; se expone arriba porque el ciclo autónomo la
            # consulta directamente y no debería tener que saber dónde vive el
            # indicador. NO es un factor del score.
            "volume_confirmation": {
                "ratio": tech.get("volume", {}).get("ratio"),
                "level": tech.get("volume", {}).get("level", "unknown"),
                "confirms_entry": tech.get("volume", {}).get("confirms_entry", False),
            },
            "candles_series": sorted_candles,
            "bracket_order_plan": {
                "entry_price": curr_price,
                "take_profit_price": take_profit,
                "stop_loss_price": stop_loss,
                "atr": atr_val,
                "risk_reward": round((take_profit - curr_price) / (curr_price - stop_loss + 1e-9), 2)
            },
            "news": news[:3],
            "dividends": dividends[:3]
        }

    def build_options_proposal(
        self,
        ticker: str,
        service: Any,
        config_overrides: Optional[Dict[str, Any]] = None,
        today: Optional[date] = None,
        strategy_type: Optional[str] = None,
        premium_strategy: Optional[str] = None,
        target_strike: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Pipeline de propuestas de opciones 1-leg:
        senal -> seleccion -> sizing -> tesis -> salidas.
        Fail-closed: {'available': False, 'reason': ...} ante cualquier bloqueo.
        El parametro `today` es inyectable para tests deterministas
        (si es None se usa la fecha actual).
        Omitting strategy_type keeps the directional pipeline.
        """
        ticker = ticker.upper()
        cfg = merge_config(config_overrides)

        if str(strategy_type or "").strip().lower() == "premium_income":
            return self._build_premium_proposal(
                ticker, service, cfg, premium_strategy, target_strike, today=today
            )

        base = self.analyze(ticker, service)
        if not base.get("available"):
            return {"available": False,
                    "reason": base.get("reason", "Base analysis unavailable")}

        direction = resolve_direction(base.get("signal"))
        if direction is None:
            return {"available": False,
                    "reason": f"Signal '{base.get('signal')}' not eligible for "
                              "directional options"}

        contracts = service.get_options_chain(
            ticker,
            dte_min=int(cfg["dte_min"]),
            dte_max=int(cfg["dte_max"]),
            today=today,
        )
        if not contracts:
            return {"available": False,
                    "reason": f"No options chain available for {ticker}"}
        try:
            spot = float(base["price"])
        except (TypeError, ValueError):
            return {"available": False, "reason": "Non-numeric underlying price"}
        try:
            contracts = service.enrich_chain_with_oi(
                contracts,
                spot=spot,
                dte_min=int(cfg["dte_min"]),
                dte_max=int(cfg["dte_max"]),
                today=today,
                # Sin esto solo se enriquece el vencimiento mas cercano y el
                # selector no puede elegir otro tramo (ver el comentario en
                # `enrich_chain_with_oi`).
                dte_sweet_min=int(cfg["dte_sweet_min"]),
            )
        except Exception as e:
            print(f"[options_proposal] Error enriching OI: {e}")  # OI is best-effort

        # Reglas de trader de opciones (R1 DTE, R2 IV vs RV, R3 theta,
        # R5 estructura de plazos). `realized_vol` viene del analisis del
        # subyacente; si es None el filtro R2 se salta, no bloquea.
        decision = evaluate_options_filters(
            contracts, spot, direction, cfg, today=today,
            realized_vol=base.get("realized_vol"),
        )
        contract = decision["contract"]
        if contract is None:
            return {"available": False,
                    "reason": decision["reason"] or "No contract passes liquidity/DTE filters",
                    "filter_stats": decision["stats"]}

        try:
            equity = float(service.get_account().get("equity", 0.0))
        except Exception:
            equity = 0.0
        sizing = size_position(contract["ask"], equity, cfg)
        if sizing["qty"] <= 0:
            # `filter_stats` tambien aqui. Era el unico rechazo que se iba sin
            # el, y es justo el que mas confunde: el contrato PASO todos los
            # filtros de opciones y aun asi no se opera. Sin el embudo, la UI
            # mostraba el motivo y ninguna prueba de que el contrato era bueno.
            return {"available": False,
                    "reason": "Position sizing resolved to 0 contracts "
                              "(loss-at-stop exceeds the max risk per trade)",
                    "filter_stats": decision["stats"]}

        exit_plan = build_exit_plan(contract["ask"], contract["expiry"], cfg, today=today)

        strat_name = (base.get("winning_strategy") or {}).get("name", "Quantitative Strategy")
        iv_val = contract.get("iv")
        iv_txt = f"{iv_val:.0%}" if isinstance(iv_val, (int, float)) else "N/A"
        tp_val = exit_plan.get("tp_premium")
        sl_val = exit_plan.get("sl_premium")
        if tp_val is None or sl_val is None:
            return {"available": False, "reason": "Failed to build a valid exit plan"}

        # Cuarto punto de tesis: los controles ESPECIFICOS DE OPCIONES. Sin el
        # la propuesta solo defendia la direccion (indicadores de la accion) y
        # ocultaba que el contrato se elegia solo por delta.
        rv_val = base.get("realized_vol")
        theta_txt = (f"{contract['theta_pct']}%/day"
                     if contract.get("theta_pct") is not None else "n/a")
        rv_txt = (f"{rv_val:.0%}" if isinstance(rv_val, (int, float)) else "n/a")
        ratio_txt = (f"{contract['iv_rv_ratio']}x" if contract.get("iv_rv_ratio") is not None
                     else "n/a")
        term = (decision.get("stats") or {}).get("term") or {}
        term_txt = (f"IV term structure {term['ratio']}x (near/far), no event priced in"
                    if term.get("applied") else "IV term structure not verifiable")
        options_checks = (
            f"Options checks: {contract['dte']} DTE (sweet spot >= "
            f"{cfg['dte_sweet_min']}), theta burn {theta_txt} (cap "
            f"{cfg['max_theta_pct']}%/day), IV {iv_txt} vs {rv_txt} realized "
            f"({ratio_txt}, cap {cfg['max_iv_rv_ratio']}x), {term_txt}."
        )

        thesis = {
            "headline": (f"{direction.replace('_', ' ').title()} on {ticker}: "
                         f"{strat_name} ({base.get('signal')})"),
            "points": [
                (f"Signal {base.get('signal')} "
                 f"(score {base.get('confidence_score')}/100) from the strategy tournament."),
                (f"Selected contract: ${contract['strike']:.0f} strike with delta "
                 f"{contract.get('delta')}, IV {iv_txt}, {contract.get('spread_pct')}% spread."),
                (f"Risk defined: entry ${contract['ask']:.2f}, "
                 f"TP ${tp_val:.2f} (+{cfg['take_profit_pct']:.0f}%), "
                 f"SL ${sl_val:.2f} (-{cfg['stop_loss_pct']:.0f}%), "
                 f"time-stop {exit_plan['min_exit_date']}."),
                options_checks,
            ],
            "risk_level": "HIGH" if base.get("confidence_score", 0) < 70 else "MEDIUM",
        }

        return {
            "available": True,
            "ticker": ticker,
            "direction": direction,
            "signal": base.get("signal"),
            "confidence_score": base.get("confidence_score"),
            "spot": base["price"],
            "realized_vol": rv_val,
            "contract": contract,
            "sizing": sizing,
            "exit_plan": exit_plan,
            "filter_stats": decision["stats"],
            "thesis": thesis,
            # Confirmación por volumen del subyacente. Viaja con la propuesta
            # para que el ciclo autónomo pueda aplicar el freno sin volver a
            # calcular indicadores. NO modifica el score ni el contrato.
            "volume_confirmation": base.get("volume_confirmation")
                                   or {"ratio": None, "level": "unknown",
                                       "confirms_entry": False},
        }

    def _build_premium_proposal(
        self,
        ticker: str,
        service: Any,
        cfg: Dict[str, Any],
        premium_strategy: Optional[str],
        target_strike: Optional[Any],
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        strategy = str(premium_strategy or "").strip().lower()
        if strategy not in PREMIUM_STRATEGIES:
            return {"available": False,
                    "reason": f"Unknown premium strategy '{premium_strategy}'"}

        base = self.analyze(ticker, service)
        if not base.get("available"):
            return {"available": False,
                    "reason": base.get("reason", "Base analysis unavailable")}
        try:
            spot = float(base["price"])
        except (TypeError, ValueError):
            return {"available": False, "reason": "Non-numeric underlying price"}
        if not spot or spot <= 0:
            return {"available": False, "reason": "Non-numeric underlying price"}

        coverage = None
        contracts_qty = 1
        if strategy == "covered_call":
            shares = service.get_stock_position(ticker)
            if shares is None:
                return {"available": False,
                        "reason": "Could not read stock position for coverage"}
            short_calls = service.get_open_short_call_contracts(ticker)
            coverage = covered_call_contracts(shares, short_calls)
            if coverage["contracts"] == 0:
                reason = coverage.get("reason") or (
                    f"Insufficient shares to cover a call: {float(shares):.0f} shares owned"
                )
                return {"available": False, "reason": reason}
            contracts_qty = int(coverage["contracts"])
            parsed = parse_target_strike(target_strike)
            target = parsed if parsed is not None else spot
        else:
            target = parse_target_strike(target_strike)
            if target is None:
                return {"available": False,
                        "reason": f"Invalid target strike '{target_strike}'"}

        chain = service.get_options_chain(
            ticker,
            dte_min=int(cfg["dte_min"]),
            dte_max=int(cfg["dte_max"]),
            today=today,
        )
        if not chain:
            return {"available": False,
                    "reason": f"No options chain available for {ticker}"}
        try:
            chain = service.enrich_chain_with_oi(
                chain,
                spot=spot,
                dte_min=int(cfg["dte_min"]),
                dte_max=int(cfg["dte_max"]),
                today=today,
            )
        except Exception as e:
            print(f"[options_proposal] Error enriching OI: {e}")

        contract = select_premium_contract(
            chain, strategy, target, config=cfg, today=today
        )
        if contract is None:
            kind = "put" if strategy == "cash_secured_put" else "call"
            return {"available": False,
                    "reason": (f"No liquid {kind} found near strike {target:g} "
                               f"within the {int(cfg['dte_min'])}-{int(cfg['dte_max'])} "
                               "DTE window")}

        collateral = None
        metrics = None
        if strategy == "cash_secured_put":
            try:
                buying_power = float(service.get_account().get("buying_power", 0.0))
            except Exception:
                buying_power = 0.0
            collateral = validate_csp_collateral(contract["strike"], buying_power, contracts=1)
            max_by_bp = int(collateral.get("max_contracts_by_buying_power") or 0)
            qty = min(1, max_by_bp, int(cfg["max_contracts"]))
            if qty <= 0:
                return {"available": False,
                        "reason": collateral.get("reason") or "Insufficient collateral"}
            metrics = compute_csp_metrics(contract["strike"], contract["bid"], qty)
            if metrics is None:
                return {"available": False, "reason": "Could not compute CSP metrics"}
            contracts_qty = qty
        else:
            qty = min(contracts_qty, int(cfg["max_contracts"]))
            if qty <= 0:
                return {"available": False, "reason": "Covered-call quantity resolved to 0"}
            metrics = compute_covered_call_metrics(
                contract["strike"], contract["bid"], spot, qty
            )
            if metrics is None:
                return {"available": False, "reason": "Could not compute covered-call metrics"}
            contracts_qty = qty

        strat_name = (base.get("winning_strategy") or {}).get("name", "Quantitative Strategy")
        iv_val = contract.get("iv")
        iv_txt = f"{iv_val:.0%}" if isinstance(iv_val, (int, float)) else "N/A"
        label = "Cash-secured put" if strategy == "cash_secured_put" else "Covered call"
        thesis = (
            f"{label} on {ticker}: {strat_name} ({base.get('signal')}). "
            f"Sell to open {contract.get('occ_symbol')} at bid ${contract['bid']:.2f}, "
            f"IV {iv_txt}."
        )
        payload = {
            "available": True,
            "ticker": ticker,
            "spot": spot,
            "branch": "premium_income",
            "strategy": strategy,
            "contract": contract,
            "quantity": int(contracts_qty),
            "action": "sell_to_open",
            "limit_price": float(contract["bid"]),
            "metrics": metrics,
            "thesis": thesis,
            "manual_confirmation_required": True,
            "account_snapshot_note": (
                "Coverage and buying power reflect the account at proposal time."
            ),
        }
        if collateral is not None:
            payload["collateral"] = collateral
        if coverage is not None:
            payload["coverage"] = coverage
        return payload

    def run_autonomous_autopilot(
        self,
        service: Any,
        auto_submit: bool = True,
        max_orders: int = 3,
        candidate_tickers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Escanea automáticamente el universo de mercado, corre torneos y ejecuta Bracket Orders.

        Desde el 2026-09-01 toda orden pasa por `risk_gate.evaluate_new_entry`
        antes de salir a Alpaca. El 31-ago este método envió 180 órdenes en
        18 segundos porque solo contaba las órdenes del ciclo en curso: nunca
        miró cuántas posiciones había abiertas, ni el buying power disponible,
        ni la pérdida acumulada del día.

        Desde el 2026-09-01 el flujo autónomo es options-only: con
        `STOCK_AUTOPILOT_ENABLED` en False este método devuelve antes de tocar
        la cuenta. Las órdenes de opciones viven en `/api/options/order`.
        """
        if not STOCK_AUTOPILOT_ENABLED:
            return {
                "success": True,
                "paused": False,
                "reason": "stock_autopilot_disabled",
                "candidates_scanned": 0,
                "opportunities_count": 0,
                "orders_executed_count": 0,
                "blocked_orders_count": 0,
                "executed_orders": [],
                "opportunities": [],
                "blocked_orders": [],
                "logs": ["[OPTIONS ONLY] Stock autopilot disabled by policy; "
                         "hackathon flow trades options exclusively."],
            }

        MIN_PRICE_USD = 5.00

        day_key = date.today().isoformat()

        # Pausas persistentes. Antes vivían solo en memoria y reiniciar el
        # servidor las borraba, así que el agente volvía a operar.
        # `is_paused` NO cubre la pausa de safety, así que se consulta el
        # estado completo: son dos motivos distintos con dos salidas distintas.
        pauses = self.risk_tracker.pause_state(day_key)

        def _paused(reason: str, log: str):
            return {
                "success": True,
                "paused": True,
                "reason": reason,
                "candidates_scanned": 0,
                "opportunities_count": 0,
                "orders_executed_count": 0,
                "blocked_orders_count": 0,
                "executed_orders": [],
                "opportunities": [],
                "blocked_orders": [],
                "logs": [log],
            }

        if pauses["safety_paused"]:
            return _paused(
                "safety_pause",
                f"[SAFETY PAUSE] Not trading: {pauses['safety_reason']}. "
                f"Explicit clear_safety_pause() is required.",
            )

        if pauses["daily_loss_paused"]:
            return _paused(
                "daily_loss_pause",
                f"[DAILY LOSS PAUSE] Not trading: account paused for daily "
                f"loss ({day_key}).",
            )

        # ── Foto de riesgo del ciclo ──────────────────────────────────────────
        # Se levanta UNA vez y todo el gate se evalúa contra ella. Fail-closed:
        # si no podemos leer la cuenta, no se envía ninguna orden.
        try:
            account = service.get_account()
        except Exception as e:
            account = None
            scan_logs = [f"[RISK GATE] Could not read the account: {e}. Cycle placed no orders."]
            return {
                "success": False,
                "paused": False,
                "reason": "account_unavailable",
                "candidates_scanned": 0,
                "opportunities_count": 0,
                "orders_executed_count": 0,
                "blocked_orders_count": 0,
                "executed_orders": [],
                "opportunities": [],
                "blocked_orders": [],
                "logs": scan_logs,
            }

        try:
            open_positions = service.get_positions() or []
        except Exception:
            open_positions = []

        day_start_equity = self.risk_tracker.day_start_equity(day_key, account.get("equity"))

        in_flight: Set[str] = set()
        blocked_orders: List[Dict[str, Any]] = []

        if not candidate_tickers:
            # Construir universo de candidatos desde el Screener de Alpaca + Core Tech Institucional
            movers = service.get_top_movers(top=6)
            actives = service.get_most_active(top=6)
            
            universe = set(["NVDA", "AAPL", "MSFT", "AMZN", "TSLA", "AMD", "META", "GOOGL", "PLTR", "O"])
            for g in movers.get("gainers", []):
                # Filtrar penny stocks de centavos desde el screener
                if float(g.get("price", 0.0)) >= MIN_PRICE_USD:
                    universe.add(g["symbol"])
            for a in actives:
                universe.add(a["symbol"])
            candidate_list = list(universe)[:12]
        else:
            candidate_list = candidate_tickers

        scan_logs = []
        executed_orders = []
        opportunities_found = []

        scan_logs.append(f"[AUTOPILOT INIT] Starting autonomous scan over {len(candidate_list)} institutional candidates (Filter >= ${MIN_PRICE_USD:.2f})...")

        for ticker in candidate_list:
            try:
                analysis = self.analyze(ticker, service)
                if not analysis.get("available"):
                    scan_logs.append(f"⚠️ [{ticker}] Insufficient Alpaca data. Skipped.")
                    continue

                price = analysis["price"]

                # Institutional anti-penny-stock quality filter
                if price < MIN_PRICE_USD:
                    scan_logs.append(f"[{ticker}] Price (${price:.2f}) below ${MIN_PRICE_USD:.2f} (Anti-Penny-Stock Filter). Skipped for risk.")
                    continue

                signal = analysis["signal"]
                score = analysis["confidence_score"]
                strat = analysis["winning_strategy"]["name"]
                win_rate = analysis["winning_strategy"]["win_rate"]
                price = analysis["price"]
                plan = analysis["bracket_order_plan"]

                scan_logs.append(
                    f"[{ticker}] Price: ${price:.2f} | Tournament: '{strat}' ({win_rate}% Win Rate) -> Signal: {signal} (Score: {score}/100)"
                )

                # Autonomous execution criterion
                if signal in ("STRONG BUY", "ACCUMULATE") and score >= 65:
                    opportunities_found.append(analysis)

                    if auto_submit and len(executed_orders) < max_orders:
                        # Tamaño derivado del equity real de la cuenta. Antes era
                        # int(2000.0 / price), que ignoraba por completo el tamaño
                        # de la cuenta y podía apalancarla sin querer.
                        qty = size_order_qty(
                            account.get("equity"),
                            price,
                            limits=self.risk_limits,
                        )
                        if qty <= 0:
                            scan_logs.append(
                                f"[RISK GATE BLOCKED] {ticker}: sizing returned 0 shares (equity or price not valid)."
                            )
                            continue

                        verdict = evaluate_new_entry(
                            account=account,
                            open_positions=open_positions,
                            symbol=ticker,
                            qty=qty,
                            price=price,
                            day_start_equity=day_start_equity,
                            in_flight_symbols=in_flight,
                            limits=self.risk_limits,
                        )

                        if not verdict["allowed"]:
                            blocked_orders.append({"ticker": ticker, "reason": verdict["reason"]})
                            scan_logs.append(
                                f"[RISK GATE BLOCKED] {ticker}: {verdict['reason']} "
                                f"(notional=${verdict.get('notional')}, posiciones abiertas={len(open_positions)})"
                            )
                            if verdict["reason"] == "daily_loss_breach":
                                self.risk_tracker.pause(day_key, verdict["reason"])
                                scan_logs.append(
                                    f"[DAILY LOSS PAUSE] Account paused for the rest of {day_key}."
                                )
                                break
                            continue

                        # Idempotencia: Alpaca rechaza un client_order_id repetido,
                        # así que incluso si el gate fallara, el duplicado no entra.
                        client_order_id = build_client_order_id(
                            ticker, "buy", strat, now_epoch=time.time()
                        )

                        try:
                            order_res = service.place_bracket_order(
                                ticker=ticker,
                                qty=qty,
                                side="buy",
                                take_profit_price=plan["take_profit_price"],
                                stop_loss_price=plan["stop_loss_price"],
                                client_order_id=client_order_id
                            )
                            in_flight.add(ticker.upper())
                            executed_orders.append({
                                "ticker": ticker,
                                "qty": qty,
                                "entry_price": price,
                                "take_profit": plan["take_profit_price"],
                                "stop_loss": plan["stop_loss_price"],
                                "strategy": strat,
                                "order_id": order_res.get("id"),
                                "client_order_id": client_order_id
                            })
                            scan_logs.append(
                                f"[ORDER SENT TO ALPACA] {qty} shares of {ticker} at ${price:.2f} with TP at ${plan['take_profit_price']} (+3x ATR) and SL at ${plan['stop_loss_price']} (-2x ATR) [Order #{order_res.get('id')[:8]}...]"
                            )
                        except Exception as oe:
                            scan_logs.append(f"[{ticker}] Error submitting order to Alpaca: {oe}")
            except Exception as e:
                scan_logs.append(f"[{ticker}] Error during analysis: {e}")

        # 1. Supervise and adjust Trailing Stops on open positions before looking for new buys
        trailing_logs = self.supervise_trailing_stops(service)
        scan_logs.extend(trailing_logs)

        scan_logs.append(
            f"[AUTOPILOT COMPLETED] Scan finished. Opportunities: {len(opportunities_found)}, "
            f"Bracket Orders Executed on Alpaca: {len(executed_orders)}, "
            f"Blocked by Risk Gate: {len(blocked_orders)}"
        )

        return {
            "success": True,
            "paused": False,
            "candidates_scanned": len(candidate_list),
            "opportunities_count": len(opportunities_found),
            "orders_executed_count": len(executed_orders),
            "blocked_orders_count": len(blocked_orders),
            "executed_orders": executed_orders,
            "blocked_orders": blocked_orders,
            "opportunities": opportunities_found,
            "logs": scan_logs
        }

    def supervise_trailing_stops(self, service: Any) -> List[str]:
        """Supervisa las posiciones abiertas y ajusta Trailing Stops dinámicos por ATR para proteger ganancias."""
        logs = []
        try:
            positions = service.get_positions()
            if not positions:
                return logs

            for pos in positions:
                sym = pos.get("symbol")
                qty = float(pos.get("qty", 0))
                entry_price = float(pos.get("avg_entry_price", 0))
                curr_price = float(pos.get("current_price", 0))
                unrealized_pl = float(pos.get("unrealized_pl", 0))
                unrealized_plpc = float(pos.get("unrealized_plpc", 0))

                if qty <= 0 or entry_price <= 0:
                    continue

                # Fetch daily ATR to compute the Trailing Stop distance
                bars = service.get_stock_bars(sym, days=60)
                if bars.empty or len(bars) < 15:
                    continue

                tech = compute_technical_indicators(bars)
                atr_val = tech.get("volatility", {}).get("atr_20", 2.0)
                trail_distance = 2.0 * atr_val

                # Recent high watermark (last 10 bars)
                recent_high = float(bars.tail(10)["High"].max()) if "High" in bars.columns else curr_price
                dynamic_trailing_stop = round(max(entry_price * 0.95, recent_high - trail_distance), 2)

                # If the position is in profit (+3% or more) and the trailing stop is above entry (Profit Lock)
                if unrealized_plpc >= 2.5 and dynamic_trailing_stop > entry_price:
                    if curr_price <= dynamic_trailing_stop:
                        # Trigger sell to lock in the gain.
                        # El client_order_id es estable dentro de la misma hora, así
                        # que Alpaca rechaza el reenvío automático en el siguiente
                        # ciclo de 60s. Antes no había llave de idempotencia y este
                        # bloque reenviaba la venta en cada ciclo mientras el precio
                        # siguiera por debajo del stop.
                        try:
                            service.place_bracket_order(
                                ticker=sym,
                                qty=qty,
                                side="sell",
                                client_order_id=build_client_order_id(
                                    sym, "sell", "trailing",
                                    bucket_seconds=3600,
                                    now_epoch=time.time()
                                )
                            )
                            logs.append(
                                f"[TRAILING STOP PROFIT LOCK] {sym}: Profit secured at Trailing Stop ${dynamic_trailing_stop:.2f} (Entry: ${entry_price:.2f}). P&L: +${unrealized_pl:.2f} (+{unrealized_plpc:.2f}%)"
                            )
                        except Exception as e:
                            logs.append(f"⚠️ [{sym}] Error executing Trailing Stop: {e}")
                    else:
                        logs.append(
                            f"[{sym}] Trailing Stop Active: ${dynamic_trailing_stop:.2f} (ATR Distance: ${trail_distance:.2f}) protecting +{unrealized_plpc:.2f}% gain"
                        )
        except Exception as e:
            logs.append(f"⚠️ Error supervising Trailing Stops: {e}")

        return logs

