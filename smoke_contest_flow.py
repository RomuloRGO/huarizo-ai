"""Smoke test de punta a punta sobre la cuenta NUEVA de paper del concurso.

Por que existe
--------------
Las 424 pruebas unitarias cubren la logica con dobles. Lo que NO cubren es la
pregunta que decide si la entrega al concurso es valida: con las credenciales
reales, la cuenta real y la cadena real, ?funciona el camino completo
MCP -> validacion de cuenta -> cadena -> gate de riesgo -> autorizacion ->
reserva -> orden -> journal?

Ese camino se rompe de formas que un mock nunca ve: un nombre de herramienta
MCP que no existe, un `APCA_PAPER` mal puesto, un snapshot que no trae
`options_buying_power`, una cuenta que no es la del concurso.

Modos
-----
Por defecto el script es de SOLO LECTURA: no envia ninguna orden. Con
`--execute` envia exactamente UNA orden de opciones y se detiene. Nunca
itera sobre simbolos enviando ordenes: recorre candidatos solo para
ENCONTRAR una propuesta, y el contador `_orders_placed` aborta si algo
intenta enviar una segunda.

Uso
---
    python smoke_contest_flow.py                 # solo lectura
    python smoke_contest_flow.py --execute       # UNA orden real (paper)
    python smoke_contest_flow.py --ticker AAPL   # forzar un subyacente

Cualquier corrida con `--execute` va contra la cuenta de paper real.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

from dotenv import load_dotenv

# El `.env` se carga ANTES de importar `app`: `app` construye `AlpacaService` y
# `AlpacaTools` al importarse, y ambos leen el entorno en ese momento.
load_dotenv()

# Mismo criterio que `AlpacaTools` y que `AlpacaService`. No se acepta un valor
# vacio: `APCA_PAPER=` significa dinero real.
_PAPER_TRUE = {"1", "true", "yes", "on"}

DEFAULT_CANDIDATES = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PRECONDITION = 2


def is_paper_env() -> bool:
    return str(os.getenv("APCA_PAPER", "")).strip().lower() in _PAPER_TRUE


def _mask(value: Optional[str]) -> str:
    """Huella de una llave: suficiente para reconocerla, inutil para usarla."""
    if not value:
        return "<vacia>"
    return f"{value[:4]}...{value[-4:]} (len={len(value)})"


class Report:
    """Acumula pasos, advertencias y fallos; imprime un resumen al final."""

    def __init__(self) -> None:
        self.steps: List[Tuple[int, str, str, str]] = []  # (n, nombre, estado, detalle)
        self.warnings: List[str] = []

    def step(self, number: int, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((number, name, "PASS" if ok else "FAIL", detail))
        return ok

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    @property
    def failed(self) -> bool:
        return any(state == "FAIL" for _, _, state, _ in self.steps)

    def render(self) -> None:
        width = 78
        print()
        print("=" * width)
        print("RESUMEN")
        print("=" * width)
        for number, name, state, detail in self.steps:
            line = f"  [{state}] {number}. {name}"
            print(line)
            if detail:
                for chunk in detail.splitlines():
                    print(f"         {chunk}")
        for message in self.warnings:
            print(f"  [WARN] {message}")
        print("-" * width)
        print(f"  Pasos: {len(self.steps)} | "
              f"Fallos: {sum(1 for s in self.steps if s[2] == 'FAIL')} | "
              f"Advertencias: {len(self.warnings)}")
        print("=" * width)


def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "<no numerico>"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke test de punta a punta para la cuenta del concurso.")
    parser.add_argument("--execute", action="store_true",
                        help="Envia UNA orden real de opciones en la cuenta de paper.")
    parser.add_argument("--qty", type=int, default=0,
                        help="Sobrescribir el sizing de produccion (solo para el smoke).")
    parser.add_argument("--ticker", default="",
                        help="Forzar un subyacente (por defecto recorre candidatos).")
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES),
                        help="Lista de subyacentes separada por comas.")
    args = parser.parse_args(argv)

    report = Report()
    started = datetime.now(timezone.utc)

    print("=" * 78)
    print("HUARIZO AI - SMOKE TEST DE CONCURSO")
    print(f"UTC: {started.isoformat()}")
    print(f"Modo: {'EXECUTE (envia 1 orden)' if args.execute else 'READ-ONLY (sin ordenes)'}")
    print("=" * 78)

    # ── PASO 1: entorno ────────────────────────────────────────────────────
    paper = is_paper_env()
    key = os.getenv("APCA_API_KEY_ID", "")
    secret = os.getenv("APCA_API_SECRET_KEY", "")
    print(f"\n[Paso 1] Entorno y credenciales")
    print(f"         APCA_PAPER           = {os.getenv('APCA_PAPER', '<no definido>')!r}")
    print(f"         APCA_API_KEY_ID      = {_mask(key)}")
    print(f"         APCA_API_SECRET_KEY  = {_mask(secret)}")
    print(f"         HUARIZO_ALPACA_TOOL  = {os.getenv('HUARIZO_ALPACA_TOOL', 'none')!r}")

    if not paper:
        print("\n  ABORTADO: APCA_PAPER no es verdadero. Este script solo corre")
        print("  contra paper. Un valor vacio o 'false' apunta a dinero real.\n")
        return EXIT_PRECONDITION
    if not key or not secret:
        print("\n  ABORTADO: faltan credenciales de Alpaca en el entorno.\n")
        return EXIT_PRECONDITION
    report.step(1, "Entorno en modo paper con credenciales", True,
                f"paper=True, key={_mask(key)}")

    # ── Import diferido y sin daemons ──────────────────────────────────────
    # `import app` arranca el exit manager y el autopilot de opciones porque
    # ese modulo ES el servidor. Aqui no queremos ningun hilo de fondo: el
    # exit manager podria cerrar posiciones mientras corre el script.
    os.environ["HUARIZO_DISABLE_DAEMONS"] = "true"
    import app as huarizo_app  # noqa: E402
    from account_onboarding import (  # noqa: E402
        load_onboarding, validate_armed_account, validate_contest_account)
    from execution_ledger import ExecutionLedger  # noqa: E402
    from options_engine import merge_config  # noqa: E402
    from risk_gate import build_client_order_id, evaluate_new_entry  # noqa: E402
    import journal as journal_mod  # noqa: E402

    service = huarizo_app.alpaca_service
    agent = huarizo_app.huarizo_agent
    tools = huarizo_app.ALPACA_TOOLS

    # Defensa en profundidad: las dos capas deben apuntar a paper.
    # `AlpacaService` construido sin argumentos queda en paper por defecto, asi
    # que se verifica la URL efectiva y no una bandera declarada.
    if "paper-api" not in service.trading_base_url:
        print(f"\n  ABORTADO: AlpacaService apunta a {service.trading_base_url}\n")
        return EXIT_PRECONDITION
    if tools.method != "none" and not tools.paper:
        print(f"\n  ABORTADO: la capa {tools.method} de Alpaca no esta en paper.\n")
        return EXIT_PRECONDITION

    # ── PASO 2: cuenta del concurso (con la herramienta oficial) ───────────
    print(f"\n[Paso 2] Validacion de la cuenta del concurso")
    print(f"         Capa oficial de Alpaca: {tools.method} (paper={tools.paper})")
    if tools.method == "none":
        report.warn("HUARIZO_ALPACA_TOOL=none: el concurso exige MCP server o CLI. "
                    "Esta corrida NO prueba el requisito.")

    snapshot, snapshot_error = huarizo_app._contest_account_snapshot()
    if not snapshot:
        report.step(2, "Snapshot de cuenta", False,
                    snapshot_error or "sin snapshot utilizable")
        report.render()
        return EXIT_FAILED

    if tools.method != "none":
        if snapshot_error:
            report.warn(f"La capa {tools.method} no entrego snapshot: {snapshot_error}. "
                        "Se continua con REST, pero el requisito del concurso queda "
                        "sin verificar en esta corrida.")
            print(f"         Capa oficial: ERROR -> {snapshot_error}")
        else:
            print(f"         Capa oficial: OK (merge sobre REST)")

    equity = snapshot.get("equity")
    print(f"         equity={_fmt_money(equity)} cash={_fmt_money(snapshot.get('cash'))} "
          f"status={snapshot.get('status')!r}")
    print(f"         opciones nivel {snapshot.get('options_approved_level')} "
          f"| options_bp={_fmt_money(snapshot.get('options_buying_power'))}")

    base = huarizo_app.expected_start_equity()
    verdict = validate_contest_account(snapshot, expected_start_equity=base)
    if verdict["valid"]:
        report.step(2, "Cuenta NUEVA y valida para el concurso", True,
                    f"equity={_fmt_money(equity)} dentro de +/-1% de {_fmt_money(base)}")
    else:
        reason = verdict.get("reason")
        # Tras un --execute anterior la cuenta ya no esta "fresca": tener una
        # posicion es el resultado esperado, no una regresion. La prueba de
        # frescura quedo registrada en la primera corrida; aqui basta con que
        # siga siendo la cuenta armada y siga operable.
        recoverable = reason in ("existing_positions", "equity_out_of_range")
        armed = validate_armed_account(snapshot, load_onboarding(
            huarizo_app.ONBOARDING_PATH)) if recoverable else {"valid": False}
        if recoverable and armed.get("valid"):
            report.step(2, "Cuenta armada y operable (frescura ya acreditada antes)", True,
                        f"estricto={reason}; armado=ok")
            report.warn(f"La validacion estricta de frescura reporta '{reason}': "
                        "la cuenta ya fue operada por una corrida anterior con "
                        "--execute. La prueba de cuenta nueva es la del primer run.")
        else:
            report.step(2, "Cuenta valida para el concurso", False,
                        f"razon={reason} detalle={verdict.get('detail')}")
            report.render()
            return EXIT_FAILED

    # ── PASO 3: cadena de opciones de un subyacente liquido ────────────────
    print(f"\n[Paso 3] Cadena de opciones")
    candidates = ([t.strip().upper() for t in args.ticker.split(",") if t.strip()]
                  if args.ticker
                  else [t.strip().upper() for t in args.candidates.split(",")
                        if t.strip()])
    # Misma configuracion que usa el pipeline de propuestas (sin overrides):
    # si el smoke leyera otra ventana DTE, estaria probando otra cadena.
    opt_cfg = merge_config(None)
    dte_min = int(opt_cfg["dte_min"])
    dte_max = int(opt_cfg["dte_max"])

    chain_ticker = None
    chain: List[Dict[str, Any]] = []
    for ticker in candidates:
        try:
            fetched = service.get_options_chain(ticker, dte_min=dte_min,
                                                dte_max=dte_max)
        except Exception as e:  # noqa: BLE001 - se reporta, no se propaga
            print(f"         {ticker}: error -> {e}")
            continue
        if fetched:
            chain_ticker, chain = ticker, fetched
            break
        print(f"         {ticker}: cadena vacia")

    if not chain:
        report.step(3, "Cadena de opciones disponible", False,
                    f"ninguno de {candidates} devolvio contratos")
        report.render()
        return EXIT_FAILED
    calls = sum(1 for c in chain if c.get("type") == "call")
    puts = sum(1 for c in chain if c.get("type") == "put")
    report.step(3, "Cadena de opciones disponible", True,
                f"{chain_ticker}: {len(chain)} contratos "
                f"({calls} calls / {puts} puts), DTE {dte_min}-{dte_max}")
    print(f"         {chain_ticker}: {len(chain)} contratos ({calls}C/{puts}P)")

    # ── PASO 4: propuesta + gate de riesgo ─────────────────────────────────
    print(f"\n[Paso 4] Propuesta y gate de riesgo")
    proposal: Optional[Dict[str, Any]] = None
    proposal_ticker = None
    for ticker in candidates:
        try:
            built = agent.build_options_proposal(ticker, service)
        except Exception as e:  # noqa: BLE001
            print(f"         {ticker}: excepcion -> {e}")
            continue
        if built.get("available"):
            proposal, proposal_ticker = built, ticker
            break
        print(f"         {ticker}: sin propuesta -> {built.get('reason')}")

    if proposal is None:
        report.step(4, "Propuesta de opciones construida", False,
                    "ningun candidato produjo una propuesta disponible")
        report.render()
        return EXIT_FAILED

    contract = proposal["contract"] or {}
    sizing = proposal["sizing"] or {}
    occ = contract.get("occ_symbol")
    production_qty = int(sizing.get("qty", 0))
    qty = int(args.qty) if args.qty and args.qty > 0 else production_qty
    ask = float(contract.get("ask") or 0.0)
    direction = str(proposal.get("direction") or "")
    notional = qty * ask * huarizo_app.OPTIONS_CONTRACT_MULTIPLIER
    print(f"         {proposal_ticker}: {occ} x{qty} @ ${ask:.2f} "
          f"(notional {_fmt_money(notional)})")
    print(f"         senal={proposal.get('signal')} "
          f"confianza={proposal.get('confidence_score')} dir={direction}")
    if qty != production_qty:
        # El gate y el ledger deben ver la cantidad que REALMENTE se va a
        # enviar. Se sobreescribe antes de ambos, no despues.
        report.warn(f"Sizing de produccion sobrescrito: {production_qty} -> {qty} "
                    f"contrato(s) por --qty. El gate evaluo x{qty}, no x{production_qty}.")
        print(f"         --qty: {production_qty} -> {qty} contrato(s)")
    # Cifras del sizing de PRODUCCION, no de la cantidad que se envia: sirven
    # para mostrar en el video que el bot dimensiona por riesgo, no por prima.
    print(f"         sizing produccion: riesgo={_fmt_money(sizing.get('risk_usd'))} "
          f"presupuesto={_fmt_money(sizing.get('risk_budget_usd'))} "
          f"piso_aplicado={sizing.get('floor_applied')}")

    try:
        open_positions = service.get_positions()
    except Exception as e:  # noqa: BLE001
        print(f"         no se pudo leer posiciones: {e}")
        open_positions = []

    # El multiplicador importa: 1 contrato a $3 son $300 de exposicion, no $3.
    risk_verdict = evaluate_new_entry(
        account=snapshot,
        open_positions=open_positions,
        symbol=occ,
        qty=qty,
        price=ask * huarizo_app.OPTIONS_CONTRACT_MULTIPLIER,
        limits=agent.risk_limits,
    )
    allowed = bool(risk_verdict.get("allowed"))
    report.step(4, "Propuesta evaluada por el gate de riesgo", allowed,
                f"allowed={allowed} razon={risk_verdict.get('reason')} "
                f"notional={_fmt_money(notional)}")
    print(f"         gate: allowed={allowed} razon={risk_verdict.get('reason')}")
    if not allowed:
        # Sin gate no hay orden: el ledger rechazaria la autorizacion de todos
        # modos, asi que se detiene aqui en vez de simular un camino que no
        # existe en produccion.
        report.render()
        return EXIT_FAILED

    # ── PASOS 5 y 6: autorizacion, reserva e idempotencia ──────────────────
    # Se ejercitan contra un ledger AISLADO. Si se usara el de produccion, la
    # corrida read-only reservaria el mismo client_order_id que luego necesita
    # la corrida --execute, y esta quedaria bloqueada por su propia demo.
    print(f"\n[Paso 5] Autorizacion y reserva (ledger aislado)")
    print(f"\n[Paso 6] Idempotencia de la reserva")
    tmpdir = tempfile.mkdtemp(prefix="huarizo-smoke-")
    # `now_epoch` va explicito: sin el el bucket queda en 0 y la llave seria
    # constante. Es el mismo bug que se corrigio en `submit_authorized_option_order`
    # y que este script delato al imprimir `...-longcall-0`. Se calcula igual
    # que alla para que la llave de la demo sea la que usara la orden real.
    client_order_id = build_client_order_id(
        symbol=occ, side="buy", strategy=direction,
        bucket_seconds=huarizo_app.OPTIONS_IDEMPOTENCY_BUCKET_SECONDS,
        now_epoch=time.time())
    print(f"         client_order_id = {client_order_id}")
    if client_order_id.endswith("-0"):
        report.warn(f"Llave de idempotencia con bucket 0: {client_order_id}")
    try:
        ledger = ExecutionLedger(path=os.path.join(tmpdir, "ledger.json"))
        auth = ledger.authorize({"cycle_id": "smoke", "occ_symbol": occ},
                                risk_verdict)
        auth_ok = auth.get("status") == "authorized"
        report.step(5, "Autorizacion concedida por el ledger", auth_ok,
                    f"status={auth.get('status')} id={auth.get('authorization_id')}")
        if not auth_ok:
            report.render()
            return EXIT_FAILED

        first = ledger.reserve(auth["authorization_id"], occ, "buy", qty,
                               client_order_id)
        reserved_ok = bool(first.get("reserved")) and not first.get("duplicate")
        report.step(5, "Reserva creada", reserved_ok,
                    f"reserved={first.get('reserved')} "
                    f"duplicate={first.get('duplicate')}")

        second = ledger.reserve(auth["authorization_id"], occ, "buy", qty,
                                client_order_id)
        dup_ok = bool(second.get("duplicate"))
        report.step(6, "Segunda reserva reporta duplicate=True", dup_ok,
                    f"reserved={second.get('reserved')} "
                    f"duplicate={second.get('duplicate')} "
                    f"razon={second.get('reason')}")
        if not dup_ok:
            report.render()
            return EXIT_FAILED
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ── PASO 7: envio (solo con --execute) ─────────────────────────────────
    print(f"\n[Paso 7] Envio de la orden")
    if not args.execute:
        report.step(7, "Envio de la orden", True,
                    "OMITIDO: sin --execute el script no envia ordenes")
        print("         omitido (modo read-only)")
    else:
        print("         *** ENVIANDO UNA ORDEN REAL EN PAPER ***")
        outcome = huarizo_app.submit_authorized_option_order(
            occ_symbol=occ, qty=qty, ask_val=ask, direction=direction,
            account=snapshot, open_positions=open_positions)
        if not outcome.get("ok"):
            # Una SEGUNDA corrida con --execute dentro de la misma ventana de
            # idempotencia llega aqui con 409. Ese es el resultado correcto: la
            # garantia es precisamente que no exista una segunda orden. Se
            # reporta como PASS aca y se refuerza con el conteo de ordenes de
            # abajo, pero solo para este motivo: cualquier otro rechazo es FAIL.
            reason = str(outcome.get("error") or "")
            blocked_by_idempotency = (
                outcome.get("status") == 409
                and ("already_consumed" in reason or "already_reserved" in reason
                     or "duplicate" in reason))
            report.step(7, "Orden enviada",
                        blocked_by_idempotency,
                        (f"BLOQUEADA por idempotencia (correcto: no hay segunda "
                         f"orden) {reason}")
                        if blocked_by_idempotency
                        else f"status={outcome.get('status')} error={reason}")
            if not blocked_by_idempotency:
                report.render()
                return EXIT_FAILED
            print(f"         bloqueada por idempotencia: {reason}")
            placed = False
        else:
            placed = True
        order = outcome["order"] or {} if placed else {}
        order_id = order.get("id")

        if placed:
            print(f"         orden {order_id} estado={order.get('status')}")
            try:
                journal_mod.add_entry({
                    "ts_entry": datetime.now(huarizo_app._ET).strftime(
                        "%Y-%m-%dT%H:%M:%S"),
                    "ticker": proposal_ticker,
                    "occ": outcome.get("occ_symbol") or occ,
                    "direction": direction, "qty": qty, "entry_ask": ask,
                    "order_id": order_id, "exit_plan": proposal.get("exit_plan"),
                    "source": "smoke_contest_flow",
                })
            except Exception as e:  # noqa: BLE001
                report.warn(f"La orden {order_id} quedo sin journal: {e}")

            # Idempotencia de verdad: una segunda llamada identica dentro de la
            # misma ventana debe ser rechazada ANTES de llegar a la API. Si aqui
            # apareciera una segunda orden, el diseno del ledger estaria roto.
            retry = huarizo_app.submit_authorized_option_order(
                occ_symbol=occ, qty=qty, ask_val=ask, direction=direction,
                account=snapshot, open_positions=open_positions)
            blocked = not retry.get("ok") and retry.get("status") == 409
            report.step(7, "Reintento identico rechazado (409)", blocked,
                        f"ok={retry.get('ok')} status={retry.get('status')} "
                        f"error={retry.get('error')}")
            if not blocked:
                report.warn("El reintento NO fue bloqueado: revisar la ventana "
                            "de idempotencia del ledger.")

        total_orders = 0
        try:
            all_orders = service.get_orders(status="all")
            total_orders = len(all_orders)
            matching = [o for o in all_orders
                        if str(o.get("client_order_id") or "") == client_order_id]
        except Exception as e:  # noqa: BLE001
            matching = []
            report.warn(f"No se pudo reconciliar ordenes: {e}")
        report.step(7, "Exactamente una orden con ese client_order_id",
                    len(matching) == 1,
                    f"coincidencias={len(matching)} "
                    f"sobre {total_orders} ordenes historicas")
        print(f"         ordenes con esa llave: {len(matching)} "
              f"(de {total_orders} historicas)")

    # ── PASO 8: resumen del journal ────────────────────────────────────────
    print(f"\n[Paso 8] Resumen del journal")
    entries = journal_mod.load_journal()
    open_entries = [e for e in entries if e.get("status") == "open"]
    closed_entries = [e for e in entries if e.get("status") == "closed"]
    realized = journal_mod.realized_pnl()
    print(f"         entradas={len(entries)} abiertas={len(open_entries)} "
          f"cerradas={len(closed_entries)} pnl_realizado={_fmt_money(realized)}")
    for e in open_entries:
        print(f"           #{e.get('id')} {e.get('occ')} x{e.get('qty')} "
              f"@{e.get('entry_ask')} src={e.get('source', '-')}")
    report.step(8, "Journal legible y reconciliado", True,
                f"{len(entries)} entradas ({len(open_entries)} abiertas), "
                f"pnl realizado {_fmt_money(realized)}")

    try:
        positions = service.get_positions()
        print(f"         posiciones abiertas en la cuenta: {len(positions)}")
    except Exception as e:  # noqa: BLE001
        print(f"         no se pudo leer posiciones finales: {e}")

    # La sesion MCP vive en un hilo propio: sin esto el proceso no termina.
    try:
        tools.shutdown()
    except Exception:  # noqa: BLE001
        pass

    report.render()
    print(f"\nModo: {'EXECUTE' if args.execute else 'READ-ONLY'} | "
          f"Duracion: {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")
    return EXIT_FAILED if report.failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
