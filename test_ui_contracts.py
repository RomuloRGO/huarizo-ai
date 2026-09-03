"""Contratos entre el frontend y la API: nada de números inventados ni 404s.

Dos clases de defecto que la auditoría encontró en la UI:

1. **Datos fabricados.** `renderTournamentArena` tenía un fallback hardcodeado
   con `win_rate: 51.6`, `profit_factor: 1.23`, `composite_score: 40.5`. Cuando
   el torneo real no devolvía nada, la UI mostraba esos números como si fueran
   un resultado. Peor: coronaba un "WINNER #1" sobre datos inexistentes.

2. **Endpoints muertos.** El JS llama a rutas que `app.py` no define
   (`/api/backtest/run`) o con un método que no existe
   (`DELETE /api/positions/<symbol>`). La acción parece funcionar y en realidad
   devuelve 404/405.

El chequeo de endpoints compara MÉTODO + RUTA. Comparar sólo la ruta no
detectaría el `DELETE`, que es justo uno de los dos casos reales.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(path):
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as f:
        return f.read()


def _code(path):
    """JS sin comentarios: las aserciones no deben chocar con la documentación
    del propio fix (que menciona los valores eliminados). El `//` dentro de una
    URL (`https://`) no se toca."""
    js = _read(path)
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?<!:)//[^\n]*", "", js)
    return js


# ── Rutas declaradas en app.py ────────────────────────────────────────────────

_ROUTE_RE = re.compile(
    r'@app\.route\(\s*"([^"]+)"(?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?', re.S
)
_PARAM_RE = re.compile(r"<(?:[^:<>]+:)?[^<>]+>")


def _declared_routes():
    """{(método, patrón_regex)} declarados en app.py."""
    src = _read(os.path.join(ROOT, "app.py"))
    out = set()
    for path, methods in _ROUTE_RE.findall(src):
        methods = methods or ""
        verbs = set(re.findall(r'"([A-Z]+)"', methods)) or {"GET"}
        # Los params de Flask comodín se tratan como un segmento cualquiera.
        pattern = "^" + _PARAM_RE.sub(r"[^/]+", re.escape(
            _PARAM_RE.sub("\0", path)).replace("\0", "§")) + "$"
        # re.escape rompe los marcadores; se reconstruye a mano.
        pattern = "^" + re.sub(
            r"\\?§", r"[^/]+",
            re.escape(_PARAM_RE.sub("§", path))
        ).replace("/§", "/[^/]+") + "$"
        for verb in verbs:
            out.add((verb, pattern))
    return out


def _js_calls():
    """[(método, ruta)] que el frontend realmente invoca."""
    js = _read(os.path.join(ROOT, "static", "app.js"))
    calls = []
    for m in re.finditer(r"fetch\(\s*[`\"'](/api/[^`\"'?\s]*)", js):
        path = m.group(1)
        # Buscar el método en la ventana siguiente de la llamada.
        window = js[m.end():m.end() + 400]
        method = re.search(r"method\s*:\s*[\"']([A-Z]+)[\"']", window)
        calls.append((method.group(1).upper() if method else "GET", path))
    return calls


def _matches(method, path, routes):
    for verb, pattern in routes:
        if verb == method and re.match(pattern, path):
            return True
    return False


# ── 1. Sin números fabricados ─────────────────────────────────────────────────

def test_no_fabricated_tournament_numbers():
    js = _code(os.path.join("static", "app.js"))
    assert "51.6" not in js, "win_rate fabricado en el fallback del torneo"
    assert "WINNER #1" not in js, "badge de ganador sobre datos inexistentes"


def test_no_hardcoded_composite_score():
    """Un composite_score escrito a mano en el JS es siempre mentira."""
    js = _code(os.path.join("static", "app.js"))
    assert "composite_score: 40.5" not in js
    assert "composite_score: " not in js.replace("composite_score: 40.5", "")


# ── 2. Endpoints que existen de verdad ────────────────────────────────────────

def test_js_has_no_dead_endpoints():
    routes = _declared_routes()
    missing = []
    for method, path in _js_calls():
        if not _matches(method, path, routes):
            missing.append(f"{method} {path}")
    assert missing == [], f"frontend llama a rutas inexistentes: {sorted(set(missing))}"


def test_route_extractor_finds_the_known_routes():
    """El extractor debe ver las rutas reales; si no, el test anterior es vacuo."""
    routes = _declared_routes()
    patterns = {p for _, p in routes}
    assert any(re.match(p, "/api/options/chain/AAPL") for p in patterns)
    assert any(re.match(p, "/api/positions") for p in patterns)


def test_no_delete_on_positions_route():
    """`DELETE /api/positions/<symbol>` no existe: sólo hay GET.

    Era el control de cierre de acciones, incompatible con la política
    options-only de la Task 2.
    """
    routes = _declared_routes()
    assert not any(verb == "DELETE" and re.match(p, "/api/positions/AAPL")
                   for verb, p in routes)


def test_backtest_uses_the_real_endpoint():
    js = _code(os.path.join("static", "app.js"))
    assert "/api/backtest/run" not in js, "endpoint muerto"
    assert "/api/agent/backtest" in js


def test_no_stock_close_control_in_js():
    """No debe quedar ningún cierre de ACCIONES en el frontend."""
    js = _code(os.path.join("static", "app.js"))
    assert "/api/positions/${symbol}" not in js
    assert "closePosition(" not in js


# ── 3. El torneo no puede coronar sin elegibilidad ────────────────────────────

def test_tournament_renders_empty_state():
    """Sin torneo válido la UI debe decirlo, no inventar una tabla."""
    js = _read(os.path.join("static", "app.js"))
    lowered = js.lower()
    assert "tournament-empty" in js
    assert "no valid tournament" in lowered


def test_js_renders_undefined_profit_factor():
    """`profit_factor` puede ser null desde la Task 6: hay que mostrar 'n/d'."""
    js = _read(os.path.join("static", "app.js"))
    assert "n/d" in js


def test_js_surfaces_options_only_rejection():
    """El 403 de /api/orders/bracket (Task 2) debe tener mensaje visible."""
    js = _read(os.path.join("static", "app.js"))
    lowered = js.lower()
    assert "options-only" in lowered or "options only" in lowered or \
        "solo opciones" in lowered or "sólo opciones" in lowered
