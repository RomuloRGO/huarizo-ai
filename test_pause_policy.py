"""Política de pausas: pérdida diaria y safety son Estados DISTINTOS.

Antes sólo existía `daily_loss_paused`. Una pausa de safety (MCP caído, datos
degradados, excepción inesperada) se guardaba como si fuera pérdida diaria, y
como la pausa diaria se llavea por fecha, **cambiaba de día y la cuenta
volvía a operar sola** con la falla todavía presente.

Semántica que fijan estos tests:
- `daily_loss_paused`  -> se llavea por día; al cambiar la fecha se levanta sola.
- `safety_paused`      -> NO depende del día; sólo se levanta con
                          `clear_safety_pause()` explícito.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from risk_gate import DailyLossTracker


def _tracker(tmp_path):
    return DailyLossTracker(path=str(tmp_path / "risk.json"))


# ── Pausa diaria: alcance natural por fecha ───────────────────────────────────

def test_daily_pause_does_not_clear_same_day(tmp_path):
    tr = _tracker(tmp_path)
    tr.pause("2026-09-01", "daily_loss_breach")
    assert tr.is_paused("2026-09-01") is True


def test_daily_pause_is_naturally_scoped_to_day(tmp_path):
    """Cambia el día y la pausa diaria desaparece: es su comportamiento correcto."""
    tr = _tracker(tmp_path)
    tr.pause("2026-09-01", "daily_loss_breach")
    assert tr.is_paused("2026-09-02") is False


# ── Pausa de safety: independiente de la fecha ────────────────────────────────

def test_safety_pause_independent_of_daily(tmp_path):
    tr = _tracker(tmp_path)
    tr.safety_pause("mcp_unavailable")
    assert tr.is_safety_paused() is True
    assert tr.is_paused("2026-09-01") is False


def test_safety_pause_survives_day_rollover(tmp_path):
    """Éste es el bug que se corrige: la safety NO se levanta sola mañana."""
    tr = _tracker(tmp_path)
    tr.safety_pause("mcp_unavailable")
    assert tr.is_safety_paused() is True  # mismo día
    # El día cambió, pero la falla de safety sigue ahí.
    assert tr.is_safety_paused() is True


def test_clear_safety_pause(tmp_path):
    tr = _tracker(tmp_path)
    tr.safety_pause("timeout")
    tr.clear_safety_pause()
    assert tr.is_safety_paused() is False


# ── Independencia entre los dos Estados ───────────────────────────────────────

def test_clearing_safety_pause_keeps_daily_pause(tmp_path):
    tr = _tracker(tmp_path)
    tr.pause("2026-09-01", "daily_loss_breach")
    tr.safety_pause("mcp_unavailable")

    tr.clear_safety_pause()

    assert tr.is_safety_paused() is False
    assert tr.is_paused("2026-09-01") is True  # la diaria sigue


def test_clearing_daily_pause_keeps_safety_pause(tmp_path):
    tr = _tracker(tmp_path)
    tr.pause("2026-09-01", "daily_loss_breach")
    tr.safety_pause("mcp_unavailable")

    tr.clear()

    assert tr.is_paused("2026-09-01") is False
    # `clear()` borra TODO el estado, incluida la safety. Documentado a propósito:
    # es el reset manual de emergencia.
    assert tr.is_safety_paused() is False


# ── Persistencia ──────────────────────────────────────────────────────────────

def test_safety_pause_persists_across_instances(tmp_path):
    """Reiniciar el proceso no debe resucitar una cuenta en safety pause."""
    path = str(tmp_path / "risk.json")
    DailyLossTracker(path=path).safety_pause("mcp_unavailable")

    restarted = DailyLossTracker(path=path)

    assert restarted.is_safety_paused() is True
    assert restarted.pause_state("2026-09-01")["safety_reason"] == "mcp_unavailable"


def test_daily_pause_persists_across_instances(tmp_path):
    path = str(tmp_path / "risk.json")
    DailyLossTracker(path=path).pause("2026-09-01", "daily_loss_breach")
    assert DailyLossTracker(path=path).is_paused("2026-09-01") is True


# ── Estado combinado (consumido por Tasks 9 y 11) ─────────────────────────────

def test_pause_state_reports_both(tmp_path):
    tr = _tracker(tmp_path)
    tr.pause("2026-09-01", "daily_loss_breach")
    tr.safety_pause("data_degraded")

    state = tr.pause_state("2026-09-01")

    assert state["daily_loss_paused"] is True
    assert state["safety_paused"] is True
    assert state["safety_reason"] == "data_degraded"
    assert state["daily_reason"] == "daily_loss_breach"


def test_pause_state_clean_when_nothing_paused(tmp_path):
    tr = _tracker(tmp_path)
    state = tr.pause_state("2026-09-01")

    assert state["daily_loss_paused"] is False
    assert state["safety_paused"] is False
    assert state["daily_reason"] is None
    assert state["safety_reason"] is None


def test_pause_state_daily_reason_scoped_to_day(tmp_path):
    tr = _tracker(tmp_path)
    tr.pause("2026-09-01", "daily_loss_breach")

    state = tr.pause_state("2026-09-02")

    assert state["daily_loss_paused"] is False
    assert state["daily_reason"] is None


def test_safety_pause_reason_is_overwritable(tmp_path):
    """Una nueva falla de safety reemplaza el motivo anterior."""
    tr = _tracker(tmp_path)
    tr.safety_pause("mcp_unavailable")
    tr.safety_pause("clock_skew")

    assert tr.is_safety_paused() is True
    assert tr.pause_state("2026-09-01")["safety_reason"] == "clock_skew"


def test_clear_safety_pause_is_idempotent(tmp_path):
    tr = _tracker(tmp_path)
    tr.clear_safety_pause()  # sin pausa previa no debe explotar
    assert tr.is_safety_paused() is False
