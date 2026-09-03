"""Capa LLM: transporte, presupuesto y caché.

Contrato que fijan estos tests:

- Sin clave, sin SDK o sin modelo: `LLMError` con motivo legible. Nunca una
  respuesta inventada.
- El tope diario y el tope por símbolo existen para que un ciclo autónomo no
  pueda salirse de la capa gratuita de la Gemini API.
- La caché evita repetir una llamada idéntica y **no** consume cupo.
- El SDK nuevo (`google-genai`) se intenta antes que el legacy; si falla, se
  cae al legacy; si los dos fallan, se prueba el siguiente modelo.

Ningún test toca la red: los dos SDKs se sustituyen por stubs.
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_provider
from llm_provider import DailyBudget, LLMError


# ── Helpers: stubs de los dos SDKs ────────────────────────────────────────────

def _install(monkeypatch, attr, obj):
    """Cuelga un módulo falso de `google.<attr>` y del paquete `google`."""
    google_mod = sys.modules.get("google")
    if not isinstance(google_mod, types.ModuleType):
        google_mod = types.ModuleType("google")
        monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setattr(google_mod, attr, obj, raising=False)
    monkeypatch.setitem(sys.modules, f"google.{attr}", obj)


def _install_genai(monkeypatch, handler):
    """Stub del SDK nuevo: `genai.Client(api_key).models.generate_content(...)`."""

    class _Resp:
        def __init__(self, text):
            self.text = text

    class _Models:
        def __init__(self, api_key):
            self._api_key = api_key

        def generate_content(self, model=None, contents=None):
            return _Resp(handler(contents, self._api_key, model))

    class _Client:
        def __init__(self, api_key=None):
            self.models = _Models(api_key)

    _install(monkeypatch, "genai", types.SimpleNamespace(Client=_Client))


def _install_genai_full(monkeypatch, handler):
    """Stub del SDK nuevo con las **dos** firmas: API key y Vertex (ADC).

    El handler recibe `(contents, mode, model)` donde `mode` es
    `("apikey", <clave>, None)` o `("vertex", <project>, <location>)`. Asi un
    solo test puede comprobar el orden de preferencia entre las dos rutas,
    que comparten modulo (`google.genai`) pero no credenciales.
    """

    class _Resp:
        def __init__(self, text):
            self.text = text

    class _Models:
        def __init__(self, mode):
            self._mode = mode

        def generate_content(self, model=None, contents=None):
            return _Resp(handler(contents, self._mode, model))

    class _Client:
        def __init__(self, api_key=None, vertexai=False, project=None, location=None):
            mode = ("vertex", project, location) if vertexai else ("apikey", api_key, None)
            self.models = _Models(mode)

    _install(monkeypatch, "genai", types.SimpleNamespace(Client=_Client))


def _install_legacy(monkeypatch, handler):
    """Stub del SDK legacy: `genai.configure(...)` + `GenerativeModel(...)`."""
    holder = {"key": None}

    class _Resp:
        def __init__(self, text):
            self.text = text

    class _Model:
        def __init__(self, name):
            self.name = name

        def generate_content(self, prompt):
            return _Resp(handler(prompt, holder["key"], self.name))

    mod = types.ModuleType("google.generativeai")
    mod.configure = lambda api_key=None, **kw: holder.__setitem__("key", api_key)
    mod.GenerativeModel = _Model
    _install(monkeypatch, "generativeai", mod)


def _budget(tmp_path, max_calls=100, max_per_key=100):
    return DailyBudget(path=str(tmp_path / "s.json"),
                       max_calls=max_calls, max_per_key=max_per_key)


@pytest.fixture(autouse=True)
def _limpio(monkeypatch, tmp_path):
    """Sin caché, sin contador persistido y sin variables que se filtren."""
    for var in ("HUARIZO_LLM_PROVIDER", "HUARIZO_LLM_MODEL",
                "HUARIZO_LLM_STATE_PATH", "HUARIZO_LLM_MAX_CALLS_PER_DAY",
                "HUARIZO_LLM_MAX_PER_KEY_PER_DAY",
                "HUARIZO_LLM_VERTEX", "HUARIZO_LLM_VERTEX_MODEL",
                "GOOGLE_VERTEX_PROJECT", "GOOGLE_VERTEX_LOCATION",
                "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    state = str(tmp_path / "llm_state.json")
    llm_provider.reset_for_tests(state)
    yield state
    llm_provider.reset_for_tests(state)


# ── Configuración ─────────────────────────────────────────────────────────────

def test_resolve_provider_por_defecto_es_auto():
    assert llm_provider.resolve_provider() == "auto"


@pytest.mark.parametrize("valor,esperado", [
    ("genai", "genai"), ("legacy", "legacy"), ("none", "none"),
    ("AUTO", "auto"), ("basura", "auto"),
])
def test_resolve_provider_normaliza(monkeypatch, valor, esperado):
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", valor)
    assert llm_provider.resolve_provider() == esperado


def test_resolve_provider_explicito_gana_al_env(monkeypatch):
    monkeypatch.setenv("HUARIZO_LLM_PROVIDER", "none")
    assert llm_provider.resolve_provider("legacy") == "legacy"


def test_resolve_models_por_defecto_incluye_un_relevo_estable():
    modelos = llm_provider.resolve_models()
    assert modelos[0] == "gemini-3.8-flash"
    assert len(modelos) >= 2  # si el nuevo no entra en la capa gratuita, hay relevo


def test_resolve_models_por_env(monkeypatch):
    monkeypatch.setenv("HUARIZO_LLM_MODEL", "gemini-2.5-flash, gemini-2.5-flash-lite")
    assert llm_provider.resolve_models() == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]


# ── Rechazos sin red ──────────────────────────────────────────────────────────

def test_sin_clave_no_intenta_nada(_limpio):
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="")
    assert ei.value.kind == "no_api_key"


def test_provider_none_desactiva_la_capa(_limpio):
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="k", provider="none")
    assert ei.value.kind == "llm_disabled"


# ── Orden de proveedores y modelos ────────────────────────────────────────────

def test_auto_prueba_el_sdk_nuevo_primero(monkeypatch, tmp_path, _limpio):
    vistos = []
    _install_genai(monkeypatch, lambda p, k, m: vistos.append(("genai", m)) or "NUEVO")
    _install_legacy(monkeypatch, lambda p, k, m: vistos.append(("legacy", m)) or "LEGACY")

    out = llm_provider.complete("prompt", api_key="k", models=["m1"],
                                provider="auto", budget=_budget(tmp_path))
    assert out["provider"] == "genai"
    assert vistos == [("genai", "m1")]  # el legacy ni se tocó


def test_auto_cae_al_legacy_si_el_nuevo_falla(monkeypatch, tmp_path, _limpio):
    def explota(p, k, m):
        raise RuntimeError("modelo no encontrado")

    _install_genai(monkeypatch, explota)
    _install_legacy(monkeypatch, lambda p, k, m: "LEGACY")

    out = llm_provider.complete("prompt", api_key="k", models=["m1"],
                                provider="auto", budget=_budget(tmp_path))
    assert out["provider"] == "legacy"
    assert out["text"] == "LEGACY"


def test_si_un_modelo_falla_se_prueba_el_siguiente(monkeypatch, tmp_path, _limpio):
    probados = []

    def handler(p, k, m):
        probados.append(m)
        if m == "gemini-3.8-flash":
            raise RuntimeError("aun no en la capa gratuita")
        return "OK"

    _install_genai(monkeypatch, handler)
    out = llm_provider.complete("prompt", api_key="k",
                                models=["gemini-3.8-flash", "gemini-2.5-flash"],
                                provider="genai", budget=_budget(tmp_path))
    assert probados == ["gemini-3.8-flash", "gemini-2.5-flash"]
    assert out["model"] == "gemini-2.5-flash"


def test_respuesta_vacia_no_cuenta_como_exito(monkeypatch, tmp_path, _limpio):
    _install_genai(monkeypatch, lambda p, k, m: "   ")
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="k", models=["m"],
                              provider="genai", budget=_budget(tmp_path))
    assert ei.value.kind == "llm_unavailable"
    assert "empty_response" in str(ei.value)


def test_si_fallan_todo_el_motivo_viaja_en_el_error(monkeypatch, tmp_path, _limpio):
    _install_genai(monkeypatch, lambda p, k, m: (_ for _ in ()).throw(RuntimeError("boom")))
    _install_legacy(monkeypatch, lambda p, k, m: (_ for _ in ()).throw(RuntimeError("boom2")))

    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="k", models=["m"],
                              provider="auto", budget=_budget(tmp_path))
    assert ei.value.kind == "llm_unavailable"
    assert "boom" in str(ei.value)  # el motivo, no un mensaje genérico


# ── Vertex AI ─────────────────────────────────────────────────────────────────
#
# Vertex es la ruta que hoy responde en esta maquina: se autentica con las
# credenciales de gcloud (ADC), sin clave. Dos consecuencias que fijan estos
# tests: no se le puede exigir un `GEMINI_API_KEY` que no usa, y no puede
# activarse por sorpresa.

def test_vertex_apagado_por_defecto():
    assert llm_provider.vertex_enabled() is False


@pytest.mark.parametrize("valor", ["1", "true", "TRUE", "yes", "on", "si", " sí "])
def test_vertex_enabled_acepta_las_escrituras_habituales(monkeypatch, valor):
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", valor)
    assert llm_provider.vertex_enabled() is True


def test_vertex_no_exige_api_key(monkeypatch, tmp_path, _limpio):
    """Vertex firma con ADC: exigirle una clave lo dejaria fuera sin motivo."""
    _install_genai_full(monkeypatch, lambda c, mode, m: "OK")
    out = llm_provider.complete("prompt", api_key="", models=["m"],
                                provider="vertex", budget=_budget(tmp_path))
    assert out["text"] == "OK"
    assert out["provider"] == "vertex"


def test_con_vertex_apagado_sin_clave_sigue_siendo_no_api_key(monkeypatch, tmp_path, _limpio):
    """La ruta nueva no puede 'arreglar' la falta de clave si no esta activa."""
    _install_genai_full(monkeypatch, lambda c, mode, m: "OK")
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="", models=["m"],
                              provider="auto", budget=_budget(tmp_path))
    assert ei.value.kind == "no_api_key"


def test_con_vertex_activo_sin_clave_se_usa_vertex_igual(monkeypatch, tmp_path, _limpio):
    """Vertex no necesita clave: la falta de clave no puede dejarlo fuera.

    Bug real de la primera implementacion: se exigia la clave con tal de que
    `genai`/`legacy` siguieran como relevo, asi que Vertex quedaba inutilizable
    justo en el caso para el que existe (no hay clave). Es el caso de esta
    maquina.
    """
    vistos = []
    _install_genai_full(monkeypatch,
                        lambda c, mode, m: vistos.append(mode[0]) or "VERTEX")
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")

    out = llm_provider.complete("prompt", api_key="", models=["m"],
                                provider="auto", budget=_budget(tmp_path))
    assert out["provider"] == "vertex"
    assert vistos == ["vertex"]  # las rutas con clave ni se intentan


def test_sin_clave_las_rutas_con_clave_no_se_intentan(monkeypatch, tmp_path, _limpio):
    """Sin clave, intentar `genai` daria un error confuso; mejor no intentarlo."""
    vistos = []

    def handler(contents, mode, model):
        vistos.append(mode[0])
        if mode[0] == "vertex":
            raise RuntimeError("ADC caducado")
        return "APIKEY"

    _install_genai_full(monkeypatch, handler)
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")

    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="", models=["m"],
                              provider="auto", budget=_budget(tmp_path))
    assert ei.value.kind == "llm_unavailable"
    assert vistos == ["vertex"]  # no se arrastro a rutas que no pueden funcionar


def test_auto_prefiere_vertex_y_deja_la_api_key_de_repuesto(monkeypatch, tmp_path, _limpio):
    vistos = []

    def handler(contents, mode, model):
        vistos.append(mode[0])
        return "VERTEX" if mode[0] == "vertex" else "APIKEY"

    _install_genai_full(monkeypatch, handler)
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")

    out = llm_provider.complete("prompt", api_key="k", models=["m"],
                                provider="auto", budget=_budget(tmp_path))
    assert out["text"] == "VERTEX"
    assert out["provider"] == "vertex"
    assert vistos == ["vertex"]  # la clave ni se toca


def test_si_vertex_falla_auto_cae_a_la_api_key(monkeypatch, tmp_path, _limpio):
    def handler(contents, mode, model):
        if mode[0] == "vertex":
            raise RuntimeError("sin credenciales ADC")
        return "APIKEY"

    _install_genai_full(monkeypatch, handler)
    monkeypatch.setenv("HUARIZO_LLM_VERTEX", "1")

    out = llm_provider.complete("prompt", api_key="k", models=["m"],
                                provider="auto", budget=_budget(tmp_path))
    assert out["provider"] == "genai"
    assert out["text"] == "APIKEY"


def test_vertex_usa_su_propio_catalogo_con_relevo(monkeypatch, tmp_path, _limpio):
    """El catalogo de Vertex va por otra puerta y siempre lleva relevo.

    El 3.8 se publico el mismo dia que esta implementacion. En Vertex responde
    SOLO en la region `global`; en `us-central1` da 404 aunque aparezca en el
    catalogo. El 2.5 queda detras como relevo.
    """
    usados = []
    _install_genai_full(monkeypatch, lambda c, mode, m: usados.append(m) or "OK")

    llm_provider.complete("prompt", api_key="", models=None,
                          provider="vertex", budget=_budget(tmp_path))
    assert usados == ["gemini-3.8-flash"]  # el primero que responde gana
    assert len(llm_provider.resolve_vertex_models()) >= 2  # y hay relevo


def test_la_region_por_defecto_de_vertex_es_global(tmp_path, _limpio):
    """`global` es la unica region que sirve 3.8 y 3.5, y tambien sirve 2.5."""
    assert llm_provider.vertex_location() == "global"


def test_vertex_manda_el_proyecto_y_la_location_configurados(monkeypatch, tmp_path, _limpio):
    recibido = {}

    def handler(contents, mode, model):
        recibido["project"] = mode[1]
        recibido["location"] = mode[2]
        return "OK"

    _install_genai_full(monkeypatch, handler)
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "mi-proyecto")
    monkeypatch.setenv("GOOGLE_VERTEX_LOCATION", "europe-west1")

    llm_provider.complete("prompt", api_key="", models=["m"],
                          provider="vertex", budget=_budget(tmp_path))
    assert recibido == {"project": "mi-proyecto", "location": "europe-west1"}


def test_si_un_modelo_de_vertex_falla_se_prueba_el_siguiente(monkeypatch, tmp_path, _limpio):
    """3.8 y 2.5 conviven en el catalogo; si 3.8 entra manana, no hay que tocar codigo."""
    probados = []

    def handler(contents, mode, model):
        probados.append(model)
        if model == "gemini-3.8-flash":
            raise RuntimeError("404 NOT_FOUND")
        return "OK"

    _install_genai_full(monkeypatch, handler)
    monkeypatch.setenv("HUARIZO_LLM_VERTEX_MODEL", "gemini-3.8-flash,gemini-2.5-flash")

    out = llm_provider.complete("prompt", api_key="", models=None,
                                provider="vertex", budget=_budget(tmp_path))
    assert probados == ["gemini-3.8-flash", "gemini-2.5-flash"]
    assert out["model"] == "gemini-2.5-flash"


def test_si_el_adc_falla_el_motivo_viaja_en_el_error(monkeypatch, tmp_path, _limpio):
    def handler(contents, mode, model):
        raise RuntimeError("DefaultCredentialsError: no hay credenciales")

    _install_genai_full(monkeypatch, handler)
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="", models=["m"],
                              provider="vertex", budget=_budget(tmp_path))
    assert ei.value.kind == "llm_unavailable"
    assert "DefaultCredentialsError" in str(ei.value)


def test_el_tope_diario_tambien_frena_a_vertex(monkeypatch, tmp_path, _limpio):
    """Vertex factura: el presupuesto es el freno, no la capa gratuita."""
    _install_genai_full(monkeypatch, lambda c, mode, m: "OK")
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="", models=["m"], provider="vertex",
                              budget=_budget(tmp_path, max_calls=0))
    assert ei.value.kind == "budget_exhausted"


# ── Presupuesto ───────────────────────────────────────────────────────────────

def test_tope_diario_bloquea_y_lo_declara(tmp_path, _limpio):
    b = _budget(tmp_path, max_calls=1, max_per_key=10)
    assert b.check() is None
    b.spend()
    motivo = b.check()
    assert motivo is not None and "daily_cap_reached" in motivo


def test_tope_por_simbolo_reparte_el_cupo(tmp_path, _limpio):
    """Sin este tope, el primer símbolo del universo se come todas las llamadas."""
    b = _budget(tmp_path, max_calls=100, max_per_key=1)
    assert b.check("SPY") is None
    b.spend("SPY")
    assert b.check("SPY") is not None
    assert b.check("QQQ") is None  # otro símbolo sigue disponible


def test_el_presupuesto_se_persiste_en_disco(tmp_path, _limpio):
    path = str(tmp_path / "s.json")
    DailyBudget(path=path, max_calls=10, max_per_key=10).spend("SPY")
    recargado = DailyBudget(path=path, max_calls=10, max_per_key=10)
    assert recargado.snapshot()["total"] == 1
    assert recargado.snapshot()["by_key"]["SPY"] == 1


def test_el_contador_se_reinicia_al_cambiar_el_dia(tmp_path, _limpio):
    path = str(tmp_path / "s.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"date": "2000-01-01", "total": 99, "by_key": {"SPY": 99}}, fh)
    b = DailyBudget(path=path, max_calls=10, max_per_key=10)
    assert b.snapshot()["total"] == 0
    assert b.check("SPY") is None


def test_si_el_disco_falla_el_tope_se_sigue_cumpliendo(tmp_path, _limpio):
    """El freno no puede desaparecer en silencio: es la guarda de la ruta que factura.

    `_save` se traga el error a propósito (el contador no tumba un ciclo), pero
    sin respaldo en memoria eso dejaba `check()` en `None` para siempre, es
    decir, llamadas ilimitadas justo cuando Vertex está cobrando.
    """
    # Directorio que no existe: `mkstemp` falla y nada se persiste.
    path = str(tmp_path / "no_existe" / "s.json")
    b = DailyBudget(path=path, max_calls=1, max_per_key=10)

    assert b.check() is None
    b.spend()
    assert b.snapshot()["total"] == 1
    assert b.check() is not None  # el tope se cumple aunque no se haya persistido


def test_el_respaldo_en_memoria_sobrevive_a_instancias_nuevas(tmp_path, _limpio):
    """`complete()` crea un `DailyBudget` por llamada: el respaldo debe ser del módulo."""
    path = str(tmp_path / "no_existe" / "s.json")
    DailyBudget(path=path, max_calls=2, max_per_key=10).spend("SPY")
    DailyBudget(path=path, max_calls=2, max_per_key=10).spend("SPY")

    tercera = DailyBudget(path=path, max_calls=2, max_per_key=10)
    assert tercera.snapshot()["total"] == 2
    assert tercera.check("SPY") is not None


def test_lo_persistido_y_lo_contado_en_memoria_no_se_suman(tmp_path, _limpio):
    """El total es el máximo de ambas fuentes, no la suma: si no, se duplica."""
    path = str(tmp_path / "s.json")
    DailyBudget(path=path, max_calls=10, max_per_key=10).spend("SPY")
    recargado = DailyBudget(path=path, max_calls=10, max_per_key=10)
    recargado.spend("QQQ")
    assert recargado.snapshot()["total"] == 2
    assert recargado.snapshot()["by_key"] == {"SPY": 1, "QQQ": 1}


def test_sin_cupo_el_llamador_recibe_el_motivo(monkeypatch, tmp_path, _limpio):
    _install_genai(monkeypatch, lambda p, k, m: "OK")
    with pytest.raises(LLMError) as ei:
        llm_provider.complete("prompt", api_key="k", models=["m"],
                              provider="genai",
                              budget=_budget(tmp_path, max_calls=0))
    assert ei.value.kind == "budget_exhausted"


# ── Caché ─────────────────────────────────────────────────────────────────────

def test_cache_evita_repetir_llamada_y_no_gasta_cupo(monkeypatch, tmp_path, _limpio):
    llamadas = {"n": 0}

    def handler(p, k, m):
        llamadas["n"] += 1
        return "OK"

    _install_genai(monkeypatch, handler)
    # Tope de 1: si la caché fallara, la segunda llamada quedaría bloqueada.
    b = _budget(tmp_path, max_calls=1, max_per_key=10)

    primera = llm_provider.complete("prompt", api_key="k", models=["m"],
                                    provider="genai", budget=b)
    segunda = llm_provider.complete("prompt", api_key="k", models=["m"],
                                    provider="genai", budget=b)
    assert llamadas["n"] == 1
    assert primera["provider"] == "genai"
    assert segunda["provider"] == "cache"
    assert b.snapshot()["total"] == 1  # el acierto de caché no consume cupo


def test_cache_se_puede_desactivar(monkeypatch, tmp_path, _limpio):
    llamadas = {"n": 0}

    def handler(p, k, m):
        llamadas["n"] += 1
        return "OK"

    _install_genai(monkeypatch, handler)
    b = _budget(tmp_path, max_calls=10, max_per_key=10)
    for _ in range(2):
        llm_provider.complete("prompt", api_key="k", models=["m"],
                              provider="genai", budget=b, use_cache=False)
    assert llamadas["n"] == 2


def test_cache_distingue_el_modelo(monkeypatch, tmp_path, _limpio):
    llamadas = []
    _install_genai(monkeypatch, lambda p, k, m: llamadas.append(m) or "OK")
    b = _budget(tmp_path, max_calls=10, max_per_key=10)
    llm_provider.complete("prompt", api_key="k", models=["m1"], provider="genai", budget=b)
    llm_provider.complete("prompt", api_key="k", models=["m2"], provider="genai", budget=b)
    assert llamadas == ["m1", "m2"]
