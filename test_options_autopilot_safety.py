"""El autopilot de opciones debe obedecer las pausas y usar el ledger.

Dos huecos encontrados al cablear la política de pausas (Task 5):

1. `options_autopilot_state["paused_reason"]` vive SÓLO en memoria. Reiniciar el
   proceso resucitaba la cuenta aunque estuviera en pausa por pérdida diaria,
   que es exactamente el bug que `DailyLossTracker` se creó para evitar.

2. `_run_one_options_cycle` llamaba a `alpaca_service.place_option_order`
   DIRECTAMENTE, sin pasar por el execution ledger. Quiere decir que el camino
   autónomo (el que puntúa en el concurso) se quedó sin autorización, sin
   reserva y sin llave de idempotencia cuando la Task 3 cableó el endpoint
   manual. Un reenvío en el mismo bucket duplicaba la orden.
"""
import json
import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
import journal
from execution_ledger import ExecutionLedger
from exit_manager import ET_TZ, is_market_open as real_is_market_open
from risk_gate import DailyLossTracker

OCC = "AAPL260918C00220000"
DAY_KEY = date.today().isoformat()

_ORIG_LOAD_JOURNAL = journal.load_journal


def _proposal(occ=OCC, ask=3.0, qty=1):
    return {
        "available": True,
        "direction": "long",
        "contract": {"occ_symbol": occ, "ask": ask},
        "sizing": {"qty": qty},
        "exit_plan": {"tp_premium": ask * 1.5, "sl_premium": ask * 0.5},
        # Desde el 2026-09-03 el ciclo autónomo aplica un freno de volumen que
        # FALLA CERRADO: una propuesta sin esta llave se descarta. Debe estar
        # en todos los mocks, porque `build_options_proposal` real siempre la
        # devuelve (con fallback a `unknown`).
        "volume_confirmation": _VOLUME_NEUTRAL,
    }


# ratio >= VOLUME_MIN_RATIO (0.5) y < VOLUME_STRONG_RATIO (1.2) -> confirma.
_VOLUME_NEUTRAL = {"ratio": 1.0, "level": "neutral", "confirms_entry": True}
_VOLUME_STRONG = {"ratio": 1.8, "level": "strong", "confirms_entry": True}
_VOLUME_WEAK = {"ratio": 0.2, "level": "weak", "confirms_entry": False}
_VOLUME_UNKNOWN = {"ratio": None, "level": "unknown", "confirms_entry": False}


@pytest.fixture
def autopilot_env(tmp_path, monkeypatch):
    """Aísla journal, ledger y risk tracker; nunca toca la red ni archivos reales."""
    jpath = str(tmp_path / "journal.json")

    monkeypatch.setattr(app_module, "exec_ledger",
                        ExecutionLedger(path=str(tmp_path / "ledger.json"),
                                        journal_path=jpath))
    monkeypatch.setattr(app_module.huarizo_agent, "risk_tracker",
                        DailyLossTracker(path=str(tmp_path / "risk.json")))
    monkeypatch.setattr(journal, "load_journal",
                        lambda *a, **k: _ORIG_LOAD_JOURNAL(path=jpath))
    monkeypatch.setattr(app_module.journal_mod, "add_entry",
                        lambda entry, **kw: dict(entry, id=1))

    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: {"equity": 100000.0, "cash": 100000.0,
                                 "buying_power": 100000.0,
                                 "options_buying_power": 100000.0,
                                 "last_equity": 100000.0})
    # Sin este mock el ciclo consulta la cuenta REAL de Alpaca y R6 rechaza al
    # candidato en cuanto el paper account tiene una posicion abierta en ese
    # subyacente: estos tests pasaban con la cuenta recien creada y empezaron a
    # fallar cuando el concurso abrio operaciones. Un test que depende del
    # estado de una cuenta viva no es un test.
    monkeypatch.setattr(app_module.alpaca_service, "get_positions", lambda: [])
    monkeypatch.setattr(app_module.alpaca_service, "get_top_movers",
                        lambda top=5: {"gainers": [{"symbol": "AAPL"}]})
    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal",
                        lambda t, svc: _proposal())

    calls = []

    def fake_place(occ_symbol, qty=None, action=None, limit_price=None,
                   time_in_force=None, client_order_id=None, **kw):
        calls.append({"occ": occ_symbol, "qty": qty,
                      "client_order_id": client_order_id})
        return {"id": f"SIM-{len(calls)}", "status": "accepted"}

    monkeypatch.setattr(app_module.alpaca_service, "place_option_order",
                        fake_place)

    # El ciclo de apertura respeta el horario de mercado desde el 2026-09-02.
    # Los tests de este archivo verifican pausas y ledger, NO horario: si se
    # corren un sabado o a las 3 a.m. todos verian `market_closed` y dejarian
    # de probar lo que importa. Se fija reloj "siempre abierto"; los tests de
    # horario de abajo restauran la funcion real a proposito.
    monkeypatch.setattr(app_module, "is_market_open", lambda now=None: True)
    return calls


# ── Pausas ────────────────────────────────────────────────────────────────────

def test_autopilot_respeta_safety_pause(autopilot_env, monkeypatch):
    """Una pausa de safety debe detener el autopilot de opciones."""
    app_module.huarizo_agent.risk_tracker.safety_pause("mcp_unavailable")

    result = app_module._run_one_options_cycle()

    assert result.get("skipped") == "safety_pause"
    assert autopilot_env == []


def test_autopilot_respeta_daily_loss_pause(autopilot_env):
    app_module.huarizo_agent.risk_tracker.pause(DAY_KEY, "daily_loss_breach")

    result = app_module._run_one_options_cycle()

    assert result.get("skipped") == "daily_loss_pause"
    assert autopilot_env == []


def test_autopilot_persiste_la_pausa_diaria_en_disco(autopilot_env, monkeypatch):
    """La pausa por pérdida diaria se guarda y sobrevive a un reinicio."""
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: {"equity": 94000.0, "cash": 94000.0,
                                 "buying_power": 94000.0,
                                 "options_buying_power": 94000.0,
                                 "last_equity": 100000.0})

    result = app_module._run_one_options_cycle()

    assert result.get("skipped") == "daily_loss_pause"
    tracker = app_module.huarizo_agent.risk_tracker
    assert tracker.is_paused(DAY_KEY) is True
    # Un "reinicio" (nueva instancia sobre el mismo archivo) sigue en pausa.
    restarted = DailyLossTracker(path=tracker.path)
    assert restarted.is_paused(DAY_KEY) is True
    state = restarted.pause_state(DAY_KEY)
    assert state["daily_loss_paused"] is True
    assert "daily loss" in str(state["daily_reason"])


def test_autopilot_pausa_safety_si_no_puede_leer_la_cuenta(autopilot_env, monkeypatch):
    """Sin foto de cuenta no se opera y queda marcado como falla de safety."""
    def boom():
        raise RuntimeError("Alpaca 503")

    monkeypatch.setattr(app_module.alpaca_service, "get_account", boom)

    result = app_module._run_one_options_cycle()

    assert "account_error" in str(result.get("skipped"))
    assert app_module.huarizo_agent.risk_tracker.is_safety_paused() is True
    assert autopilot_env == []


# ── Ledger en el camino autónomo ──────────────────────────────────────────────

def test_autopilot_envia_client_order_id(autopilot_env):
    """El camino autónomo debe usar el ledger, igual que el endpoint manual."""
    result = app_module._run_one_options_cycle()

    assert result.get("opened") == "AAPL"
    assert len(autopilot_env) == 1
    assert autopilot_env[0]["client_order_id"]
    assert autopilot_env[0]["client_order_id"].startswith("huarizo-")


def test_autopilot_no_duplica_ordenes_en_el_mismo_bucket(autopilot_env):
    """Dos ciclos en la misma ventana de idempotencia => una sola orden."""
    first = app_module._run_one_options_cycle()
    second = app_module._run_one_options_cycle()

    assert first.get("opened") == "AAPL"
    assert len(autopilot_env) == 1
    # El segundo ciclo no encuentra candidatos válidos (ya está reservado).
    assert second.get("skipped") is not None


# ── Confirmación por volumen ──────────────────────────────────────────────────
#
# Cableada el 2026-09-03. Es un FRENO sobre la entrada autónoma, no un factor
# del score: no mueve `confidence_score` ni elige el contrato. El punto que
# importa fijar aquí es que FALLA CERRADO — si el volumen no se puede medir, no
# se abre. Preferimos perder una entrada antes que operar a ciegas con un feed
# degradado que devuelve un `Volume` vacío.

def _proposal_with_volume(level="neutral", ratio=1.0):
    """Una propuesta igual a `_proposal` pero con el veredicto de volumen dado."""
    payload = _proposal()
    payload["volume_confirmation"] = {
        "ratio": ratio,
        "level": level,
        "confirms_entry": level in ("strong", "neutral"),
    }
    return payload


@pytest.mark.parametrize("level,ratio", [
    ("strong", 1.8),
    ("neutral", 0.9),
])
def test_autopilot_abre_si_el_volumen_confirma(autopilot_env, monkeypatch,
                                               level, ratio):
    """Volumen `strong` o `neutral`: la entrada procede normalmente."""
    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal",
                        lambda t, svc: _proposal_with_volume(level, ratio))

    result = app_module._run_one_options_cycle()

    assert result.get("opened") == "AAPL"
    assert len(autopilot_env) == 1


@pytest.mark.parametrize("level,ratio", [
    ("weak", 0.2),      # volumen muerto: < 0.5x su media de 20 sesiones
    ("unknown", None),  # no medible: feed degradado o pocas velas
])
def test_autopilot_no_abre_si_el_volumen_no_confirma(autopilot_env, monkeypatch,
                                                     level, ratio):
    """Sin confirmación de volumen no se escanea el resto del candidato."""
    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal",
                        lambda t, svc: _proposal_with_volume(level, ratio))

    result = app_module._run_one_options_cycle()

    assert autopilot_env == []
    assert result.get("opened") is None


def test_autopilot_no_abre_si_la_propuesta_no_trae_volumen(autopilot_env,
                                                           monkeypatch):
    """El freno falla cerrado: propuesta sin la llave => no se opera.

    `build_options_proposal` real siempre devuelve la llave (con fallback a
    `unknown`), así que en producción este caso no ocurre. El test existe para
    que nadie "optimice" el freno quitándolo y deje el ciclo operando a ciegas
    si algún día cambia la forma del payload.
    """
    def sin_volumen(ticker, svc):
        payload = _proposal()
        payload.pop("volume_confirmation", None)
        return payload

    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal",
                        sin_volumen)

    result = app_module._run_one_options_cycle()

    assert autopilot_env == []
    assert result.get("opened") is None


# ── Horario de mercado ────────────────────────────────────────────────────────
#
# El exit manager ya respetaba el horario, pero el ciclo de APERTURA no: con el
# daemon encendido escaneaba a las 3 a.m. y mandaba una orden `limit` + `day`
# contra el `ask` de un snapshot rancio. Estos dos tests usan la funcion REAL
# (no la del fixture) para probar el cableado, no solo que se respeta un flag.

def _et(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=ET_TZ)


@pytest.mark.parametrize("when,label", [
    (_et(2026, 8, 22, 11, 0), "sabado 11:00 ET"),
    (_et(2026, 8, 26, 3, 0), "miercoles 03:00 ET"),
    (_et(2026, 8, 26, 8, 0), "miercoles 08:00 ET (pre-market)"),
    (_et(2026, 8, 26, 16, 30), "miercoles 16:30 ET (post-cierre)"),
])
def test_autopilot_no_abre_fuera_de_horario(autopilot_env, monkeypatch, when, label):
    """Fuera de horario no se escanea, no se llama a la API y no se ordena."""
    monkeypatch.setattr(app_module, "is_market_open", real_is_market_open)

    result = app_module._run_one_options_cycle(now=when)

    assert result.get("skipped") == "market_closed", f"{label}: {result}"
    assert autopilot_env == [], f"{label}: se envio una orden"


def test_autopilot_abre_dentro_de_horario(autopilot_env, monkeypatch):
    """Miercoles 11:00 ET: el ciclo procede. Contrapartida del test anterior."""
    monkeypatch.setattr(app_module, "is_market_open", real_is_market_open)

    result = app_module._run_one_options_cycle(now=_et(2026, 8, 26, 11, 0))

    assert result.get("opened") == "AAPL"
    assert len(autopilot_env) == 1


# ── Universo de candidatos ────────────────────────────────────────────────────
#
# El 2026-09-02 los gainers del screener eran warrants (RCKTW, GFAIW, KWMWW):
# sin cadena de opciones. El daemon corria cada 5 min y jamas abria nada.

def test_el_universo_de_respaldo_esta_definido():
    """Sin respaldo no hay candidatos operables cuando el screener falla."""
    assert app_module.OPTIONS_UNIVERSE
    assert "SPY" in app_module.OPTIONS_UNIVERSE
    assert all(s and s == s.strip().upper() for s in app_module.OPTIONS_UNIVERSE)


def test_autopilot_usa_el_respaldo_si_el_screener_no_es_operable(autopilot_env, monkeypatch):
    """Gainers sin cadena -> el respaldo liquido debe rescatar el ciclo."""
    monkeypatch.setattr(app_module.alpaca_service, "get_top_movers",
                        lambda top=5: {"gainers": [{"symbol": "RCKTW"},
                                                   {"symbol": "GFAIW"}]})

    def only_liquid(ticker, svc):
        if ticker not in ("SPY", "QQQ"):
            return {"available": False, "reason": "No options chain available"}
        return _proposal()

    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal",
                        only_liquid)

    result = app_module._run_one_options_cycle()

    assert result.get("opened") == "SPY"
    assert len(autopilot_env) == 1


def test_autopilot_no_cae_si_el_screener_falla(autopilot_env, monkeypatch):
    """Un screener caido no debe tumbar el ciclo: con el respaldo alcanza."""
    def boom(top=5):
        raise RuntimeError("screener 503")

    monkeypatch.setattr(app_module.alpaca_service, "get_top_movers", boom)

    result = app_module._run_one_options_cycle()

    assert result.get("opened") is not None
    assert len(autopilot_env) == 1


def test_autopilot_da_prioridad_al_screener(autopilot_env, monkeypatch):
    """Si un gainer SI es operable, gana: el respaldo no lo desplaza."""
    monkeypatch.setattr(app_module.alpaca_service, "get_top_movers",
                        lambda top=5: {"gainers": [{"symbol": "AAPL"}]})
    seen = []

    def record(ticker, svc):
        seen.append(ticker)
        return _proposal()

    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal", record)

    result = app_module._run_one_options_cycle()

    assert result.get("opened") == "AAPL"
    assert seen[0] == "AAPL"


def test_autopilot_respeta_el_techo_de_candidatos(autopilot_env, monkeypatch):
    """No se analiza el universo entero: cada candidato cuesta 4 backtests."""
    seen = []

    def record(ticker, svc):
        seen.append(ticker)
        return {"available": False, "reason": "none"}

    monkeypatch.setattr(app_module.huarizo_agent, "build_options_proposal", record)

    app_module._run_one_options_cycle()

    assert len(seen) <= app_module.OPTIONS_MAX_CANDIDATES
    assert len(set(seen)) == len(seen)  # sin duplicados


# ── Persistencia del interruptor ──────────────────────────────────────────────
#
# `options_autopilot_state["enabled"]` vivia SOLO en memoria y se inicializaba
# en False. Traduccion: cada reinicio del proceso — caida, corte de luz,
# despliegue — apagaba el trading SIN avisar. El proceso seguia vivo, respondia
# HTTP y no abria una sola posicion: nadie se enteraba hasta la manana siguiente.
#
# Estos tests fijan sobre todo el lado peligroso de la persistencia: un archivo
# de estado puede APAGAR el bot, pero no debe poder ENCENDERLO por su cuenta.

@pytest.fixture
def switch(tmp_path, monkeypatch):
    """Aísla el archivo de estado y el diccionario en memoria."""
    path = str(tmp_path / "autopilot_state.json")
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_STATE_PATH", path)
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_ENV_ENABLED", True)
    for key, value in (("enabled", False), ("paused_reason", None)):
        monkeypatch.setitem(app_module.options_autopilot_state, key, value)
    return path


def _write(path, raw):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(raw)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_al_reiniciar_el_autopilot_vuelve_encendido(switch):
    """El bug. Sin persistencia el bot amanece mudo y nadie se entera."""
    _write(switch, json.dumps({"enabled": True, "saved_at": 0.0}))

    assert app_module._autopilot_restore() is True
    assert app_module.options_autopilot_state["enabled"] is True


def test_apagar_queda_guardado_en_disco(switch):
    app_module.options_autopilot_state["enabled"] = True
    assert app_module._autopilot_persist() is True
    assert _read(switch)["enabled"] is True

    app_module.options_autopilot_state["enabled"] = False
    assert app_module._autopilot_persist() is True
    assert _read(switch)["enabled"] is False


def test_sin_archivo_el_autopilot_arranca_apagado(switch):
    assert not os.path.exists(switch)
    assert app_module._autopilot_restore() is False
    assert app_module.options_autopilot_state["enabled"] is False


def test_un_archivo_corrupto_no_enciende_el_trading(switch):
    _write(switch, "{esto no es json")

    assert app_module._autopilot_restore() is False
    assert app_module.options_autopilot_state["enabled"] is False


@pytest.mark.parametrize("raw", [
    '{"enabled": "true"}',   # cadena en vez de booleano
    '{"enabled": 1}',        # 1 es truthy en Python, pero no es una decisión
    '{"enabled": null}',
    '{"encendido": true}',   # clave equivocada
    '{"enabled": true',      # truncado a mitad de la escritura
    '[]',                    # JSON valido, forma equivocada
])
def test_solo_un_booleano_explicito_puede_encender_el_trading(switch, raw):
    """Aceptar `1` o `"true"` dejaria que cualquier cosa que quepa en un JSON
    arranque el trading. El archivo puede apagar, nunca prender por su cuenta.
    """
    _write(switch, raw)

    assert app_module._autopilot_restore() is False
    assert app_module.options_autopilot_state["enabled"] is False


def test_el_kill_switch_de_entorno_manda_sobre_el_disco(switch, monkeypatch):
    """Apagar por variables de entorno tiene que seguir apagando, aunque haya
    quedado un archivo viejo diciendo lo contrario."""
    _write(switch, json.dumps({"enabled": True}))
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_ENV_ENABLED", False)

    assert app_module._autopilot_restore() is False
    assert app_module.options_autopilot_state["enabled"] is False


def test_si_el_disco_falla_apagar_sigue_funcionando(switch, tmp_path, monkeypatch):
    """El freno de emergencia no puede depender de poder escribir un archivo."""
    monkeypatch.setattr(app_module, "OPTIONS_AUTOPILOT_STATE_PATH",
                        str(tmp_path / "no_existe" / "state.json"))
    app_module.options_autopilot_state["enabled"] = False

    # No levanta, y avisa devolviendo False en vez de tragarse el error.
    assert app_module._autopilot_persist() is False
    assert app_module.options_autopilot_state["enabled"] is False


def test_persistir_no_deja_temporales_sueltos(switch):
    app_module.options_autopilot_state["enabled"] = True
    assert app_module._autopilot_persist() is True

    sueltos = [p for p in os.listdir(os.path.dirname(switch))
               if p.startswith(".autopilot_")]
    assert sueltos == []
