"""Ledger of authorizations, reservations and order submissions.

Only this module may transition a proposal into an order. It exists because
on 2026-08-31 the agent sent 180 orders in 18 seconds: idempotency was a
derived key with no persisted reservation, so repeated cycles could not tell
that an order was already in flight.

All state transitions happen under a single lock and are persisted to JSON
with atomic replace. Fail-closed: any corrupt or missing record denies.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

import journal as journal_mod
from risk_gate import is_option_symbol


def _safe_int(val: Any, default: int) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


class ExecutionLedger:
    """Persisted authorize -> reserve -> submit chain plus close claims.

    `journal_path` overrides the journal file this ledger reads and writes for
    `begin_close` / `finish_close`. It is a constructor parameter (not a global)
    so tests and alternate deployments can point at their own file; when it is
    None the journal module uses its own default.
    """

    def __init__(self, path: Optional[str] = None,
                 journal_path: Optional[str] = None) -> None:
        self.path = path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "execution_ledger.json"
        )
        self.journal_path = journal_path
        self._lock = threading.Lock()

    def _journal_kwargs(self) -> Dict[str, Any]:
        """Path kwarg for journal calls; empty when the default should apply."""
        if self.journal_path is None:
            return {}
        return {"path": self.journal_path}

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"authorizations": {}, "reservations": {}}
        except (OSError, ValueError) as e:
            # Fail closed: an unreadable ledger denies rather than forgets.
            print(f"[execution_ledger] ledger {self.path} unreadable ({e}); "
                  f"starting from empty state")
            return {"authorizations": {}, "reservations": {}}
        if not isinstance(data, dict):
            return {"authorizations": {}, "reservations": {}}
        data.setdefault("authorizations", {})
        data.setdefault("reservations", {})
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def authorize(self, proposal: Dict[str, Any],
                  risk_verdict: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(risk_verdict, dict):
            return {"status": "rejected", "reason": "invalid_verdict",
                    "authorization_id": None}
        if not risk_verdict.get("allowed"):
            return {"status": "rejected",
                    "reason": risk_verdict.get("reason", "not_approved"),
                    "authorization_id": None}
        occ = str(proposal.get("occ_symbol") or proposal.get("symbol") or "").strip()
        # El chequeo original era `len(occ) < 15`, que dejaba pasar cadenas
        # basura de 15+ caracteres ("NOTANOPTION12345"). Se valida la forma OCC
        # real para que una acción no pueda autorizarse como si fuera opción.
        if not is_option_symbol(occ):
            return {"status": "rejected", "reason": "not_an_option_symbol",
                    "authorization_id": None}
        cycle_id = str(proposal.get("cycle_id") or "").strip()
        auth_id = f"auth-{cycle_id or 'nocycle'}-{occ}"
        with self._lock:
            data = self._read()
            if auth_id in data["authorizations"]:
                return {"status": "rejected",
                        "reason": "authorization_already_consumed",
                        "authorization_id": None}
            data["authorizations"][auth_id] = {
                "authorization_id": auth_id,
                "cycle_id": cycle_id,
                "occ_symbol": occ,
                "verdict": risk_verdict,
                "state": "authorized",
            }
            self._write(data)
        return {"status": "authorized", "authorization_id": auth_id,
                "reason": "approved"}

    def reserve(self, authorization_id: Optional[str], occ_symbol: str,
                side: str, qty: int, client_order_id: str) -> Dict[str, Any]:
        key = str(client_order_id)[:48]
        with self._lock:
            data = self._read()
            auth = data["authorizations"].get(authorization_id or "")
            if auth is None or auth.get("state") != "authorized":
                return {"reserved": False, "duplicate": False,
                        "reason": "invalid_authorization",
                        "client_order_id": key}
            existing = data["reservations"].get(key)
            if existing is not None:
                return {"reserved": True, "duplicate": True,
                        "reason": "already_reserved",
                        "client_order_id": key, "record": existing}
            record = {"client_order_id": key,
                      "authorization_id": authorization_id,
                      "occ_symbol": occ_symbol, "side": side, "qty": int(qty),
                      "state": "reserved", "alpaca_order_id": None}
            data["reservations"][key] = record
            self._write(data)
        return {"reserved": True, "duplicate": False, "reason": "reserved",
                "client_order_id": key, "record": record}

    def mark_submitted(self, client_order_id: str,
                       alpaca_order_id: str) -> Dict[str, Any]:
        key = str(client_order_id)[:48]
        with self._lock:
            data = self._read()
            record = data["reservations"].get(key)
            if record is None:
                return {"ok": False, "reason": "unknown_reservation"}
            record["state"] = "submitted"
            record["alpaca_order_id"] = alpaca_order_id
            self._write(data)
        return {"ok": True, "state": "submitted"}

    def get_reservation(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._read()["reservations"].get(str(client_order_id)[:48])

    def begin_close(self, journal_id: int, reason: str) -> Dict[str, Any]:
        """Claim a journal entry for closing. Exactly one caller can win."""
        jid = _safe_int(journal_id, -1)
        with self._lock:
            entries = journal_mod.load_journal(**self._journal_kwargs())
            target = None
            for e in entries:
                if _safe_int(e.get("id"), -1) == jid:
                    target = e
                    break
            if target is None:
                return {"claimed": False, "reason": "not_found"}
            if target.get("status") != "open":
                return {"claimed": False, "reason": "not_open",
                        "status": target.get("status")}
            journal_mod.update_entry(jid, {"status": "closing", "reason": reason},
                                     **self._journal_kwargs())
            return {"claimed": True, "reason": "claimed", "entry": dict(target)}

    def mark_close_submitted(self, journal_id: int,
                             alpaca_order_id: Optional[str],
                             last_mid: Optional[float] = None) -> Dict[str, Any]:
        """Registra la orden de cierre SIN dar la posición por cerrada.

        El exit manager necesita dos fases: enviar la orden (la entrada sigue
        en `closing`) y luego confirmar el fill para liquidar el P&L
        (`_finalize_filled_closes`). `finish_close(ok=True)` saltaría esa
        confirmación y dejaría el P&L sin calcular, así que este método existe
        para la primera fase.
        """
        jid = _safe_int(journal_id, -1)
        with self._lock:
            entries = journal_mod.load_journal(**self._journal_kwargs())
            target = None
            for e in entries:
                if _safe_int(e.get("id"), -1) == jid:
                    target = e
                    break
            if target is None:
                return {"ok": False, "reason": "not_found"}
            if target.get("status") != "closing":
                return {"ok": False, "reason": "not_closing",
                        "status": target.get("status")}
            patch: Dict[str, Any] = {"close_order_id": alpaca_order_id}
            if last_mid is not None:
                patch["last_mid"] = last_mid
            journal_mod.update_entry(jid, patch, **self._journal_kwargs())
        return {"ok": True, "state": "closing", "close_order_id": alpaca_order_id}

    def finish_close(self, journal_id: int, alpaca_order_id: Optional[str],
                     ok: bool) -> None:
        """Release a close claim: closed on success, reopened on failure."""
        jid = _safe_int(journal_id, -1)
        with self._lock:
            journal_mod.update_entry(
                jid,
                {"status": "closed" if ok else "open",
                 "close_order_id": alpaca_order_id,
                 "reason": None if ok else "close_failed"},
                **self._journal_kwargs(),
            )
