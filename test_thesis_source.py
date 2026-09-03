"""Transparencia de la tesis: nunca presentar un fallback como si fuera LLM.

Dos defectos reales en `HuarizoAgent._generate_llm_thesis`:

1. **Fallo silencioso.** `except Exception as e: pass` tiraba el error a la
   basura. Nadie —ni el UI ni un juez leyendo la respuesta de la API— podía
   distinguir una tesis escrita por Gemini de una plantilla determinista. Y
   como `google.generativeai` no está instalado en este entorno, el fallback se
   dispara por ImportError, o sea que en la práctica **todo** era plantilla.

2. **Números inventados.** El fallback hacía `winning_strat.get("win_rate", 65.0)`
   y `.get("profit_factor", 1.8)`. Desde la Task 6 el profit_factor legítimamente
   puede ser `None` (indeterminado: cero pérdidas), y `dict.get` **no** aplica el
   default cuando la clave existe con valor None. Resultado literal, reproducido
   corriendo `analyze()`: "Nonex Profit Factor." Y cuando faltan las claves, se
   afirmaba un 65.0% de win rate jamás medido.

Contrato que fijan estos tests:
- `thesis_source`: "llm" | "deterministic"
- `thesis_error`:  None en éxito | "no_gemini_key" | "<Tipo>: <mensaje>"
- Dato desconocido -> NO se afirma ningún número.
"""
import inspect
import json
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_engine import HuarizoAgent
import llm_provider


# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent(api_key=None):
    """Agente sin clave de Gemini: el fallback determinista es el camino base."""
    return HuarizoAgent(gemini_api_key=api_key, risk_state_path=None)


def _thesis(agent=None, **over):
    kwargs = dict(
        ticker="TEST",
        price=100.0,
        trend="BULLISH",
        rsi=55.0,
        stoch={"k": 60.0, "d": 55.0, "cross": "BULLISH_CROSS", "zone": "NEUTRAL (20-80)"},
        pattern=None,
        news=[],
        winning_strat={"name": "Mean Reversion Sniper", "win_rate": 62.5, "profit_factor": 1.4},
        signal="STRONG BUY",
    )
    kwargs.update(over)
    return (agent or _agent())._generate_llm_thesis(**kwargs)


def _text(out):
    return " ".join([out.get("headline", "")] + list(out.get("thesis_points", [])))


@pytest.fixture(autouse=True)
def _llm_isolation(monkeypatch, tmp_path):
    """Aísla la capa LLM entre tests. Sin esto pasan dos cosas malas:

    1. La caché de `llm_provider` devuelve la respuesta de un test anterior, así
       que los tests que inspeccionan el prompt no ven ninguna llamada.
    2. Con `google-genai` instalado, el proveedor "auto" intentaría el SDK nuevo
       con una clave falsa: una llamada de red real dentro de un unit test.

    Se fija `legacy` para que los stubs sean el camino que se ejecuta, y el
    contador diario vive en `tmp_path` para no tocar `llm_state.json` real.
    """
    state = str(tmp_path / "llm_state.json")
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "legacy")
    monkeypatch.setenv("HUARIZO_LLM_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("HUARIZO_LLM_STATE_PATH", state)
    llm_provider.reset_for_tests(state)
    yield
    llm_provider.reset_for_tests(state)


def _install_genai(monkeypatch, model_cls):
    """Inyecta un stub de `google.generativeai` en `sys.modules`.

    Monkeypatchear `agent_engine.genai = None` sería un NO-OP: el método hace un
    `import google.generativeai as genai` LOCAL dentro del try, que rescata el
    módulo de `sys.modules` y sombrea cualquier atributo de módulo. Además, como
    `google.generativeai` no está instalado aquí, ese test pasaría por ImportError
    y no probaría que el fallo quedó registrado. Hay que ejecutar el camino real.
    """
    mod = types.ModuleType("google.generativeai")
    mod.configure = lambda api_key=None, **kw: None
    mod.GenerativeModel = model_cls

    parent = sys.modules.get("google")
    if parent is None:
        parent = types.ModuleType("google")
        monkeypatch.setitem(sys.modules, "google", parent)
    monkeypatch.setitem(sys.modules, "google.generativeai", mod)
    # raising=False: `google` es un namespace package y no tiene el atributo.
    monkeypatch.setattr(parent, "generativeai", mod, raising=False)
    return mod


def _install_vertex(monkeypatch, model_cls):
    """Inyecta un stub de `google.genai` en modo Vertex (ADC, sin clave).

    Devuelve un dict `seen` con cómo se construyó el cliente y con qué modelo se
    llamó. No es decorado de test: son las dos afirmaciones que un 404 silencioso
    no deja ver —que se pidió Vertex (`vertexai=True`) y en qué región—, y la
    región es exactamente lo que decide si `gemini-3.8-flash` existe o no.
    """
    seen = {}

    class _Models:
        def generate_content(self, model=None, contents=None):
            seen["model"] = model
            seen["prompt"] = contents
            return model_cls(model).generate_content(contents)

    class _Client:
        def __init__(self, **kwargs):
            seen["client"] = dict(kwargs)
            self.models = _Models()

    mod = types.ModuleType("google.genai")
    mod.Client = _Client

    parent = sys.modules.get("google")
    if parent is None:
        parent = types.ModuleType("google")
        monkeypatch.setitem(sys.modules, "google", parent)
    monkeypatch.setitem(sys.modules, "google.genai", mod)
    # raising=False: si `google-genai` está instalado, `google` es un namespace
    # package real y no trae el atributo.
    monkeypatch.setattr(parent, "genai", mod, raising=False)
    return seen


class _Resp:
    def __init__(self, text):
        self.text = text


class _OkModel:
    """Modelo stub que devuelve JSON válido: sin red y sin clave real."""

    payload = {
        "headline": "Gemini wrote this verdict",
        "thesis_points": ["llm point 1", "llm point 2", "llm point 3"],
        "risk_level": "LOW",
    }

    def __init__(self, name):
        self.name = name

    def generate_content(self, prompt):
        return _Resp("```json" + json.dumps(self.payload) + "```")


class _FailingModel:
    """Modelo stub que revienta en la llamada, no en el import."""

    def __init__(self, name):
        self.name = name

    def generate_content(self, prompt):
        raise RuntimeError("429 quota exceeded for model gemini-1.5-flash")


# ── 1. Sin clave de Gemini ────────────────────────────────────────────────────

def test_no_api_key_reports_deterministic_source():
    out = _thesis(_agent(api_key=""))
    assert out["thesis_source"] == "deterministic"
    assert out["thesis_error"] == "no_gemini_key"


def test_deterministic_thesis_still_has_full_content():
    out = _thesis(_agent(api_key=""))
    assert out["headline"]
    assert len(out["thesis_points"]) == 3
    assert out["risk_level"] in ("LOW", "MEDIUM", "HIGH")


# ── 1 bis. Vertex: quién decide si hay LLM ────────────────────────────────────
#
# Contexto: el gate era `if self.gemini_api_key:`. Con una sola ruta esa
# condición era correcta; con Vertex deja fuera justo el caso que cubre Vertex
# (no hay clave, firma con Application Default Credentials). Con `GEMINI_API_KEY`
# vacío y Vertex operativo, el bot seguía emitiendo tesis de plantilla y
# reportando `no_gemini_key` — un error que además acusaba al síntoma equivocado.


def test_con_vertex_activo_y_sin_api_key_la_tesis_viene_del_llm(monkeypatch):
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")
    seen = _install_vertex(monkeypatch, _OkModel)

    out = _thesis(_agent(api_key=""))

    assert out["thesis_source"] == "llm"
    assert out["thesis_error"] is None
    assert out["llm_provider"] == "vertex"
    assert out["headline"] == "Gemini wrote this verdict"
    # Firma con ADC: ni se le pasa clave ni se la pide.
    assert seen["client"]["vertexai"] is True
    assert "api_key" not in seen["client"]


def test_vertex_arranca_por_el_modelo_de_su_propio_catalogo(monkeypatch):
    """La pregunta del usuario: ¿se puede usar 3.8? Respuesta observable.

    Vertex no comparte catálogo con la Gemini API. Si se le pasara la lista de
    `HUARIZO_LLM_MODEL` (2.5), el 3.8 quedaría fuera del todo y el modelo nunca
    se sabría: el bot funcionaría "bien" con el modelo viejo.
    """
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")
    seen = _install_vertex(monkeypatch, _OkModel)

    out = _thesis(_agent(api_key=""))

    assert out["llm_model"] == "gemini-3.8-flash"
    assert seen["model"] == "gemini-3.8-flash"


def test_la_region_de_vertex_es_la_que_hace_existir_al_modelo(monkeypatch):
    """`gemini-3.8-flash` da 404 en us-central1/us-east4/europe-west1 y existe en
    `global` (medido en vivo el 2026-09-02). Una región mal puesta se ve como
    "el modelo no está disponible", así que se afirma aquí y no solo en el .env.
    """
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")
    monkeypatch.setenv("GOOGLE_VERTEX_LOCATION", "global")
    seen = _install_vertex(monkeypatch, _OkModel)

    _thesis(_agent(api_key=""))

    assert seen["client"]["location"] == "global"
    assert seen["client"]["project"]


def test_auto_con_vertex_activo_lo_prefiere_aunque_haya_clave(monkeypatch):
    """Vertex va primero: la capa gratuita de AI Studio es la que se agota."""
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "auto")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")
    seen = _install_vertex(monkeypatch, _OkModel)

    out = _thesis(_agent(api_key="fake-key-123"))

    assert out["thesis_source"] == "llm"
    assert out["llm_provider"] == "vertex"
    assert seen["client"]["vertexai"] is True


def test_si_vertex_falla_el_motivo_no_se_disfraza_de_falta_de_clave(monkeypatch):
    """ADC caducado o cuota agotada NO es "no_gemini_key": ese token significaría
    "configura una clave", y arreglaría el problema equivocado."""
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")
    _install_vertex(monkeypatch, _FailingModel)

    out = _thesis(_agent(api_key=""))

    assert out["thesis_source"] == "deterministic"
    assert out["thesis_error"] != "no_gemini_key"
    assert "quota exceeded" in out["thesis_error"]


def test_sin_clave_y_sin_vertex_el_token_sigue_siendo_no_gemini_key(monkeypatch):
    """El gate cambió de sitio, no de significado: la UI compara este literal."""
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "auto")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "0")

    out = _thesis(_agent(api_key=""))

    assert out["thesis_source"] == "deterministic"
    assert out["thesis_error"] == "no_gemini_key"


# ── 2. Un fallo real del LLM queda registrado, no se silencia ─────────────────

def test_llm_failure_is_recorded_not_swallowed(monkeypatch):
    """El bug: `except Exception: pass` borraba el motivo del fallback."""
    _install_genai(monkeypatch, _FailingModel)
    out = _thesis(_agent(api_key="fake-key-123"))

    assert out["thesis_source"] == "deterministic"
    assert out["thesis_error"] is not None
    assert out["thesis_error"] != "no_gemini_key"
    assert "RuntimeError" in out["thesis_error"]
    assert "quota exceeded" in out["thesis_error"]
    # Y la tesis sigue siendo utilizable.
    assert len(out["thesis_points"]) == 3


def test_llm_failure_does_not_fall_back_to_fabricated_numbers(monkeypatch):
    """Sin datos de torneo, caer al fallback no autoriza a inventar métricas."""
    _install_genai(monkeypatch, _FailingModel)
    out = _thesis(_agent(api_key="fake-key-123"), winning_strat={})
    assert "65.0" not in _text(out)
    assert "1.8" not in _text(out)


# ── 3. Camino LLM exitoso ─────────────────────────────────────────────────────

def test_successful_llm_path_is_labelled_llm(monkeypatch):
    _install_genai(monkeypatch, _OkModel)
    out = _thesis(_agent(api_key="fake-key-123"))

    assert out["thesis_source"] == "llm"
    assert out["thesis_error"] is None
    # El contenido del LLM sobrevive: no se reemplazó por la plantilla.
    assert out["headline"] == "Gemini wrote this verdict"
    assert out["thesis_points"] == ["llm point 1", "llm point 2", "llm point 3"]


@pytest.mark.parametrize("winning_strat,expected_fragment", [
    ({"name": "S", "win_rate": None, "profit_factor": None},
     "no validated backtest metrics were produced for this asset"),
    ({"name": "S", "win_rate": 62.5, "profit_factor": None},
     "Profit Factor: not measured (undefined: no losing trades)"),
])
def test_llm_prompt_never_leaks_raw_none_metrics(monkeypatch, winning_strat, expected_fragment):
    """El prompt interpolaba `winning_strat['profit_factor']` en crudo.

    Con la Task 6 ese valor puede ser None, así que el modelo recibía
    "Profit Factor: Nonex" y podía devolverlo como si fuera una medición real.
    """
    seen = {}

    class _CapturingModel:
        def __init__(self, name):
            self.name = name

        def generate_content(self, prompt):
            seen["prompt"] = prompt
            return _Resp(json.dumps({
                "headline": "ok",
                "thesis_points": ["a", "b", "c"],
                "risk_level": "MEDIUM",
            }))

    _install_genai(monkeypatch, _CapturingModel)
    _thesis(_agent(api_key="fake-key-123"), winning_strat=winning_strat)

    prompt = seen["prompt"]
    assert "Nonex" not in prompt
    assert "None%" not in prompt
    # Y se declara explícitamente la ausencia de medición, en vez de omitirla.
    assert expected_fragment in prompt


def test_llm_prompt_reports_measured_metrics_when_present(monkeypatch):
    """Con métricas reales el prompt debe seguir mandándolas: no regresión."""
    seen = {}

    class _CapturingModel:
        def __init__(self, name):
            self.name = name

        def generate_content(self, prompt):
            seen["prompt"] = prompt
            return _Resp(json.dumps({
                "headline": "ok",
                "thesis_points": ["a", "b", "c"],
                "risk_level": "MEDIUM",
            }))

    _install_genai(monkeypatch, _CapturingModel)
    _thesis(_agent(api_key="fake-key-123"))

    prompt = seen["prompt"]
    assert "62.5%" in prompt
    assert "1.4x" in prompt


# ── 4. profit_factor=None no se renderiza como "Nonex" ────────────────────────

def test_none_profit_factor_is_not_rendered_as_nonex():
    """Bug reproducido en vivo: '...and Nonex Profit Factor.'"""
    out = _thesis(winning_strat={"name": "Mean Reversion Sniper",
                                 "win_rate": 62.5, "profit_factor": None})
    text = _text(out)
    assert "Nonex" not in text
    assert "None" not in text
    assert "62.5" in text, "el win rate conocido sí debe reportarse"


def test_undefined_profit_factor_is_labelled_nd():
    out = _thesis(winning_strat={"name": "Mean Reversion Sniper",
                                 "win_rate": 62.5, "profit_factor": None})
    assert "n/d" in _text(out)


# ── 5. Sin métricas de torneo no se afirma ningún número ─────────────────────

@pytest.mark.parametrize("winning_strat", [
    {},
    {"name": "Quantitative Strategy"},
    {"name": "Mean Reversion Sniper", "win_rate": None, "profit_factor": None},
])
def test_missing_metrics_are_never_invented(winning_strat):
    out = _thesis(winning_strat=winning_strat)
    text = _text(out)
    assert "65.0" not in text, "win rate fabricado por el default de dict.get"
    assert "1.8" not in text, "profit factor fabricado por el default de dict.get"
    assert "n/d" in text or "no validated" in text.lower()


def test_known_metrics_are_still_reported():
    out = _thesis(winning_strat={"name": "Mean Reversion Sniper",
                                 "win_rate": 62.5, "profit_factor": 1.4})
    text = _text(out)
    assert "62.5" in text
    assert "1.4" in text


# ── 6. risk_level con win rate desconocido ────────────────────────────────────

def test_unknown_win_rate_never_downgrades_risk_to_low():
    """`win_rate >= 70` con default 65.0 significaba 'LOW' imposible.

    Desconocido debe ser conservador: sin win rate validado no hay evidencia
    para prometer riesgo LOW, así que se queda en MEDIUM.
    """
    out = _thesis(signal="STRONG BUY", winning_strat={"name": "S", "win_rate": None})
    assert out["risk_level"] == "MEDIUM"


def test_high_win_rate_still_earns_low_risk():
    out = _thesis(signal="STRONG BUY",
                  winning_strat={"name": "S", "win_rate": 80.0, "profit_factor": 2.0})
    assert out["risk_level"] == "LOW"


# ── 7. `dividends` ya no es parámetro ─────────────────────────────────────────

def test_dividends_parameter_removed():
    params = inspect.signature(HuarizoAgent._generate_llm_thesis).parameters
    assert "dividends" not in params


# ── 8. Propagación a analyze() ────────────────────────────────────────────────

def _make_bars(n=250, seed=7):
    rng = np.random.default_rng(seed)
    close = np.maximum(100 + np.cumsum(rng.normal(0.3, 1.0, n)), 5.0)
    df = pd.DataFrame({
        "Open": close + rng.uniform(-0.4, 0.4, n),
        "High": close + rng.uniform(0.2, 1.0, n),
        "Low": close - rng.uniform(0.2, 1.0, n),
        "Close": close,
        "Volume": rng.integers(1e6, 5e6, n).astype(float),
    })
    df["t"] = pd.bdate_range("2025-01-01", periods=n)
    return df


class _StubService:
    """Servicio Alpaca falso: sin red, sin credenciales."""

    dividends = [{"amount": 0.5, "ex_date": "2025-06-01"}]

    def get_stock_bars(self, ticker, days=250):
        return _make_bars()

    def get_stock_snapshot(self, ticker):
        return {"change_pct": 1.2}

    def get_stock_news(self, ticker, limit=4):
        return [{"headline": "Synthetic headline for testing purposes only"}]

    def get_dividends(self, ticker, days=365):
        return list(self.dividends)


def test_analyze_exposes_thesis_source_and_error():
    """Sin esto la transparencia es invisible en la respuesta de la API."""
    out = _agent(api_key="").analyze("TEST", _StubService())

    assert out["available"] is True
    assert out["thesis_source"] == "deterministic"
    assert out["thesis_error"] == "no_gemini_key"


def test_analyze_llega_al_llm_por_vertex_sin_api_key(monkeypatch):
    """Mismo escenario que `test_analyze_exposes_thesis_source_and_error`, pero
    con Vertex activo: la ÚNICA diferencia es el gate de `agent_engine`, y tiene
    que cambiar el resultado. Este test es el que habría fallado con el bug.
    """
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")
    _install_vertex(monkeypatch, _OkModel)

    out = _agent(api_key="").analyze("TEST", _StubService())

    assert out["thesis_source"] == "llm"
    assert out["llm_provider"] == "vertex"
    assert out["headline"] == "Gemini wrote this verdict"


def test_analyze_does_not_ship_the_nonex_bug():
    out = _agent(api_key="").analyze("TEST", _StubService())
    assert "Nonex" not in " ".join(out["thesis_points"])


def test_analyze_keeps_dividends_after_signature_change():
    """Quitar el parámetro no puede sacar los dividendos de la respuesta."""
    out = _agent(api_key="").analyze("TEST", _StubService())
    assert "dividends" in out
    assert out["dividends"]


def test_analyze_without_tournament_winner_invents_no_metrics(monkeypatch):
    """El fallback de `analyze()` también inventaba 65.0 / 1.8."""
    agent = _agent(api_key="")
    monkeypatch.setattr(agent, "run_strategy_tournament",
                        lambda df: {"available": False, "tournament": [], "winner": None})
    out = agent.analyze("TEST", _StubService())

    text = " ".join(out["thesis_points"])
    assert "65.0" not in text
    assert "1.8" not in text
    assert "Nonex" not in text
