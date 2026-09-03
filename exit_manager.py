"""Exit Manager: OCO casero para opciones (Alpaca no soporta brackets OCC).

Triggers puros testeables + thread daemon que monitorea cada 60s en market hours.
Idempotencia: una posicion marcada 'closing' no vuelve a dispararse.
"""

import math
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

# Import directo (no via modulo) para que los tests puedan parchear
# exit_manager.open_entries / exit_manager.update_entry con unittest.mock.
from journal import load_journal, open_entries, update_entry
from execution_ledger import ExecutionLedger

ET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN_MIN = 9 * 60 + 30
MARKET_CLOSE_MIN = 16 * 60


def _num(val: Any) -> Optional[float]:
    """Convierte a float finito; None ante valores malformados o no finitos."""
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _time_trigger(entry: Dict[str, Any], today: Optional[date]) -> Optional[str]:
    """'time' si hoy >= min_exit_date. None ante fecha ausente/malformada."""
    plan = entry.get("exit_plan", {}) or {}
    raw = plan.get("min_exit_date")
    if today is None or not raw:
        return None
    try:
        min_exit = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return "time" if today >= min_exit else None


def evaluate_exit(mid: float, entry: Dict[str, Any], today: Optional[date]) -> Optional[str]:
    """Retorna 'tp' | 'sl' | 'time' | None. Prioridad tp > sl > time.

    Fail-closed: mid invalido o no finito -> None.
    """
    plan = entry.get("exit_plan", {}) or {}
    try:
        mid_f = float(mid)
        if not (mid_f == mid_f) or mid_f in (float("inf"), float("-inf")):
            return None
    except (TypeError, ValueError):
        return None
    tp_v, sl_v = _num(plan.get("tp_premium")), _num(plan.get("sl_premium"))
    if tp_v is not None and mid_f >= tp_v:
        return "tp"
    if sl_v is not None and mid_f <= sl_v:
        return "sl"
    return _time_trigger(entry, today)


def _is_market_open(now: Optional[datetime] = None) -> bool:
    """Market hours ET (9:30-16:00, Lun-Vie). Feriados exactos fuera de MVP."""
    now = now or datetime.now(ET_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return MARKET_OPEN_MIN <= minutes < MARKET_CLOSE_MIN


# Alias publico: el ciclo de APERTURA de `app` necesita el mismo criterio que
# este daemon, e importar un nombre privado atravesaria la frontera de modulo.
# El nombre privado se conserva porque los tests ya lo referencian.
is_market_open = _is_market_open


def process_open_positions(service: Any, today: Optional[date] = None,
                           ledger: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Un ciclo de monitoreo: dispara closes segun triggers sobre posiciones abiertas.

    El cierre pasa por el execution ledger y se RECLAMA ANTES de enviar la
    orden. El codigo anterior hacia `close_option_position(...)` y solo despues
    marcaba `status=closing`: en esa ventana otro hilo (otro ciclo del daemon o
    un cierre manual desde la UI) veia la entrada todavia `open` y enviaba una
    segunda orden de cierre. Con la reclamacion atomica el segundo llamante
    recibe `claimed=False` y se detiene.
    """
    actions: List[Dict[str, Any]] = []
    today = today or datetime.now(ET_TZ).date()
    if ledger is None:
        ledger = ExecutionLedger()
    positions = open_entries()
    if not positions:
        return actions
    occs = [p["occ"] for p in positions if p.get("occ")]
    quotes = service.get_option_quote(occs)
    for entry in positions:
        try:
            occ = entry.get("occ")
            occ_key = str(occ).upper() if occ else None
            mid = quotes.get(occ_key) if occ_key else None
            reason = evaluate_exit(mid=mid, entry=entry, today=today) if mid is not None else None
            if reason is None:
                reason = _time_trigger(entry, today)
            if reason is None:
                continue
            # 1. Reclamar la entrada (open -> closing) ANTES de tocar Alpaca.
            #    Solo un llamante puede ganar; el resto recibe claimed=False.
            claim = ledger.begin_close(entry["id"], reason)
            if not claim.get("claimed"):
                continue
            # 2. Enviar la orden. Si falla se libera la reclamación y la
            #    entrada vuelve a `open` para reintentar en el próximo ciclo.
            try:
                order = service.close_option_position(occ, int(entry.get("qty", 1)))
            except Exception:
                ledger.finish_close(entry["id"], None, False)
                raise
            # 3. Registrar el id de la orden sin dar la posición por cerrada:
            #    el P&L se liquida cuando el fill se confirma.
            ledger.mark_close_submitted(entry["id"], order.get("id"), last_mid=mid)
            actions.append({"journal_id": entry["id"],
                            "action": "close_submitted", "reason": reason})
        except Exception as e:
            print(f"[exit_manager] Error processing entry {entry.get('id')}: {e}")
            continue
    return actions


class ExitManager:
    """Daemon que corre process_open_positions cada intervalo durante market hours."""

    def __init__(self, service: Any, interval_seconds: int = 60,
                 ledger: Optional[Any] = None):
        self.service = service
        self.interval_seconds = interval_seconds
        # Se inyecta el ledger compartido de la app para que el daemon y el
        # cierre manual compitan por la misma reclamación atómica.
        self.ledger = ledger
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="huarizo-exit-manager")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        print(">> [EXIT MANAGER] Daemon started.")
        while not self._stop.is_set():
            try:
                if _is_market_open():
                    process_open_positions(self.service, ledger=self.ledger)
                    self._finalize_filled_closes()
            except Exception as e:
                print(f"[exit_manager] Error in cycle: {e}")
            self._stop.wait(self.interval_seconds)
        print(">> [EXIT MANAGER] Daemon stopped.")

    def _finalize_filled_closes(self) -> None:
        """Cuando la orden de cierre queda filled, registra exit price y P&L realizado."""
        for entry in load_journal():
            if entry.get("status") != "closing":
                continue
            oid = entry.get("close_order_id")
            if not oid:
                continue
            try:
                order = self.service.get_order(oid)
                status = str(order.get("status", "")).lower()
                if status == "filled":
                    try:
                        exit_price = float(order.get("filled_avg_price", 0.0)
                                           or entry.get("last_mid", 0.0))
                    except (TypeError, ValueError):
                        exit_price = 0.0
                    qty = int(entry.get("qty", 1))
                    entry_price = float(entry.get("entry_ask", 0.0))
                    pnl = round((exit_price - entry_price) * 100.0 * qty, 2)
                    update_entry(entry["id"], {
                        "status": "closed",
                        "ts_exit": datetime.now(ET_TZ).strftime("%Y-%m-%dT%H:%M:%S"),
                        "exit_mid": exit_price,
                        "pnl_usd": pnl,
                    })
                elif status in ("canceled", "expired", "rejected"):
                    # La orden de cierre murio sin ejecutar: reabrir para que los
                    # triggers vuelvan a disparar el proximo ciclo.
                    update_entry(entry["id"], {"status": "open",
                                               "close_order_id": None,
                                               "reason": None})
            except Exception as e:
                print(f"[exit_manager] Error processing entry {entry.get('id')}: {e}")
                continue
