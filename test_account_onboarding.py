"""Onboarding de la cuenta de concurso: solo se arma contra una cuenta nueva.

El concurso exige una cuenta de paper NUEVA que arranca en 100.000 USD. La
cuenta anterior estaba contaminada (equity 176k, cash -155k, posiciones
heredadas), así que armar el bot contra ella invalidaría la entrada.

El plan original pedía una sola función `validate_contest_account` que rechaza
si `abs(equity - 100000) > 1%`, corriendo en CADA habilitación. Esa es la trampa
que este archivo blinda: el equity se mueve en cuanto se opera. Tras una sesión
la cuenta está legítimamente en 101.500 o 98.000, fuera de la banda
(99.000-101.000), y el bot ya no podría volver a encenderse nunca. Fallaría
justo cuando más se necesita: después de que un drawdown dispare una pausa, la
recuperación quedaría bloqueada para siempre.

De ahí las dos validaciones:
  * `validate_contest_account` -> ONBOARDING, estricto, una sola vez.
  * `validate_armed_account`   -> CONTINUO, cada habilitación. El equity puede
    derivar libremente; lo que se exige es identidad y salud.

El test clave es `test_equity_derivado_tras_armar`: onboarding lo rechaza,
armado lo acepta.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
import account_onboarding as onboarding_mod
from account_onboarding import (
    EXPECTED_START_EQUITY,
    build_onboarding_record,
    load_onboarding,
    save_onboarding,
    validate_armed_account,
    validate_contest_account,
)

# Cuenta nueva verificada en vivo (MCP) el 2026-09-01.
FRESH = {
    "account_id": "a5122159-6134-48c3-ad8c-d978f662c265",
    "account_number": "PA3M5SST2YN2",
    "equity": 100000.0,
    "cash": 100000.0,
    "last_equity": 100000.0,
    "buying_power": 400000.0,
    "options_buying_power": 100000.0,
    "long_market_value": 0.0,
    "short_market_value": 0.0,
    "options_approved_level": 3,
    "status": "ACTIVE",
    "usable": True,
}

# La cuenta contaminada que se quiere dejar fuera. Nótese que también tenía
# options_approved_level 3: ese campo por sí solo no distingue frescura.
CONTAMINATED = {
    "account_id": "old-0000-contaminated",
    "account_number": "OLD123456",
    "equity": 176304.0,
    "cash": -155235.0,
    "last_equity": 175000.0,
    "buying_power": 500.0,
    "long_market_value": 331540.0,
    "short_market_value": 0.0,
    "options_approved_level": 3,
    "status": "ACTIVE",
    "usable": True,
}


def _fresh(**over):
    snap = dict(FRESH)
    snap.update(over)
    return snap


def _armed(**over):
    """Registro de onboarding tal como quedaría tras armar con FRESH."""
    rec = build_onboarding_record(_fresh(**over))
    rec["armed_at"] = "2026-09-01T12:00:00+00:00"
    return rec


# ── 1. Onboarding estricto ───────────────────────────────────────────────────

def test_cuenta_nueva_pasa_onboarding():
    verdict = validate_contest_account(FRESH)

    assert verdict["valid"] is True
    assert verdict["reason"] == "ok"


def test_cuenta_contaminada_es_rechazada():
    verdict = validate_contest_account(CONTAMINATED)

    assert verdict["valid"] is False
    assert verdict["reason"] == "equity_out_of_range"


def test_cuenta_con_posiciones_heredadas_es_rechazada():
    """Una cuenta plana en 100k pero con posiciones abiertas NO es nueva."""
    verdict = validate_contest_account(_fresh(long_market_value=4200.0))

    assert verdict["valid"] is False
    assert verdict["reason"] == "existing_positions"


def test_equity_fuera_de_banda_1_por_ciento_es_rechazado():
    assert validate_contest_account(_fresh(equity=101500.0))["reason"] == "equity_out_of_range"
    assert validate_contest_account(_fresh(equity=98500.0))["reason"] == "equity_out_of_range"
    # Dentro de la banda: ruido de redondeo, no contaminación.
    assert validate_contest_account(_fresh(equity=100500.0))["valid"] is True


# ── 2. Opciones: el concurso las exige ───────────────────────────────────────
# Nivel 2 es el mínimo para comprar calls/puts al descubierto; 1 es solo
# covered. Una cuenta nueva puede provisionarse sin opciones y eso mata al bot.

def test_nivel_de_opciones_faltante_es_rechazado():
    snap = _fresh()
    del snap["options_approved_level"]

    verdict = validate_contest_account(snap)

    assert verdict["valid"] is False
    assert verdict["reason"] == "options_level_missing"


@pytest.mark.parametrize("level", [0, 1])
def test_nivel_de_opciones_insuficiente_es_rechazado(level):
    verdict = validate_contest_account(_fresh(options_approved_level=level))

    assert verdict["valid"] is False
    assert verdict["reason"] == "options_level_insufficient"


def test_nivel_de_opciones_2_es_suficiente():
    assert validate_contest_account(_fresh(options_approved_level=2))["valid"] is True


# ── 3. Cuenta bloqueada / no activa ──────────────────────────────────────────

def test_cuenta_no_activa_es_rechazada():
    verdict = validate_contest_account(_fresh(status="SUSPENDED"))

    assert verdict["valid"] is False
    assert verdict["reason"] == "account_not_active"


def test_cuenta_bloqueada_por_flag_usable_es_rechazada():
    verdict = validate_contest_account(_fresh(usable=False))

    assert verdict["valid"] is False
    assert verdict["reason"] == "account_blocked"


def test_cuenta_bloqueada_por_trading_blocked_es_rechazada():
    verdict = validate_contest_account(_fresh(trading_blocked=True))

    assert verdict["valid"] is False
    assert verdict["reason"] == "account_blocked"


def test_estado_desconocido_falla_cerrado():
    snap = _fresh()
    del snap["status"]

    verdict = validate_contest_account(snap)

    assert verdict["valid"] is False
    assert verdict["reason"] == "status_unknown"


# ── 4. Entradas basura: nunca levantan ───────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "cuenta", 42, [], {}])
def test_entradas_invalidas_son_rechazadas_sin_excepcion(bad):
    verdict = validate_contest_account(bad)

    assert verdict["valid"] is False
    assert isinstance(verdict["reason"], str) and verdict["reason"]


def test_equity_faltante_es_rechazado():
    snap = _fresh()
    del snap["equity"]

    assert validate_contest_account(snap)["reason"] == "missing_equity"


@pytest.mark.parametrize("bad_equity", ["abc", float("nan"), float("inf"), None, [1]])
def test_equity_no_numerico_es_rechazado(bad_equity):
    verdict = validate_contest_account(_fresh(equity=bad_equity))

    assert verdict["valid"] is False
    assert verdict["reason"] == "invalid_equity"


def test_cash_negativo_es_rechazado():
    assert validate_contest_account(_fresh(cash=-1.0))["reason"] == "negative_cash"


def test_entradas_invalidas_en_armado_tampoco_levantan():
    verdict = validate_armed_account(None, _armed())

    assert verdict["valid"] is False
    assert verdict["reason"] == "invalid_input"


# ── 5. EL TEST CLAVE: equity derivado tras armar ─────────────────────────────

def test_equity_derivado_tras_armar():
    """Regresión contra el diseño del plan.

    Tras una sesión la cuenta está en 98.500 (o 101.500). El onboarding
    estricto DEBE rechazarla; la validación continua DEBE aceptarla. Si ambas
    la rechazan, el bot no se puede re-encender nunca después del día 1.
    """
    drifted = _fresh(equity=98500.0)
    record = _armed()

    strict = validate_contest_account(drifted)

    assert strict["valid"] is False
    assert strict["reason"] == "equity_out_of_range"

    ongoing = validate_armed_account(drifted, record)

    assert ongoing["valid"] is True
    assert ongoing["reason"] == "ok"


def test_ganancia_tras_armar_tambien_es_aceptada():
    ongoing = validate_armed_account(_fresh(equity=101500.0,
                                            long_market_value=3100.0),
                                     _armed())

    assert ongoing["valid"] is True, ongoing["reason"]


def test_cuenta_diferente_tras_armar_es_rechazada():
    """Armar contra otra cuenta es el accidente que esto evita."""
    verdict = validate_armed_account(CONTAMINATED, _armed())

    assert verdict["valid"] is False
    assert verdict["reason"] == "account_mismatch"


def test_sin_identificador_en_el_snapshot_falla_cerrado():
    snap = _fresh(equity=98500.0)
    del snap["account_id"]
    del snap["account_number"]

    verdict = validate_armed_account(snap, _armed())

    assert verdict["valid"] is False
    assert verdict["reason"] == "identity_unknown"


def test_sin_onboarding_previo_es_rechazado():
    assert validate_armed_account(FRESH, None)["reason"] == "not_onboarded"


def test_armado_rechaza_insolvencia():
    verdict = validate_armed_account(_fresh(equity=0.0), _armed())

    assert verdict["valid"] is False
    assert verdict["reason"] == "insolvent"


def test_armado_rechaza_perdida_de_opciones():
    verdict = validate_armed_account(_fresh(equity=98500.0,
                                            options_approved_level=1),
                                     _armed())

    assert verdict["valid"] is False
    assert verdict["reason"] == "options_level_insufficient"


def test_armado_rechaza_cuenta_bloqueada():
    verdict = validate_armed_account(_fresh(equity=98500.0, status="CLOSED"),
                                     _armed())

    assert verdict["valid"] is False
    assert verdict["reason"] == "account_not_active"


# ── 6. Persistencia ──────────────────────────────────────────────────────────

def test_onboarding_persiste_y_se_relee(tmp_path):
    path = str(tmp_path / "onboarding.json")
    record = _armed()

    assert save_onboarding(path, record) is True

    reloaded = load_onboarding(path)

    assert reloaded["account_id"] == record["account_id"]
    assert reloaded["account_number"] == record["account_number"]
    assert reloaded["baseline_equity"] == 100000.0
    assert reloaded["armed_at"]


def test_archivo_inexistente_degrada_a_no_armado(tmp_path):
    assert load_onboarding(str(tmp_path / "nope.json")) is None


def test_json_corrupto_degrada_a_no_armado_sin_levantar(tmp_path):
    path = tmp_path / "onboarding.json"
    path.write_text('{"account_id": "a5122', encoding="utf-8")

    assert load_onboarding(str(path)) is None


def test_json_de_tipo_equivocado_degrada_a_no_armado(tmp_path):
    path = tmp_path / "onboarding.json"
    path.write_text(json.dumps(["no", "es", "un", "dict"]), encoding="utf-8")

    assert load_onboarding(str(path)) is None


def test_registro_sin_account_id_degrada_a_no_armado(tmp_path):
    path = tmp_path / "onboarding.json"
    path.write_text(json.dumps({"baseline_equity": 100000.0}), encoding="utf-8")

    assert load_onboarding(str(path)) is None


# ── 7. Endpoint /api/options/autopilot ───────────────────────────────────────

class _FakeTools:
    """Sustituye la capa MCP/CLI. `method="none"` => la app usa el servicio."""

    def __init__(self, snapshot=None, method="none"):
        self.method = method
        self.snapshot = snapshot

    def get_account_snapshot(self):
        return self.snapshot


@pytest.fixture
def autopilot(tmp_path, monkeypatch):
    """Aísla el archivo de onboarding y el estado del autopilot. Sin red."""
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_ENV_ENABLED", True)
    monkeypatch.setattr(app_module, "options_autopilot_state",
                        {"enabled": False, "paused_reason": None})
    monkeypatch.setattr(app_module, "ONBOARDING_PATH",
                        str(tmp_path / "onboarding.json"))
    # ESTA línea faltaba, y no era un descuido de higiene: era una fuga de
    # seguridad. Ocho tests de este archivo hacen POST {"enabled": True}, y el
    # endpoint persiste con `_autopilot_persist()` (app.py:847). Sin parchear la
    # ruta, CADA CORRIDA DE LA SUITE escribía `{"enabled": true}` en el archivo
    # REAL de estado.
    #
    # Consecuencia medida el 2026-09-03: el bot amaneció encendido "reanudado
    # desde el disco" y se atribuyó a una decisión del operador. Era esto. Y
    # como el servidor no sobrevive a la noche en esta máquina, los tests
    # quedaban como la vía por la que se ARMABA el trading autónomo de verdad.
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_STATE_PATH",
                        str(tmp_path / "autopilot_state.json"))
    monkeypatch.setattr(app_module, "ALPACA_TOOLS", _FakeTools())
    return app_module.app.test_client()


# Capturada al importar, ANTES de que ningún fixture la parchee. Es la ruta del
# archivo que el bot real lee al arrancar.
_REAL_STATE_PATH = app_module.OPTIONS_AUTOPILOT_STATE_PATH


def _leer_estado_real():
    if not os.path.exists(_REAL_STATE_PATH):
        return None
    with open(_REAL_STATE_PATH, encoding="utf-8") as handle:
        return handle.read()


def test_la_suite_no_arma_el_autopilot_real(autopilot, monkeypatch):
    """Correr los tests no debe poder encender el trading de verdad.

    Regresión del 2026-09-03. Ocho tests de este archivo hacen POST
    {"enabled": True} y el endpoint persiste en disco (app.py:847). El fixture
    parcheaba `ONBOARDING_PATH` pero NO `OPTIONS_AUTOPILOT_STATE_PATH`, así que
    **cada corrida de la suite dejaba `{"enabled": true}` en el archivo real**.

    El efecto es peor que ensuciar un archivo: el estado se persiste
    precisamente para sobrevivir reinicios, y en esta máquina el servidor no
    aguanta la noche. Los tests quedaban armando el bot autónomo de verdad, y
    al día siguiente aparecía como "reanudado desde el estado guardado",
    indistinguishable de una decisión del operador.
    """
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: dict(FRESH))

    antes = _leer_estado_real()

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})
    assert r.status_code == 200, r.get_json()
    # En memoria SÍ debe cambiar: es justo lo que el test quiere verificar.
    assert app_module.options_autopilot_state["enabled"] is True

    assert _leer_estado_real() == antes, (
        "la suite escribió en el archivo real de estado del autopilot: "
        "correr pytest puede dejar el bot armado")


def test_endpoint_armar_con_cuenta_nueva(autopilot, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: dict(FRESH))

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["success"] is True
    assert body["state"]["enabled"] is True
    # Dejó constancia de la cuenta contra la que se armó.
    assert load_onboarding(str(tmp_path / "onboarding.json"))["account_id"] == \
        FRESH["account_id"]


def test_endpoint_armar_con_cuenta_contaminada_devuelve_409(autopilot, monkeypatch):
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: dict(CONTAMINATED))

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 409
    body = r.get_json()
    assert body["success"] is False
    assert body["reason"] == "equity_out_of_range"
    assert body["detail"]
    # La UI muestra `error` en el alerta: un juez no puede ver "undefined".
    assert "equity_out_of_range" in body["error"]
    assert app_module.options_autopilot_state["enabled"] is False


def test_endpoint_rehabilitar_con_equity_derivado(autopilot, monkeypatch):
    """El bot se puede volver a encender tras operar. El bug del plan."""
    state = {"equity": 100000.0}

    def account():
        return _fresh(equity=state["equity"])

    monkeypatch.setattr(app_module.alpaca_service, "get_account", account)

    assert autopilot.post("/api/options/autopilot",
                          json={"enabled": True}).status_code == 200
    autopilot.post("/api/options/autopilot", json={"enabled": False})
    state["equity"] = 98500.0

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 200, r.get_json()
    assert r.get_json()["state"]["enabled"] is True


def test_endpoint_rechaza_cuenta_distinta_tras_armar(autopilot, monkeypatch):
    served = {"snap": dict(FRESH)}
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: dict(served["snap"]))

    assert autopilot.post("/api/options/autopilot",
                          json={"enabled": True}).status_code == 200
    served["snap"] = dict(CONTAMINATED)

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 409
    assert r.get_json()["reason"] == "account_mismatch"


def test_endpoint_falla_cerrado_si_no_hay_snapshot(autopilot, monkeypatch):
    def boom():
        raise RuntimeError("Alpaca 503")

    monkeypatch.setattr(app_module.alpaca_service, "get_account", boom)

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 409
    assert r.get_json()["reason"] == "account_snapshot_unavailable"
    assert app_module.options_autopilot_state["enabled"] is False


def test_desactivar_siempre_funciona_aunque_todo_falle(autopilot, monkeypatch,
                                                       tmp_path):
    """El botón de emergencia: no puede quedar bloqueado por nada."""

    def boom():
        raise RuntimeError("Alpaca 503")

    monkeypatch.setattr(app_module.alpaca_service, "get_account", boom)
    # Estado de onboarding corrupto en disco.
    (tmp_path / "onboarding.json").write_text('{"account_id": "cortado',
                                              encoding="utf-8")
    app_module.options_autopilot_state["enabled"] = True

    r = autopilot.post("/api/options/autopilot", json={"enabled": False})

    assert r.status_code == 200, r.get_json()
    assert r.get_json()["success"] is True
    assert app_module.options_autopilot_state["enabled"] is False


def test_desactivar_funciona_incluso_con_kill_switch(autopilot, monkeypatch):
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_ENV_ENABLED", False)
    app_module.options_autopilot_state["enabled"] = True

    r = autopilot.post("/api/options/autopilot", json={"enabled": False})

    assert r.status_code == 200
    assert app_module.options_autopilot_state["enabled"] is False


def test_endpoint_usa_la_herramienta_oficial_cuando_esta_activa(autopilot,
                                                                monkeypatch):
    """Con MCP/CLI activo manda su foto, pero sin perder la identidad."""
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: {"id": "servicio-viejo", "equity": 1.0,
                                 "cash": 1.0, "status": "ACTIVE",
                                 "options_approved_level": 3,
                                 "long_market_value": 0.0,
                                 "short_market_value": 0.0})
    monkeypatch.setattr(app_module, "ALPACA_TOOLS",
                        _FakeTools({"equity": 100000.0, "cash": 100000.0,
                                    "buying_power": 400000.0,
                                    "long_market_value": 0.0,
                                    "short_market_value": 0.0,
                                    "usable": True, "status": "ACTIVE",
                                    "source": "mcp"},
                                   method="mcp"))

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 200, r.get_json()
    assert app_module.options_autopilot_state["enabled"] is True
    # El merge manda en los números (equity del MCP) pero conserva la
    # identidad del servicio: sin ella no hay validación continua posible.
    assert r.get_json()["validation"]["detail"]["equity"] == 100000.0
    assert r.get_json()["onboarding"]["account_id"] == "servicio-viejo"


def test_la_herramienta_no_pisa_el_status_con_vacio(autopilot, monkeypatch):
    """El MCP manda status="" cuando no lo trae; no debe invalidar la cuenta."""
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: dict(FRESH))
    monkeypatch.setattr(app_module, "ALPACA_TOOLS",
                        _FakeTools({"equity": 100000.0, "cash": 100000.0,
                                    "buying_power": 400000.0,
                                    "long_market_value": 0.0,
                                    "short_market_value": 0.0,
                                    "usable": True, "status": "",
                                    "source": "mcp"},
                                   method="mcp"))

    r = autopilot.post("/api/options/autopilot", json={"enabled": True})

    assert r.status_code == 200, r.get_json()


def test_baseline_del_concurro_es_100k():
    assert EXPECTED_START_EQUITY == 100000.0
    assert onboarding_mod.EQUITY_TOLERANCE_PCT == 0.01
    assert onboarding_mod.MIN_OPTIONS_APPROVED_LEVEL == 2
