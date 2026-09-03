# Huarizo AI

Autonomous options-trading agent for Alpaca. Built for the **Alpaca AI Trading
Agents Hackathon** on lablab.ai.

> Paper account ID required by the rule book: **PA3M5SST2YN2** ($100,000 paper
> balance, options level 3, started 2026-09-01).

Huarizo AI runs an options-only pipeline against the Alpaca MCP server: live
OCC chain -> deterministic risk gates (R1-R6) -> idempotent reservation ->
authorization ledger -> single option order. Equity orders are **fail-closed**
at the server (`POST /api/orders/bracket` returns 403 by design). A judge can
re-derive any refusal from the local ledger and the risk_gate verdict;
nothing is hidden behind a model output.

The tournament widget in the dashboard reports a **historical backtest** of
four strategy presets over the same 250 candles. It is context, not a decision:
the tournament does **not** feed the `confidence_score` used by the order
pipeline.

---

## What this submission is, and is not

| | | |
|---|---|---|
| Options-only autonomous flow | Yes | Long call / long put on directional signals, one contract sized to the risk budget |
| Equity bracket orders | No (fail-closed) | `POST /api/orders/bracket` returns 403 unless `HUARIZO_STOCK_AUTOPILOT=true` |
| LLM-driven sizing | Optional | Deterministic by default. Kill switch: `HUARIZO_LLM_PROVIDER=none` |
| Tournament decides the order | No | It reports backtested performance; the order pipeline is separate |
| Idempotent submissions | Yes | 15-min window, server-side 409 on duplicate `client_order_id` |
| Auditable refusals | Yes | `risk_gate` verdict persisted per order attempt in the ledger |

---

## Hard risk gates (R1-R6)

Every proposal is evaluated by a **deterministic** set of gates. The gates run
without the LLM. They refuse to emit a proposal before the network call, so
the API never sees a candidate that already failed.

| Gate | Refuses when | Why |
|---|---|---|
| **R1** Buying power | Notional + margin headroom < premium x qty x 100 | A contract is **100 shares**, not 1 |
| **R2** DTE window | Days-to-expiration outside 7-45 | Too thin to be expressive; too long to be tradeable |
| **R3** Liquidity | Bid/ask spread > 10 % of mid | Mispricing is the enemy, not the spread |
| **R4** Greeks | delta / theta / iv missing on the contract | No decision without a partial greeks surface |
| **R5** Confidence floor | `confidence_score < 0.62` | The model is too uncertain to size at the production budget |
| **R6** Underlying concentration | Already `1` open position in the same underlying | One bet per ticker — no matter how tempting the second strike looks |

R6 is the gate that refused the in-flight duplicate on the live account: the
real book held `{SPY:1, QQQ:1, AAPL:1}` and a second SPY call was rejected
with `allowed=False, reason="underlying_concentration_limit"`. A `--cap 5` flag
on the demo flips the limit and the same proposal returns `allowed=True`,
which makes the gate's causality inspectable.

---

## Architecture

```
                              Alpaca developer stack
                                       |
          +----------------------------+----------------------------+
          |                            |                            |
   TradingClient                  Options snapshots         Corporate actions
   (paper account)                + chain enrichment         (cash dividends)
          |                            |                            |
          +-------------+--------------+--------------+-------------+
                        |                             |
                  Live OCC chain              Alpaca MCP server
                  (greeks, OI)                (stdio, paper mode)
                        |                             |
                        +--------------+--------------+
                                       |
                          Risk gates R1-R6 (deterministic)
                                       |
                          Idempotency check (15-min window)
                                       |
                          Authorization ledger
                                       |
                          Single option order to Alpaca
                                       |
                          App-side exit manager
                          (TP +50 %, SL -30 %, time-stop 5 DTE)
```

The dashboard reads back from the journal to render the **Strategy Tournament
Battle Arena** (historical backtest context), the **Propose Trade** panel
(the gates + R6 in motion), and the **Authorization Ledger** (every order
attempt, with verdict and reason).

---

## Quickstart

```bash
git clone https://github.com/RomuloRGO/huarizo-ai.git
cd huarizo-ai
pip install -r requirements.txt
cp .env.example .env       # fill in APCA paper keys
python app.py              # dashboard at http://localhost:5050
```

`.env` keys actually read by the app:

| Variable | Required | Notes |
|---|---|---|
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Yes | Paper account, level 3 |
| `APCA_PAPER` | Yes | Set to `True` |
| `HUARIZO_ALPACA_TOOL` | Optional | `mcp` (default) or `rest` |
| `HUARIZO_DISABLE_DAEMONS` | Optional | Set `true` to skip the exit manager |
| `HUARIZO_STOCK_AUTOPILOT` | Optional | Default `false`; equity orders are fail-closed |
| `HUARIZO_LLM_PROVIDER` | Optional | `vertex`, `genai`, or `none` (default) |
| `GEMINI_API_KEY` | Optional | Only for the `genai` provider |

To run with the Alpaca MCP server, install it (`uvx alpaca-mcp-server` or
`pip install alpaca-mcp-server`) and export the MCP config. The app discovers
the server over stdio automatically.

---

## Contest smoke test

`pytest` covers the unit logic (597 tests). What it cannot prove is the
question that decides a hackathon entry: against the **real** credentials, the
**real** fresh account and the **real** MCP server, does the whole chain hold
end to end?

`smoke_contest_flow.py` answers that. **Read-only by default** — no orders,
no production ledger writes:

```bash
python smoke_contest_flow.py
```

It runs nine steps. Exit code `0` means every step held; `1` means a step
failed; `2` means a precondition aborted the run.

Expected output (abridged):

```text
[Step 1] Environment & credentials          APCA_PAPER='True'
[Step 2] Account validation                Alpaca official layer: mcp (paper=True)
                                            equity=$100,000.00 status='ACTIVE'
[Step 3] Options chain                     SPY: 3918 contracts (1959 calls / 1959 puts)
[Step 4] Proposal & risk gate              SPY260909C00764000 x10 @ $4.16  allowed=True
[Step 5] Authorization & reservation       client_order_id = huarizo-SPY26090-buy-longcall-1987059
[Step 6] Reservation idempotency           duplicate=True   (second reserve rejected)
[Step 7] Order submission                  SKIPPED: without --execute the script sends nothing
[Step 8] Journal summary                   0 entries (0 open)
[Step 9] R6 refusal probe                  SPY260925C00776000 x1 @ $3.50
                                            allowed=False  underlying_concentration_limit
  Steps: 9 | Failures: 0 | Warnings: 0
```

To send exactly one order:

```bash
python smoke_contest_flow.py --execute
```

Running `--execute` twice inside the same 15-minute idempotency window does
**not** create a second order — the second submission gets a 409 from the
ledger before the network call.

Other flags:

| Flag | Effect |
|---|---|
| `--qty 1` | Override production sizing (1 contract instead of the 10 the risk budget computes) |
| `--ticker AAPL` | Force a single underlying instead of walking candidates |
| `--candidates SPY,QQQ` | Override the default candidate list (`SPY,QQQ,AAPL,MSFT,NVDA`) |
| `--cap 5` | Loosen R6 (max positions per underlying) for the R6 probe only |

A companion script, `demo_r6_refusal.py`, prints the R6 verdict against the
current live book and the `--cap 5` contrast, demonstrating the gate's
causality.

---

## Project layout

```
.
|-- app.py                    Flask dashboard + JSON API
|-- agent_engine.py           Proposal builder (signal -> candidate)
|-- alpaca_service.py         TradingClient wrapper (positions, history)
|-- alpaca_tools.py           Market data, news, dividends, screener
|-- backtest_engine.py        Tournament backtester (historical context)
|-- risk_gate.py              R1-R6 deterministic gates
|-- execution_ledger.py       Per-order authorization + idempotency ledger
|-- exit_manager.py           App-side bracket emulation (TP / SL / time-stop)
|-- journal.py                Options trade journal + realized P&L
|-- options_engine.py         OCC chain enrichment + snapshot logic
|-- llm_provider.py           Optional Vertex / Gemini synthesis layer
|-- technical_engine.py       SMA / EMA / RSI / MACD / ATR / Squeeze
|-- pattern_engine.py         Candlestick + chart pattern detector
|-- smoke_contest_flow.py     End-to-end smoke test
|-- demo_r6_refusal.py        R6 causality demo (live book)
|-- static/                   Dashboard (index.html, app.js, style.css)
|-- docs/
|   |-- ONE_PAGE_WRITEUP.md   Single-page narrative for judges
|   `-- spike-mcp-cli-2026-09-01.md  MCP-vs-CLI eligibility spike (read-only)
|-- test_*.py                 Pytest suite (597 tests: risk gates, idempotency, safety)
`-- tests/js/                 Node tests for the dashboard charts and order review
```

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/account` | Paper account snapshot |
| `GET` | `/api/positions` | Open positions |
| `GET` | `/api/options/chain/<ticker>` | Live OCC chain |
| `POST` | `/api/options/proposal` | Build a proposal and run it through R1-R6 |
| `POST` | `/api/options/order` | Submit one option order (after R1-R6 + idempotency) |
| `GET` | `/api/options/journal` | Trade journal + realized P&L |
| `POST` | `/api/options/close/<id>` | Manual sell-to-close |
| `GET` | `/api/options/autopilot` | Autopilot state |
| `POST` | `/api/options/autopilot` | Toggle the autopilot (kill-switchable) |
| `GET` | `/api/agent/auto-pilot/status` | State + flags used by the dashboard |
| `POST` | `/api/agent/analyze` | Run the agent against a ticker |
| `POST` | `/api/orders/bracket` | **Always returns 403** unless `HUARIZO_STOCK_AUTOPILOT=true` |
| `GET` | `/api/screener/movers` | Top gainers / losers |
| `GET` | `/api/screener/most-actives` | Most active volume |
| `GET` | `/api/market/news/<ticker>` | Benzinga news |
| `GET` | `/api/market/dividends/<ticker>` | Cash dividend corporate actions |
| `GET` | `/api/market/analysis/<ticker>` | Technicals + patterns |

Mutating routes can be locked down at the reverse proxy when exposing the app
publicly. The defaults are: `POST /api/options/order`,
`POST /api/options/close/<id>`, `POST /api/orders/bracket`,
`DELETE /api/orders/<id>`, `POST /api/agent/auto-pilot/start`,
`POST /api/options/autopilot`.

---

## License

MIT.