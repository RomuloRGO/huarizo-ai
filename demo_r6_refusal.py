"""Demuestra el rechazo por concentracion (R6) SIN enviar ninguna orden.

Sirve para grabar el video del concurso: con una sola ordenada se ve al agente
negarse a abrir una SEGUNDA posicion sobre un subyacente que ya tiene exposicion,
con el desglose completo del motivo.

Es seguro por construccion: solo llama a `risk_gate.evaluate_new_entry`, que es
una funcion pura. No reserva en el ledger, no toca el journal y no abre ninguna
conexion con Alpaca salvo la lectura opcional de posiciones. Si Alpaca no
responde se usa el journal, asi que el demo funciona incluso sin red.

Uso:
    python demo_r6_refusal.py                 # usa el subyacente con mas exposicion
    python demo_r6_refusal.py --ticker SPY    # fuerza un subyacente

Sale con codigo 0 si R6 rechazo la orden (que es el resultado esperado) y 1 si
algo paso el gate, para que se pueda usar como comprobacion en el video.
"""

import argparse
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()

import journal as journal_mod  # noqa: E402
from risk_gate import evaluate_new_entry, option_underlying  # noqa: E402

# <root><YYMMDD><C|P><strike x1000, 8 digitos>
_OCC = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")

# Una prima y un tamano cualquiera pero plausibles: lo que se quiere demostrar es
# el rechazo por CONCENTRACION, no por tamaño, asi que se eligen valores que
# pasarian holgadamente los topes de notional y de poder de compra.
_DEMO_PREMIUM = 3.50
_DEMO_QTY = 1
_EQUITY = 100000.0


def sibling_contract(occ, strike_step=5.0):
    """Mismo ticker, mismo vencimiento, mismo tipo, otro strike.

    Es justo el caso que R6 existe para frenar: dos contratos del mismo nombre
    que para el resto del sistema son dos apuestas independientes.
    """
    m = _OCC.match(str(occ or "").strip().upper())
    if not m:
        return None
    strike = int(m.group("strike")) / 1000.0
    new_strike = strike + strike_step
    return (f"{m.group('root')}{m.group('date')}{m.group('type')}"
            f"{int(round(new_strike * 1000)):08d}")


def book_from_journal():
    """Libro en el formato que espera el gate, derivado del journal.

    Es la misma caida que usa el ciclo autonomo cuando Alpaca no responde, asi
    que lo que se ve en el demo es exactamente lo que evalua el agente.
    """
    try:
        entries = journal_mod.load_journal()
    except Exception as e:
        print(f"[demo_r6] No se pudo leer el journal: {e}")
        return []
    book = []
    for e in entries or []:
        if e.get("status") not in ("open", "closing"):
            continue
        occ = str(e.get("occ") or "").upper()
        if not occ:
            continue
        qty = int(e.get("qty") or 0)
        entry_ask = float(e.get("entry_ask") or 0.0)
        book.append({"symbol": occ, "qty": qty,
                     "market_value": entry_ask * 100.0 * qty})
    return book


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=None,
                    help="Subyacente a forzar; por defecto el que mas posiciones tiene")
    ap.add_argument("--cap", type=int, default=1,
                    help="Tope de posiciones por subyacente (default 1 = R6 tal cual). "
                         "Subirlo sirve de CONTRASTE: demuestra que el rechazo lo pone "
                         "R6 y no otro check del gate.")
    args = ap.parse_args()

    book = book_from_journal()
    if not book:
        print("[demo_r6] No hay posiciones abiertas en el journal: R6 no tiene "
              "nada que rechazar today. Abre una posicion primero.")
        return 1

    counts = {}
    for p in book:
        u = option_underlying(p["symbol"]) or p["symbol"]
        counts[u] = counts.get(u, 0) + 1

    target = (args.ticker or "").strip().upper()
    if not target:
        target = max(counts, key=lambda k: counts[k])

    # Un contrato abierto del subyacente elegido, para derivar vencimiento/tipo.
    seed = next((p["symbol"] for p in book
                 if (option_underlying(p["symbol"]) or p["symbol"]) == target), None)
    if not seed:
        print(f"[demo_r6] El subyacente {target} no esta en cartera. "
              f"Disponibles: {sorted(counts)}")
        return 1

    contender = sibling_contract(seed)
    if not contender:
        print(f"[demo_r6] No pude derivar un contrato hermano de {seed}")
        return 1

    account = {"equity": _EQUITY, "cash": _EQUITY, "buying_power": _EQUITY * 4,
               "options_buying_power": _EQUITY, "status": "ACTIVE"}

    verdict = evaluate_new_entry(
        account=account, open_positions=book, symbol=contender,
        qty=_DEMO_QTY, price=_DEMO_PREMIUM * 100.0,
        limits={"max_positions_per_underlying": args.cap},
    )

    print()
    print("  Libro abierto")
    for p in book:
        print(f"    {p['symbol']}  x{p['qty']:>3}  "
              f"${p['market_value']:,.2f}  -> {option_underlying(p['symbol'])}")
    print(f"    exposicion por subyacente: {counts}")
    print()
    print(f"  Intento de apertura")
    print(f"    {contender}  x{_DEMO_QTY}  @ ${_DEMO_PREMIUM:.2f} "
          f"(notional ${_DEMO_PREMIUM * 100.0 * _DEMO_QTY:,.2f})")
    print()
    print(f"  Veredicto del gate (tope por subyacente = {args.cap})")
    print(f"    allowed = {verdict.get('allowed')}")
    print(f"    reason  = {verdict.get('reason')}")
    for k in ("underlying", "open_in_underlying", "limit", "notional"):
        if k in verdict:
            print(f"    {k:<20} = {verdict[k]}")
    print()

    blocked = verdict.get("reason") == "underlying_concentration_limit"
    if args.cap == 1:
        if blocked:
            print("  [OK] R6 rechazo la orden: el agente se niega a apilar la misma")
            print("       apuesta bajo dos contratos distintos. Ninguna orden salio")
            print("       a Alpaca: `evaluate_new_entry` es una funcion pura.")
            print()
            print("       Contraste para el video:  python demo_r6_refusal.py --cap 5")
            print("       La MISMA orden pasa con el tope subido, lo que demuestra que")
            print("       el rechazo lo pone R6 y no otro check.")
            return 0
        print("  [AVISO] La orden NO fue rechazada por R6. Revisar el libro y el gate.")
        return 1
    # Modo contraste: se espera lo contrario.
    if blocked:
        print(f"  [AVISO] Con tope {args.cap} la orden sigue bloqueada: el subyacente")
        print("          tiene demasiadas posiciones incluso para ese tope.")
        return 1
    print(f"  [OK] Con tope {args.cap} la MISMA orden pasa "
          f"(razon={verdict.get('reason')}).")
    print("       El rechazo anterior lo ponia R6, no otro check del gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
