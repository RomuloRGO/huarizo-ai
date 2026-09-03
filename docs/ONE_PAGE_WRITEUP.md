# Huarizo AI — One-Page Write-Up

**Track:** Alpaca AI Trading Agents Hackathon (lablab.ai)
**Account:** brand-new Alpaca paper account `PA3M5SST2YN2`, starting equity **$100,000.00**
**Stack:** Python 3.11 · Flask · Alpaca REST (trading + market data) · **Alpaca MCP Server** · Google Gemini (optional thesis generation)

---

## What it is

Huarizo AI is an autonomous **options-only** trading agent. It is not a chatbot that
suggests trades: it is a pipeline with hard gates, where every order must survive account
validation, a chain of risk limits, and an authorization/reservation ledger before it can
reach Alpaca.

The design came out of a real incident. On 2026-08-31 an earlier version sent **180 orders
in 18 seconds**: idempotency was a derived key with no persisted reservation, so repeated
cycles could not tell that an order was already in flight. Almost every guard described below
exists because of that night.

---

## AI logic — one agent, five mandates

Huarizo is **one autonomous options trader**, split internally into five decision mandates.
Each mandate perceives a different slice of the world, decides a different thing, and **can
veto the cycle on its own**. The split is a design boundary, not five bots: all five share one
state, one ledger and one pause machine.

| # | Mandate | Code | Perceives | Decides | Vetoes when |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 1 | **Scout** (orchestration) | `app.py` cycle, screener | clock, calendar, universe, pause state | which symbols to look at, and when | market closed · no candidates · paused |
| 2 | **Strategist** (signal) | `agent_engine.analyze` | 250 daily bars, snapshot, news, dividends | direction and conviction (0–100) | insufficient data · signal is HOLD |
| 3 | **Pricer** (options + risk) | `options_engine`, `risk_gate` | full chain, realised vol, account | **which contract, and how big** | DTE tier · IV vs RV · theta burn · term structure · liquidity · size 0 |
| 4 | **Broker** (execution) | `execution_ledger`, `journal` | authorised decision, open positions | how to place it without breaking invariants | no reservation · invalid symbol · no buying power |
| 5 | **Sentinel** (exits) | `exit_manager` | open positions, mid prices, expiries | when to close (TP / SL / time stop) | market closed · already closing |

Sentinel is the only mandate that acts continuously and with no human request in the loop;
when it closes, it frees a slot and Scout may open again.

### Strategist — a tournament that admits what it did not measure

Several presets are backtested on the symbol's own history and the winner's signal (`STRONG
BUY` / `ACCUMULATE` / `HOLD` / …) sets direction. Backtest results are only trusted when they
meet explicit validity conditions — minimum 20 trades, commissions and slippage applied, and
out-of-sample confirmation. When they are not met the system says so: win rate and profit
factor render as `not measured` rather than defaulting to a flattering number, and the UI
never invents a metric it did not compute.

An optional Gemini call turns the signal into a natural-language thesis. Every response
carries `thesis_source` (`llm` or `deterministic`): if the model is unavailable the agent
falls back to a deterministic thesis and **says it did**. No silent substitutions.

### Pricer — where the options expertise lives

Direction is not the hard part; expressing it is. The Pricer takes an existing directional
view and asks whether the chain can carry it on acceptable terms. It is deliberately a
**long-premium directional trader** — it buys options, never sells them — so every rule below
is about not overpaying and not bleeding.

| Rule | Parameter | Trader's reasoning |
| :-- | :-- | :-- |
| **R1** DTE preference | sweet ≥ 21, acceptable ≥ 14 | With no preference, selection always landed on 7 DTE: maximum theta and no time for the thesis to work |
| **R2** No rich premium | IV ≤ 1.25 × realised vol (30 sessions) | buying AAPL at IV 0.25 against RV 0.10 is paying an inflated price for movement |
| **R3** Daily bleed cap | theta ≤ 3.0 % of premium per day | 7 DTE burns ~9.3 %/day; 23 DTE burns ~2.6 %/day |
| **R5** Priced-in event | reject if IV(near) > 1.15 × IV(far) | an inverted term structure means the market is pricing an event; buying there eats the vol crush |
| **R6** Concentration | max **1** open position per underlying | two contracts on the same ticker at the same expiry are not a diversified book — they are one bet split into two tickets, with correlation ~1.0 |

R6 is capped at **1**, not 2, because with the DTE pinned at ~5 days every position expires
in the same week by construction: expiry diversification is impossible, so the underlying is
the only free axis left. A cap of 2 would not have stopped the real 2026-09-02 book (2 SPY
calls ≤ 2); a cap of 1 does. Measured on that day's actual trades, R6 would have rejected the
11:08 SPY 767C for $3,700 — entered while the SPY 764C was still open — taking premium at
risk from **$8,708 (8.7 %)** down to **$5,008 (5.0 %)**.

Realised volatility is computed **for free** from the same 250 daily bars already fetched for
the technical score (`std(log returns) × √252`, 30-session window) — no extra API call.

Two failure modes are handled explicitly, and differently:

- **Declared degradation.** If the chain carries no usable greeks, the agent falls back to
  delta + liquidity and *flags it* (`stats.greeks_degraded = True`). It does not fake a
  judgement it cannot make.
- **Firm rejection.** If it *can* price the contracts, a rejection is final — it will not let
  a data-less contract slip through to dodge the rule.

Measured on 2026-09-02, live, on the same data at the same moment:

| Symbol | Before the rules | After |
| :-- | :-- | :-- |
| SPY | 7 DTE · theta 9.14 %/day | 23 DTE · 2.63 %/day |
| AAPL | 7 DTE · theta 9.51 %/day | 23 DTE · 2.38 %/day |
| NVDA | 7 DTE · theta 10.25 %/day | 23 DTE · 2.69 %/day |
| AMZN | 7 DTE · theta 10.70 %/day | 23 DTE · 2.28 %/day |

Enabling the rules was not enough on its own: `expiries_limit=1` enriched only the nearest
expiry, and since `volume` is always 0 on the free plan, the liquidity filter could admit
**only** that expiry. The agent was structurally forced into ~7 DTE and R5 was dead code. The
fix (`dte_sweet_min`) enriches a second, sweet-spot expiry — one extra call per symbol.

### Why this is an agent, and not a script

1. It **perceives its own environment** (chain, account, clock) rather than being handed data.
2. It **decides on explicit, published criteria**, not a hidden `if`.
3. It **acts unsupervised** against a live broker.
4. **It can say no.** This is the strongest argument: a script executes, an agent declines.
   Huarizo rejects symbols for inverted term structure, for theta bleed, or for expensive
   premium — and it **writes the reason down**, in the log and in the returned proposal
   (`filter_stats`), so every refusal is auditable after the fact.
5. It **persists across cycles** and carries state between them.

The expertise shows up as much in what it refuses as in what it trades.

---

## Refusal policy — the audit trail

A trading agent is judged by what it declines as much as by what it buys, so every refusal is
recorded with its cause. The autonomous cycle logs the exact reason
(`[options_autopilot] SPY descartado: …`) and the returned proposal carries a `filter_stats`
block, so any rejection can be replayed afterwards.

A real trace, captured live on 2026-09-02 for SPY:

```text
spot             764.99
chain_size      3978      contracts in the 7-45 DTE window
candidates        10      survived type / DTE / liquidity
priced            10      carried usable greeks
term             applied  near_iv 0.1015 · far_iv 0.1122 · ratio 0.905 · not inverted
rejected_theta     5      6.10, 6.78, 7.57, 8.46, 9.49 %/day  (all five from the 7-DTE expiry)
rejected_iv_rv     0
survivors          5
selected          SPY260925C00767000
                  23 DTE · strike 767 · delta 0.50 · IV 0.108 · theta 2.67 %/day · OI 1,910
```

Read it as a sentence: *of 3,978 contracts, 10 were even worth pricing; the term structure
was normal, so no event veto; five were discarded for burning 6–9 % of premium per day — every
one of them from the 7-DTE expiry; the survivor decays at 2.67 %.* Nothing here is a black
box: the same numbers surface in the API response **and** are rendered on screen as a
*Options screening funnel* above the order button, so the scrutiny is visible while the
decision is being made, not only afterwards in a log.

The funnel also renders on **rejections**, which is where it matters most. A bare
"No proposal" is indistinguishable from a whim; "No proposal — 3,978 contracts screened,
5 dropped on theta burn (worst 9.44 %/day)" is a decision that can be argued with.

The cycle also refuses for portfolio reasons, and logs that too: with 3 positions open it
returns `{'skipped': 'max_positions'}` rather than reaching for a fourth.

When the **risk gate** is what refuses, the UI does the same thing the funnel does for the
screener: it expands the raw reason into the rule behind it, instead of surfacing a bare
`underlying_concentration_limit`. An R6 refusal reads:

> **Risk gate refused this order** — *R6 concentration* — SPY already carries 1 open
> position(s), cap is 1. Two contracts on the same ticker at the same expiry are one bet
> split into two tickets, not a diversified book: correlation is ~1.0, so a 1% move hits both.

---

## Risk gates — exact limits

Evaluated by `risk_gate.evaluate_new_entry` on every new entry. Fail-closed: a missing or
unreadable account snapshot is a rejection, never a pass.

| Gate | Limit |
| :--- | :--- |
| Max simultaneous open positions — **autonomous cycle** | **3** (`app.py`, counts `open` + `closing`) |
| Max simultaneous open positions — `risk_gate` boundary | **6** (the authorised path a human click goes through) |
| Daily loss pause | **5.0 %** of the day's opening equity (persisted to disk, survives restart) |
| Max gross exposure | **100 %** of equity |
| Max position size | **5.0 %** of equity |
| Max notional per order | **$5,000** (absolute ceiling) |
| Max open positions per underlying (**R6**) | **1** — counted over the real book, held positions plus in-flight orders |
| Buying-power buffer | **10 %** left free |
| **Options buying power** | dedicated check; a missing `options_buying_power` denies the order |
| Symbol shape | OCC regex `^[A-Z]{1,6}\d{6,8}[CP]\d{8}$` — an equity ticker cannot pass as an option |
| Contract multiplier | ×100 applied before any notional is compared (1 contract @ $3 = $300, not $3) |

Options selection and exit configuration:

| Parameter | Value |
| :--- | :--- |
| DTE window | 7–45 days |
| **R1** DTE sweet spot / acceptable floor | ≥ 21 days preferred, ≥ 14 acceptable |
| **R2** max IV vs realised vol | 1.25 × RV(30 sessions) |
| **R3** max theta burn | 3.0 % of premium per day |
| **R5** max IV term ratio (near / far) | 1.15 — above it, an event is priced in |
| **R6** max open positions per underlying | 1 (in-flight orders count too) |
| Target delta / max spread / min open interest | 0.40 / 10 % / 100 |
| Risk per trade / absolute max | 1.5 % / 3.0 % of equity |
| Max contracts | 10 |
| Take profit / stop loss / time stop | +50 % / −30 % / 5 DTE before expiry |

**Contest-account lock** (`account_onboarding`): a strict one-time check proves the account is
fresh — equity within ±1 % of $100,000, cash ≥ 0, **zero** open positions, options level ≥ 2,
status ACTIVE and unblocked. After that, re-arming uses a *different, looser* check that
verifies identity and health but lets equity drift. This split is deliberate: a single
"equity ≈ 100k" check would permanently brick the bot after the first drawdown, exactly when
recovery matters most.

---

## Alpaca infrastructure — MCP Server

The contest requires the Alpaca MCP Server or CLI. Huarizo uses the **MCP Server**, over
stdio, through the official `mcp` Python SDK (no extra runtime dependency).

```bash
uvx alpaca-mcp-server          # HUARIZO_ALPACA_MCP_COMMAND, override with HUARIZO_ALPACA_MCP_ARGS
```

The child process is launched with credentials mapped at the boundary — the project keeps
`APCA_*` in `.env`, the MCP server wants `ALPACA_*`:

```text
APCA_API_KEY_ID      -> ALPACA_API_KEY
APCA_API_SECRET_KEY  -> ALPACA_SECRET_KEY
APCA_PAPER           -> ALPACA_PAPER_TRADE
ALPACA_TOOLSETS      =  account,assets,stock-data,options-data,news
```

Secrets travel in the child's environment, never in `argv`. The toolset filter is a
**read-only** subset by default — no order-placing toolset is exposed to the MCP layer.

The account tool is `get_account_info`. Responses are unwrapped from the anti-injection
wrapper (`{"_alpaca_mcp_security": …, "data": …}`) without destroying it. The MCP session is
**reused across calls**: cold start is ~11 s (banner + 72 tools + handshake), warm calls are
~0.15 s, so opening a session per cycle would be unusable inside a 60 s loop.

MCP is an **additional gate, not a replacement**: when the layer is active, every cycle must
obtain a usable snapshot through it before trading. If it fails, the bot enters a persistent
safety pause rather than falling back to REST.

> **Note:** the CLI was also spiked (`alpaca` binary downloaded and executed). MCP was chosen
> because the CLI offers no interactive confirmation and no structured tool surface.
> See `docs/spike-mcp-cli-2026-09-01.md` for the full comparison.

---

## Idempotency

Every order carries a deterministic `client_order_id`:

```text
huarizo-<SYMBOL>-<side>-<strategy>-<bucket>
```

with `bucket = time.time() // 900`. Alpaca rejects duplicate `client_order_id`s, and the
ledger's reservations are keyed on it — so a retry, a double-click, or a repeated autopilot
cycle cannot create a second order. Verified live: a second submission inside the window is
rejected **before** reaching the API, and a re-run of the full flow produced no second order.

*This is also where the end-to-end smoke test earned its keep.* The first run printed
`huarizo-SPY26090-buy-longcall-0` — bucket frozen at `0`, because the options order path never
passed `now_epoch`. A constant key means a contract is vetoed **forever** after its first
order, which would have deadlocked the bot mid-contest. The unit-test suite (559 tests today)
missed it because
the unit tests pass `now_epoch` explicitly and one of them pinned the `bucket=0` default as
"deterministic". Fixed, with a regression test that asserts both ends: duplicate blocked
inside the window, same contract tradable after it.

---

## Contest clock and close plan

| Moment | Peru (UTC−5) | ET | UTC |
|---|---|---|---|
| Submission deadline | Fri 04 10:00 | Fri 04 11:00 | Fri 04 15:00 |
| Automatic time-stop (first open at DTE ≤ 5) | Fri 04 08:30 | Fri 04 09:30 | Fri 04 13:30 |
| Manual close | Thu 03 14:45 | Thu 03 15:45 | Thu 03 19:45 |

Relying on the automatic exit would leave **90 minutes** of margin, and that is the optimistic
number: the time-stop closes with a market order at the open, which is the widest spread of the
day, and it assumes the process is still alive at 09:30 ET. Closing by hand on Thursday leaves
**19 h 15 m** and turns the reported P&L from marks into realised trades.

---

## Honest limitations

- **P&L history is three trades.** The contest account was created on 2026-09-01. Any
  performance figure is a two-day sample of three trades, not evidence of an edge. All three
  positions opened on 2026-09-02 were closed by the exit manager on 2026-09-03 **without any
  human action** — two at take-profit, one at stop-loss:

  | Contract | Qty | Entry | Exit | Reason | Realised P&L |
  |---|---:|---:|---:|---|---:|
  | `SPY260909C00764000` | 1 | 4.16 | 6.77 | `tp` | **+$261** |
  | `SPY260909C00767000` | 10 | 3.70 | 5.54 | `tp` | **+$1,840** |
  | `QQQ260909P00710000` | 7 | 6.56 | 4.57 | `sl` | **−$1,393** |
  | | | | | **Net** | **+$708** |

  Account equity at 2026-09-03 10:12 ET: **$100,807.83 (+0.81 %) from the $100,000 start**,
  entirely in cash (flat). Two of three trades won; the losing one was the largest position by
  premium paid ($4,592) and was cut by the stop at −30 %, exactly as the exit plan specified.
  The net is positive, and it is one day of one account: we are reporting it as a record, not
  as a result.
- **Both wins came from the same direction on the same underlying.** Two of the three positions
  were SPY calls, so this is closer to *one* trade sized twice than to three independent
  signals. R6 — written on 2026-09-02 because of exactly that book — refuses *new* entries
  only. It did not liquidate the exposure that prompted it.
- **R6 diversifies by ticker, not by risk — and I can put a number on it.** R6 caps open
  positions at **1 per underlying**, which is the right axis given the DTE is pinned near 5 and
  therefore every position expires the same week: there is no expiry diversification to be had.
  But "one per underlying" is not "one per bet". Measured on 2026-09-03 over 171 overlapping
  sessions, the daily-return correlation of **SPY vs QQQ is 0.9186** (0.9114 over 60 sessions,
  0.8795 over 20). So when the agent holds a SPY call and a QQQ call — exactly what the book
  looked like at 10:25 ET on 2026-09-03 — R6 sees two underlyings and lets it through, while
  the economically honest description is *one directional bet, sized twice*. The rule is not
  useless: it does block the 2026-09-02 book of two SPY calls three strikes apart, where the
  correlation is effectively 1.0. It just buys less diversification than its name implies.
  Grouping by correlation cluster rather than by ticker is the obvious next version, and it is
  not in this build.
- **Signals without out-of-sample validation are labelled and sized to one contract**, not
  hidden behind a confident number.
- **Greeks are partial, not absent.** Measured live on 2026-09-02 across the 7–45 DTE window:
  `delta` / `theta` / `iv` are populated on **81 % of SPY contracts (3,222 / 3,978)** and
  **64 % of AAPL (714 / 1,114)**. An earlier draft of this write-up said they "come back as
  zeros" — that was wrong, and it understated the agent. The accurate statement: it prices
  what it can price, and where the greeks are missing it falls back to delta + liquidity and
  **declares the degradation** (`stats.greeks_degraded = True`) rather than faking a judgement.
- **The tradable universe is far narrower than the chain suggests.** Measured live on
  2026-09-03 across the 7–45 DTE window on SPY: the chain returns **3,648 contracts**, and
  **zero of them pass the liquidity filter before enrichment**, because `volume` is 0 on every
  single contract (0 of 3,648) and `open_interest` is absent from the snapshot entirely. Only
  after `enrich_chain_with_oi` — which pulls OI for 5 strikes around spot on **one** expiry —
  do **10 of 3,648 contracts (0.27 %)** become eligible, and all 10 sit on the same expiry
  (2026-09-10) out of the 11 available. The liquidity filter is therefore not a filter that
  narrows a wide field: it is the thing that *defines* the field. This is the thinnest input
  in the system and the single thing a paid data plan would most improve.
- **The option contract's `volume` never influences a decision, for two independent reasons.**
  It is not one of the four scoring factors, and it is not in the ranking key
  `(dte_tier, delta_gap, |strike − spot|)`. It *is* written into the liquidity gate as
  `volume > 0 OR open_interest >= 100`, but since it is always 0, that branch never fires and
  open interest carries the check alone.
- **The underlying's volume *does* influence a decision — as a brake, not as a score.**
  Added 2026-09-03, using a series that was already being loaded and then discarded. The last
  session's volume is measured against its 20-session average: `>= 1.2` is `strong`,
  `>= 0.5` is `neutral`, below that is `weak`, and unmeasurable is `unknown`. `strong` and
  `neutral` let the autonomous entry proceed; `weak` and `unknown` block it. **It fails
  closed**: a degraded feed that drops the `Volume` column — which previously raised
  `KeyError` and killed the whole analysis — now yields `unknown` and stops the cycle rather
  than trading blind. It never touches `confidence_score`; a test
  (`test_el_volumen_no_mueve_el_confidence_score`) fails if it ever does.
- **No GEX (gamma exposure), and I am not pretending otherwise.** There is no gamma code in
  this app. GEX needs per-strike open interest *and* gamma across the chain. In the 7–45 DTE
  window the agent trades, the free plan gives OI for **10 of 3,648 contracts** and gamma
  reads 0. A better OI source exists in the repo root — it returned **570 SPY contracts with
  real OI**, 57× what the agent uses — but measured 2026-09-03 its entire coverage sits on
  **2026-09-03 (195) and 2026-09-04 (375)**, i.e. 0–1 DTE, entirely outside the agent's
  window. It would power a GEX for contracts the agent does not trade. Shipping a decorative
  indicator would be worse than shipping none.
- **No earnings calendar.** R5 detects an event *already priced in* via the term structure,
  but does not know the date; there is no explicit blackout around earnings.
- ~~**Concentration is capped by position count, not by underlying.**~~ **Closed on
  2026-09-02 by R6** (max 1 open position per underlying). The original gap was worse than it
  looked: the gate already had a "no duplicates" check, but it compared the **full OCC
  symbol** against the full OCC symbol, and `SPY260909C00764000` ≠ `SPY260909C00767000`. On
  top of that the autonomous cycle passed `open_positions=[]`, so the gate scored every
  candidate against an empty book — R6, `already_in_portfolio`, the global cap and gross
  exposure were all blind on the autonomous path. Both are fixed: the underlying is extracted
  from the OCC symbol, and the cycle now passes the real book (falling back to the journal if
  Alpaca does not answer).
- **Direction comes from the stock, not from the options.** Indicators run on daily bars of
  the underlying; any edge is in the *expression* of the view, not in the view itself.
- The MCP layer is read-only by design. Orders go through the REST trading API, which is the
  path Alpaca documents for order submission.
