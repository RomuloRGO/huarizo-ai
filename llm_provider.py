"""Capa de transporte LLM: un punto de entrada, dos SDKs, cero silencio.

Por que existe este modulo
--------------------------
`agent_engine._generate_llm_thesis` llamaba directamente a
`google.generativeai` (SDK legacy) con el modelo `gemini-1.5-flash`. En
septiembre de 2026 las tres patas de esa llamada estan rotas:

1. `gemini-1.5-flash` ya no aparece en el listado oficial de modelos de la
   Gemini API (ai.google.dev/gemini-api/docs/deprecations): fue retirado.
2. `google-generativeai` es el SDK legacy. Google recomienda migrar a
   `google-genai`, que ya esta en disponibilidad general.
3. No habia clave configurada (`GEMINI_API_KEY` vacio), asi que el camino caia
   por ImportError y, en la practica, **toda** tesis era plantilla.

Este modulo aisla el transporte: el agente no sabe — ni le importa — cual de
los dos SDKs respondio, y el modelo se cambia por configuracion sin tocar
`agent_engine`.

Tres rutas, no dos
------------------
- **Vertex AI** (`vertex`) — autenticado con las credenciales de `gcloud` (ADC),
  sin clave. Es la que responde hoy en esta maquina. Va contra un proyecto con
  facturacion habilitada: cada llamada consume credito.
- **Gemini API** (`genai`) — SDK nuevo, con clave de AI Studio. Tiene capa
  gratuita, con topes por minuto y por dia.
- **Legacy** (`legacy`) — SDK antiguo, conservado solo por compatibilidad.

`auto` prueba en ese orden y se queda con el primero que contesta.

Lo que aqui NO se negocia
-------------------------
- **Nunca fallar en silencio.** Sin clave, sin SDK, sin modelo o sin cupo, se
  levanta `LLMError` con un motivo legible. El llamador decide; aqui no se
  inventa una respuesta.
- **Presupuesto duro.** La capa gratuita de la Gemini API tiene topes por
  minuto y por dia. Sin tope, el ciclo autonomo (8 simbolos cada 5 minutos) son
  ~624 llamadas diarias. Con tope, el agente cae al fallback determinista
  **y lo declara**, en vez de quedarse sin cupo a mitad del concurso.

Aviso que conviene no olvidar
-----------------------------
Una suscripcion **Google AI Pro** da mas cuota en la interfaz web de AI Studio,
NO en la API. Documentacion oficial: *"Google AI plan benefits for developer
usage apply only within the Google AI Studio web interface. Direct use of the
Gemini API (such as using API keys or external applications) is billed and
managed separately."* Las llamadas desde codigo van por clave de API, con su
propia capa gratuita y su propia facturacion.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))

#: Junto al modulo, igual que `risk_state.json` y `execution_ledger.json`.
DEFAULT_STATE_PATH = os.path.join(_HERE, "llm_state.json")

#: Modelos por defecto, en orden de preferencia.
#: `gemini-3.8-flash` se publico el 2026-09-02 — el mismo dia que esta
#: implementacion — asi que puede tardar en entrar a la capa gratuita.
#: `gemini-2.5-flash` es el relevo estable: sigue en GA sin fecha de apagado.
DEFAULT_MODELS = "gemini-3.8-flash,gemini-2.5-flash"

#: Modelos por defecto cuando se va por **Vertex AI**.
#: Verificado en vivo el 2026-09-02 contra el proyecto `vertex-ai-502118`.
#: `gemini-2.5-flash` va detras como relevo porque el 3.8 se publico ese mismo
#: dia: si algo tarda en entrar en la region, el 2.5 sostiene la tesis.
DEFAULT_VERTEX_MODELS = "gemini-3.8-flash,gemini-2.5-flash"

PROVIDERS = ("auto", "genai", "legacy", "vertex", "none")

#: Flags que activan Vertex. Se aceptan varias escrituras porque la variable
#: llega de un `.env` escrito a mano.
_VERTEX_ON = ("1", "true", "yes", "on", "si", "sí", "s")


class LLMError(Exception):
    """Fallo de la capa LLM con motivo legible y clasificable.

    `kind` es una etiqueta corta pensada para viajar hasta la respuesta de la
    API (`thesis_error`); `message` es el detalle humano.
    """

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message


# ── Configuracion ─────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def resolve_provider(explicit: Optional[str] = None) -> str:
    """Proveedor efectivo: explicito > `HUARIZO_LLM_PROVIDER` > "auto".

    - `auto`   prueba el SDK nuevo y luego el legacy
    - `genai`  solo `google-genai`
    - `legacy` solo `google-generativeai`
    - `none`   desactiva la capa sin tocar codigo
    """
    raw = (explicit or os.getenv("HUARIZO_LLM_PROVIDER") or "auto").strip().lower()
    return raw if raw in PROVIDERS else "auto"


def resolve_models(explicit: Optional[str] = None) -> List[str]:
    """Lista de modelos a intentar, en orden."""
    raw = explicit or os.getenv("HUARIZO_LLM_MODEL") or DEFAULT_MODELS
    out = [m.strip() for m in str(raw).split(",") if m.strip()]
    return out or [DEFAULT_MODELS.split(",")[0]]


# ── Configuracion Vertex AI ──────────────────────────────────────────────────
#
# Vertex no usa API key: se autentica con **Application Default Credentials**
# (ADC). En esta maquina las credenciales ya existen en
# `%APPDATA%\gcloud\application_default_credentials.json` (cuenta
# romulogranadosg@gmail.com, proyecto vertex-ai-502118), asi que basta con
# activar la ruta — no hay ningun secreto que copiar ni pegar en el `.env`.
#
# AVISO: a diferencia de la capa gratuita de la Gemini API, Vertex va contra un
# proyecto con **facturacion habilitada**. Cada llamada consume credito o
# cargo real. El tope diario de abajo es el freno.

def vertex_enabled(explicit: Optional[str] = None) -> bool:
    """Vertex se usa solo si se activa: nunca sustituye la clave por sorpresa."""
    raw = explicit if explicit is not None else os.getenv("HUARIZO_LLM_VERTEX", "")
    return str(raw).strip().lower() in _VERTEX_ON


def vertex_project() -> str:
    raw = (os.getenv("GOOGLE_VERTEX_PROJECT")
           or os.getenv("GOOGLE_CLOUD_PROJECT")
           or "vertex-ai-502118")
    return str(raw).strip()


def vertex_location() -> str:
    """Region de Vertex. Por defecto `global`, y la razon importa.

    Medido en vivo el 2026-09-02 con el proyecto `vertex-ai-502118`:

    | modelo | us-central1 | us-east4 | europe-west1 | **global** |
    |---|---|---|---|---|
    | `gemini-3.8-flash` | 404 | 404 | 404 | **OK** |
    | `gemini-3.5-flash` | 404 | 404 | 404 | **OK** |
    | `gemini-2.5-flash` | OK | OK | OK | **OK** |

    Los modelos nuevos llegan primero al endpoint `global`. Con `us-central1`
    el 3.8 da 404 aunque figure en el catalogo del proyecto, y la tentacion es
    concluir que "no hay acceso" — conclusion falsa. `global` sirve los tres,
    asi que es el valor por defecto.
    """
    return str(os.getenv("GOOGLE_VERTEX_LOCATION") or "global").strip()


def resolve_vertex_models(explicit: Optional[str] = None) -> List[str]:
    raw = explicit or os.getenv("HUARIZO_LLM_VERTEX_MODEL") or DEFAULT_VERTEX_MODELS
    out = [m.strip() for m in str(raw).split(",") if m.strip()]
    return out or [DEFAULT_VERTEX_MODELS]


def _models_for(name: str, explicit: Optional[List[str]] = None) -> List[str]:
    """Modelos a probar para un proveedor concreto.

    Vertex tiene su propio catalogo (ver `DEFAULT_VERTEX_MODELS`), asi que la
    lista no puede ser la misma para todos. Si el llamador paso `models`
    explicitamente, manda eso.
    """
    if explicit is not None:
        return explicit
    return resolve_vertex_models() if name == "vertex" else resolve_models()


def _provider_order(prov: str) -> List[str]:
    """Orden de proveedores a intentar.

    `auto` con Vertex activado lo pone **primero**, no como relevo: ahi no hace
    falta clave y no se depende de la capa gratuita de AI Studio, que es la que
    se agota primero.
    """
    if prov != "auto":
        return [prov]
    return (["vertex"] if vertex_enabled() else []) + ["genai", "legacy"]


def available(api_key: Optional[str] = None) -> bool:
    """True si hay ALGUNA ruta de LLM usable, sin gastar una llamada.

    Durante meses el llamador respondia esta pregunta con
    `if self.gemini_api_key:`, y esa respuesta era correcta mientras existio una
    sola ruta. Con Vertex en pie es falsa: Vertex firma con ADC y no necesita
    clave, asi que "no hay clave" ya NO implica "no hay LLM".

    Preguntar aqui en vez de en `agent_engine` evita que las dos capas tengan
    que saber lo mismo. Si devuelve True, `complete()` todavia puede fallar
    (ADC caducado, cuota, red) y eso viaja en `LLMError`.
    """
    prov = resolve_provider()
    if prov == "none":
        return False
    order = [n for n in _provider_order(prov) if n in _DISPATCH]
    if any(n == "vertex" for n in order):
        return True
    return bool((api_key or "").strip())


# ── Presupuesto diario ────────────────────────────────────────────────────────

def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path: str, data: Dict[str, Any]) -> None:
    """Escritura atomica: nadie debe leer un JSON a medio escribir."""
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".llm_state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        # El contador es una proteccion, no un dato critico: si no se puede
        # persistir, se sigue operando con el valor en memoria.
        pass


#: Respaldo en memoria del contador, indexado por ruta de estado.
#: Existe porque `_save` se traga el error a proposito (el contador no es un
#: dato critico y no debe tumbar un ciclo). Pero eso tenia un agujero: si el
#: disco falla, `snapshot()` volvia a leer 0 y `check()` devolvia `None` para
#: siempre — es decir, **el freno desaparecia en silencio**. En la capa
#: gratuita eso solo gastaba el cupo; en Vertex, que factura, era un cheque en
#: blanco. El respaldo es por ruta y a nivel de modulo porque `complete()`
#: crea un `DailyBudget` nuevo en cada llamada cuando no le pasan uno.
_BUDGET_MEM: Dict[str, Dict[str, Any]] = {}


class DailyBudget:
    """Tope diario de llamadas, con subcuenta por simbolo.

    Dos topes porque resuelven dos problemas distintos:

    - **Global** (`max_calls`): garantiza que una tormenta de ciclos no puede
      acercarse al limite diario de la capa gratuita.
    - **Por simbolo** (`max_per_key`): reparte el cupo. Sin el, el primer
      simbolo del universo se come todas las llamadas del dia y los demas
      nunca ven una tesis de LLM.

    El contador se lee como el **maximo** de lo que hay en disco y lo que hay
    en memoria, asi que sobrevive a un reinicio sin dejar de cumplirse cuando
    el disco no responde. Se reinicia solo al cambiar la fecha, igual que
    `DailyLossTracker`.
    """

    def __init__(self, path: Optional[str] = None,
                 max_calls: Optional[int] = None,
                 max_per_key: Optional[int] = None):
        self.path = path or os.getenv("HUARIZO_LLM_STATE_PATH", DEFAULT_STATE_PATH)
        self.max_calls = _env_int("HUARIZO_LLM_MAX_CALLS_PER_DAY", 200) \
            if max_calls is None else int(max_calls)
        self.max_per_key = _env_int("HUARIZO_LLM_MAX_PER_KEY_PER_DAY", 2) \
            if max_per_key is None else int(max_per_key)

    def _today(self) -> str:
        return date.today().isoformat()

    def _fresh(self, today: str) -> Dict[str, Any]:
        return {"date": today, "total": 0, "by_key": {}}

    def snapshot(self) -> Dict[str, Any]:
        today = self._today()

        data = _load(self.path)
        if data.get("date") != today:
            # Dia nuevo: el contador arranca de cero sin borrar nada a mano.
            data = self._fresh(today)
        data.setdefault("total", 0)
        data.setdefault("by_key", {})

        mem = _BUDGET_MEM.get(self.path)
        if not mem or mem.get("date") != today:
            mem = self._fresh(today)
            _BUDGET_MEM[self.path] = mem

        disk_by_key = data.get("by_key") or {}
        mem_by_key = mem.get("by_key") or {}
        by_key = {
            k: max(int(disk_by_key.get(k, 0)), int(mem_by_key.get(k, 0)))
            for k in set(disk_by_key) | set(mem_by_key)
        }
        return {
            "date": today,
            "total": max(int(data.get("total", 0)), int(mem.get("total", 0))),
            "by_key": by_key,
        }

    def check(self, key: Optional[str] = None) -> Optional[str]:
        """`None` si se puede llamar; si no, el motivo (para `LLMError`)."""
        data = self.snapshot()
        total = int(data.get("total", 0))
        if self.max_calls >= 0 and total >= self.max_calls:
            return f"daily_cap_reached {total}/{self.max_calls}"
        if key:
            by_key = data.get("by_key") or {}
            used = int(by_key.get(str(key), 0))
            if self.max_per_key >= 0 and used >= self.max_per_key:
                return f"per_symbol_cap_reached {key} {used}/{self.max_per_key}"
        return None

    def spend(self, key: Optional[str] = None) -> Dict[str, Any]:
        data = self.snapshot()
        data["total"] = int(data.get("total", 0)) + 1
        by_key = data.setdefault("by_key", {})
        if key:
            by_key[str(key)] = int(by_key.get(str(key), 0)) + 1
        # La memoria se actualiza ANTES de intentar el disco: si el disco
        # falla, el tope ya esta contado.
        _BUDGET_MEM[self.path] = {"date": data["date"],
                                  "total": data["total"],
                                  "by_key": dict(by_key)}
        _save(self.path, data)
        return data


# ── Cache en memoria ──────────────────────────────────────────────────────────

_CACHE: Dict[str, str] = {}
_CACHE_MAX = 256


def _cache_key(prompt: str, model: str) -> str:
    return hashlib.sha256(f"{model}::{prompt}".encode("utf-8")).hexdigest()


def _cache_get(prompt: str, model: str) -> Optional[str]:
    return _CACHE.get(_cache_key(prompt, model))


def _cache_put(prompt: str, model: str, text: str) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[_cache_key(prompt, model)] = text


# ── Proveedores ───────────────────────────────────────────────────────────────

def _call_genai(prompt: str, api_key: str, model: str) -> str:
    """SDK nuevo (`google-genai`): cliente central + `models.generate_content`."""
    from google import genai  # import local: el modulo es opcional
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


def _call_legacy(prompt: str, api_key: str, model: str) -> str:
    """SDK legacy (`google-generativeai`), conservado por compatibilidad."""
    import google.generativeai as genai  # import local: el modulo es opcional
    if api_key:
        genai.configure(api_key=api_key)
    return genai.GenerativeModel(model).generate_content(prompt).text


def _call_vertex(prompt: str, api_key: str, model: str) -> str:
    """Vertex AI: mismo SDK nuevo, pero autenticado con ADC y no con clave.

    El argumento `api_key` se recibe por uniformidad con los demas proveedores
    y se ignora: aqui quien firma la peticion es el ADC de `gcloud`. Queda el
    parametro para que `_DISPATCH` siga siendo una tabla homogenea.
    """
    from google import genai  # import local: el modulo es opcional
    client = genai.Client(vertexai=True,
                          project=vertex_project(),
                          location=vertex_location())
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


_DISPATCH = {"genai": _call_genai, "legacy": _call_legacy, "vertex": _call_vertex}


# ── Entrada publica ───────────────────────────────────────────────────────────

def complete(prompt: str,
             api_key: str = "",
             models: Optional[List[str]] = None,
             provider: Optional[str] = None,
             cache_key: Optional[str] = None,
             use_cache: bool = True,
             budget: Optional[DailyBudget] = None) -> Dict[str, Any]:
    """Manda `prompt` al LLM y devuelve `{"text", "provider", "model"}`.

    Levanta `LLMError` en cualquier fallo, con `kind` entre:
    `llm_disabled` · `no_api_key` · `budget_exhausted` · `llm_unavailable`.

    La ruta se elige con `provider` / `HUARIZO_LLM_PROVIDER`:
    `auto` (Vertex si esta activo, si no genai, si no legacy) · `vertex` ·
    `genai` · `legacy` · `none`. Vertex no necesita `api_key`; los otros dos
    si, y sin clave la capa se niega a intentar nada.

    El llamador es responsable de caer al fallback y **declarar el motivo**.
    """
    prov = resolve_provider(provider)
    if prov == "none":
        raise LLMError("llm_disabled", "HUARIZO_LLM_PROVIDER=none")

    order = _provider_order(prov)

    # Sin clave solo pueden correr las rutas que no la necesitan (Vertex firma
    # con ADC). Si no queda ninguna, se rechaza; si queda alguna, NO se le
    # puede exigir un secreto que no usa.
    #
    # Esto fallo en la primera implementacion: se exigia la clave con tal de que
    # `genai`/`legacy` siguieran en la lista como relevo, lo que dejaba a Vertex
    # fuera justo en el caso que se diseño para cubrir (no hay clave).
    if not (api_key or "").strip():
        order = [name for name in order if name == "vertex"]
        if not order:
            raise LLMError(
                "no_api_key",
                "GEMINI_API_KEY vacio: configurarlo en .env, o activar Vertex "
                "(HUARIZO_LLM_VERTEX=1), que se autentica con las credenciales "
                "de gcloud y no necesita clave",
            )

    # La cache se consulta ANTES del presupuesto, y no es un detalle: devolver
    # una respuesta que ya tenemos no sale a la red, asi que no puede gastar
    # cupo. Consultarla despues del tope bloqueaba incluso lo que ya estaba en
    # memoria — lo encontro un test, no una revision.
    if use_cache:
        for name in order:
            for model in _models_for(name, models):
                cached = _cache_get(prompt, model)
                if cached is not None:
                    return {"text": cached, "provider": "cache", "model": model}

    if budget is None:
        budget = DailyBudget()
    denied = budget.check(cache_key)
    if denied:
        raise LLMError("budget_exhausted", denied)

    attempts: List[str] = []

    # Proveedor primero, modelo despues:Vertex tiene su propio catalogo, asi que
    # no se puede recorrer "para cada modelo, todos los proveedores" sin
    # mezclar listas que no tienen nada que ver.
    for name in order:
        for model in _models_for(name, models):
            try:
                text = _DISPATCH[name](prompt, api_key, model)
            except Exception as e:  # noqa: BLE001 - el motivo viaja en el error
                attempts.append(f"{name}/{model}: {type(e).__name__}: {e}")
                continue
            if not isinstance(text, str) or not text.strip():
                attempts.append(f"{name}/{model}: empty_response")
                continue
            _cache_put(prompt, model, text)
            budget.spend(cache_key)
            return {"text": text, "provider": name, "model": model}

    raise LLMError("llm_unavailable", " | ".join(attempts[-3:]) or "no attempts")


def reset_for_tests(state_path: Optional[str] = None) -> None:
    """Deja la capa limpia entre tests (cache, contador en disco y en memoria)."""
    _CACHE.clear()
    _BUDGET_MEM.clear()
    path = state_path or os.getenv("HUARIZO_LLM_STATE_PATH", DEFAULT_STATE_PATH)
    try:
        os.unlink(path)
    except OSError:
        pass
