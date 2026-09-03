/**
 * HUARIZO AI — ALPACA HACKATHON DASHBOARD FRONTEND
 * Native Financial HTML5 Canvas Engine, SPA View Switcher & Transactions Ledger
 */

let currentTicker = "NVDA";
let currentAnalysis = null;
let currentCandles = [];
let currentPlan = null;
let rawOrdersList = [];
let activeTxFilter = "all";
let autoPilotRunning = false;
let autoPilotPollTimer = null;

document.addEventListener("DOMContentLoaded", () => {
  initApp();
});

async function initApp() {
  setupEventListeners();
  setupSpaRouting();

  // Initial Data Loads
  await loadAccount();
  await loadPositions();
  await loadOrdersHistory();
  await loadTickerAnalysis("NVDA");

  // Periodic poll for background status
  pollAutoPilotStatus();
}

// ── 0. SPA VIEW SWITCHER ───────────────────────────────────────────────────

function setupSpaRouting() {
  const navBtns = document.querySelectorAll(".sidebar-nav .nav-item");

  navBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const viewId = btn.getAttribute("data-view");
      switchSpaView(viewId);
    });
  });
}

function switchSpaView(viewId) {
  // Update sidebar active buttons
  document.querySelectorAll(".sidebar-nav .nav-item").forEach((b) => b.classList.remove("active"));
  const activeBtn = document.querySelector(`.sidebar-nav .nav-item[data-view="${viewId}"]`);
  if (activeBtn) activeBtn.classList.add("active");

  // Hide all views, show selected
  document.querySelectorAll(".spa-view").forEach((v) => v.classList.add("hidden"));
  const targetView = document.getElementById(viewId);
  if (targetView) targetView.classList.remove("hidden");

  // Update Page Title
  const title = document.getElementById("page-title");
  const tagline = document.getElementById("page-tagline");

  if (viewId === "view-terminal") {
    title.textContent = "Autonomous Multi-Strategy Trading Agent";
    tagline.textContent = "Real-Time Market Data, Quantitative Confluence & Bracket Orders";
    if (currentCandles.length > 0) {
      setTimeout(() => renderNativeCandlestickChart(currentCandles, currentPlan), 50);
    }
  } else if (viewId === "view-autopilot") {
    title.textContent = "Continuous Autonomous Auto-Pilot Hub";
    tagline.textContent = "Background Multi-Strategy Scanner, Trailing Stops & Execution Stream";
  } else if (viewId === "view-transactions") {
    title.textContent = "Official Trades & Orders Ledger";
    tagline.textContent = "Complete History of Executions, Bracket Orders & Stop-Loss on Alpaca";
    loadOrdersHistory();
  } else if (viewId === "view-options") {
    title.textContent = "Options Alpha Agent";
    tagline.textContent = "Directional Options Signals, Risk-Sized Entries & App-Side Exits";
    optLoadAutopilotStatus();
    optLoadJournal();
  }
}

function setupEventListeners() {
  // Search & Chips
  const input = document.getElementById("ticker-input");
  const btnAnalyze = document.getElementById("btn-analyze");

  btnAnalyze.addEventListener("click", () => {
    const val = input.value.trim().toUpperCase();
    if (val) loadTickerAnalysis(val);
  });

  input.addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      const val = input.value.trim().toUpperCase();
      if (val) loadTickerAnalysis(val);
    }
  });

  document.getElementById("btn-run-backtest").addEventListener("click", runCurrentBacktest);
  const executeBtn = document.getElementById("btn-execute-order");
  executeBtn.addEventListener("click", openOrderReview);
  document.getElementById(REVIEW_IDS.cancel).addEventListener("click", closeOrderReview);
  document.getElementById(REVIEW_IDS.backdrop).addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeOrderReview();
  });
  document.getElementById(REVIEW_IDS.ack).addEventListener("change", (e) => {
    document.getElementById(REVIEW_IDS.confirm).disabled = !e.target.checked;
  });
  document.getElementById(REVIEW_IDS.confirm).addEventListener("click", confirmReviewedOrder);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const backdrop = document.getElementById(REVIEW_IDS.backdrop);
    if (backdrop && !backdrop.classList.contains("hidden")) closeOrderReview();
  });
  setManualExecutionState("no-analysis", "Run an analysis to enable manual execution.");
  document.getElementById("btn-refresh-positions").addEventListener("click", () => {
    loadPositions();
    loadOrdersHistory();
    loadAccount();
  });
  
  const btnRefreshOrders = document.getElementById("btn-refresh-orders");
  if (btnRefreshOrders) {
    btnRefreshOrders.addEventListener("click", () => {
      loadOrdersHistory();
      loadAccount();
    });
  }

  document.getElementById("btn-run-autopilot").addEventListener("click", toggleAutonomousAutoPilot);

  // Transactions Filter Tabs
  document.querySelectorAll("[data-tx-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-tx-filter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      activeTxFilter = btn.getAttribute("data-tx-filter");
      renderFullTransactionsLedger();
    });
  });

  const txSearch = document.getElementById("tx-search-input");
  if (txSearch) {
    txSearch.addEventListener("input", renderFullTransactionsLedger);
  }

  // Window resize for native canvas
  window.addEventListener("resize", () => {
    if (currentCandles.length > 0) {
      renderNativeCandlestickChart(currentCandles, currentPlan);
    }
  });
}

// ── 1. ACCOUNT, POSITIONS & ORDER HISTORY ───────────────────────────────────

async function loadAccount() {
  try {
    const r = await fetch("/api/account");
    const data = await r.json();
    if (data.success && data.account) {
      const a = data.account;
      document.getElementById("val-portfolio").textContent = `$${a.portfolio_value.toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
      document.getElementById("val-cash").textContent = `$${a.cash.toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
      document.getElementById("val-buying-power").textContent = `$${a.buying_power.toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
    }
  } catch (e) {
    console.error("Error loading account:", e);
  }
}

async function loadPositions() {
  try {
    const r = await fetch("/api/positions");
    const data = await r.json();
    const tbody = document.getElementById("positions-tbody");
    tbody.innerHTML = "";

    if (data.success && data.positions && data.positions.length > 0) {
      document.getElementById("val-positions-count").textContent = data.positions.length;
      let totalPl = 0;

      data.positions.forEach((p) => {
        totalPl += p.unrealized_pl;
        const tr = document.createElement("tr");
        const plClass = p.unrealized_pl >= 0 ? "text-green" : "text-red";
        const plPrefix = p.unrealized_pl >= 0 ? "+" : "";

        tr.innerHTML = `
          <td><strong>${p.symbol}</strong></td>
          <td>${p.qty}</td>
          <td>$${p.avg_entry_price.toFixed(2)}</td>
          <td>$${p.current_price.toFixed(2)}</td>
          <td>$${p.market_value.toLocaleString("en-US", { minimumFractionDigits: 2 })}</td>
          <td class="${plClass}">${plPrefix}$${p.unrealized_pl.toFixed(2)}</td>
          <td class="${plClass}">${plPrefix}${p.unrealized_plpc.toFixed(2)}%</td>
          <td><span class="text-muted" title="Closing is manual from the broker: the autonomous flow is options-only">—</span></td>
        `;
        tbody.appendChild(tr);
      });

      const totalPlClass = totalPl >= 0 ? "text-green" : "text-red";
      document.getElementById("val-day-pl").className = `metric-sub ${totalPlClass}`;
      document.getElementById("val-day-pl").textContent = `P&L: ${totalPl >= 0 ? "+" : ""}$${totalPl.toFixed(2)}`;
    } else {
      document.getElementById("val-positions-count").textContent = "0";
      document.getElementById("val-day-pl").textContent = "P&L: $0.00 (0.00%)";
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted">No open positions in Paper Trading portfolio.</td></tr>`;
    }
  } catch (e) {
    console.error("Error loading positions:", e);
  }
}

async function loadOrdersHistory() {
  try {
    const r = await fetch("/api/orders?status=all");
    const data = await r.json();

    if (!data.success || !data.orders || data.orders.length === 0) {
      rawOrdersList = [];
      renderFullTransactionsLedger();
      return;
    }

    rawOrdersList = data.orders;

    // Render Full Ledger View
    renderFullTransactionsLedger();
  } catch (e) {
    console.error("Error loading orders history:", e);
  }
}

function renderFullTransactionsLedger() {
  const tbody = document.getElementById("full-orders-tbody");
  if (!tbody) return;

  const searchVal = (document.getElementById("tx-search-input") ? document.getElementById("tx-search-input").value : "").trim().toUpperCase();

  let filtered = rawOrdersList.filter((o) => {
    // 1. Tab filter
    if (activeTxFilter === "buy" && o.side !== "buy") return false;
    if (activeTxFilter === "sell" && o.side !== "sell") return false;
    if (activeTxFilter === "filled" && o.status !== "filled") return false;

    // 2. Search query filter
    if (searchVal && !o.symbol.toUpperCase().includes(searchVal)) return false;

    return true;
  });

  // Update Summary Stats
  const filledCount = rawOrdersList.filter((o) => o.status === "filled").length;
  const slCount = rawOrdersList.filter((o) => o.side === "sell" && o.status === "filled").length;
  
  if (document.getElementById("tx-total-count")) document.getElementById("tx-total-count").textContent = rawOrdersList.length;
  if (document.getElementById("tx-filled-count")) document.getElementById("tx-filled-count").textContent = filledCount;
  if (document.getElementById("tx-sl-count")) document.getElementById("tx-sl-count").textContent = slCount;

  tbody.innerHTML = "";

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">No trades match the current filters.</td></tr>`;
    return;
  }

  filtered.forEach((o) => {
    const tr = document.createElement("tr");
    const isBuy = o.side === "buy";
    const sideClass = isBuy ? "text-green" : "text-red";
    const sideBadge = `<span class="badge-mini ${sideClass}"><strong>${o.side.toUpperCase()}</strong></span>`;

    let statusBadge = `<span class="text-muted">${o.status}</span>`;
    if (o.status === "filled") statusBadge = `<span class="badge-mini text-green">Filled</span>`;
    else if (o.status === "held" || o.status === "new" || o.status === "accepted") statusBadge = `<span class="badge-mini text-yellow">${o.status}</span>`;
    else if (o.status === "canceled") statusBadge = `<span class="badge-mini text-red">Canceled</span>`;

    const dtStr = o.created_at ? o.created_at.replace("T", " ").substring(0, 19) : "--";
    const fillPrice = o.filled_avg_price ? Number(o.filled_avg_price) : 0;
    const fillPriceStr = fillPrice > 0 ? `$${fillPrice.toFixed(2)}` : "Pending";
    const totalValStr = fillPrice > 0 ? `$${(fillPrice * Number(o.qty)).toLocaleString("en-US", { minimumFractionDigits: 2 })}` : "--";

    tr.innerHTML = `
      <td>${dtStr}</td>
      <td><strong>${o.symbol}</strong></td>
      <td>${sideBadge}</td>
      <td>${Number(o.qty).toLocaleString()}</td>
      <td>${fillPriceStr}</td>
      <td><strong>${totalValStr}</strong></td>
      <td><span class="badge-mini">${o.order_class || "simple"}</span></td>
      <td>${statusBadge}</td>
      <td><button class="btn btn-sm btn-secondary" onclick="viewTickerInTerminal('${o.symbol}')">View Ticker</button></td>
    `;
    tbody.appendChild(tr);
  });
}

function viewTickerInTerminal(sym) {
  switchSpaView("view-terminal");
  document.getElementById("ticker-input").value = sym;
  loadTickerAnalysis(sym);
}

// El cierre de ACCIONES se eliminó: `DELETE /api/positions/<symbol>` nunca
// existió en app.py (devolvía 404) y además la política del hackathon es
// options-only (Task 2). El cierre de opciones se hace desde el journal de
// opciones con `/api/options/close/<journal_id>`, que sí es atómico (Task 4).


let analysisReqSeq = 0;

// Manual execution availability state: no-analysis | loading | ready | unavailable.
function setManualExecutionState(mode, message) {
  const btn = document.getElementById("btn-execute-order");
  const fb = document.getElementById("order-feedback");
  if (btn) btn.disabled = mode !== "ready";
  if (fb && message !== undefined) {
    fb.className = "order-feedback text-muted";
    fb.textContent = message;
  }
}

// ── 3. AI AGENT ANALYSIS & TOURNAMENT ───────────────────────────────────────

async function loadTickerAnalysis(ticker) {
  const seq = ++analysisReqSeq;
  currentTicker = ticker;
  document.getElementById("hero-ticker").textContent = ticker;
  document.getElementById("hero-signal").textContent = "ANALYZING...";
  document.getElementById("hero-confidence").textContent = "Running Tournament...";
  setManualExecutionState("loading", `Analyzing ${ticker}... manual execution disabled.`);

  try {
    const r = await fetch("/api/agent/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: ticker, use_llm: false })
    });
    if (seq !== analysisReqSeq) return;
    const data = await r.json();
    if (seq !== analysisReqSeq) return;

    if (data.success && data.analysis && data.analysis.available) {
      const a = data.analysis;
      currentAnalysis = a;
      currentCandles = a.candles_series || [];
      currentPlan = a.bracket_order_plan || {};
      setManualExecutionState("ready", "");

      // 1. Hero Summary
      document.getElementById("hero-price").textContent = `$${a.price.toFixed(2)}`;
      const chgPrefix = a.change_pct >= 0 ? "+" : "";
      const chgClass = a.change_pct >= 0 ? "text-green" : "text-red";
      document.getElementById("hero-change").className = `change-badge ${chgClass}`;
      document.getElementById("hero-change").textContent = `${chgPrefix}${a.change_pct.toFixed(2)}%`;

      // Signal Badge
      const sigBadge = document.getElementById("hero-signal-badge");
      document.getElementById("hero-signal").textContent = a.signal;
      document.getElementById("hero-confidence").textContent = `Confidence: ${a.confidence_score}%`;

      if (a.signal === "STRONG BUY" || a.signal === "ACCUMULATE") {
        sigBadge.style.borderColor = "var(--alpaca-green)";
        sigBadge.style.backgroundColor = "var(--alpaca-green-dim)";
        document.getElementById("hero-signal").style.color = "var(--alpaca-green)";
      } else if (a.signal === "REDUCE / SELL") {
        sigBadge.style.borderColor = "var(--alpaca-red)";
        sigBadge.style.backgroundColor = "var(--alpaca-red-dim)";
        document.getElementById("hero-signal").style.color = "var(--alpaca-red)";
      } else {
        sigBadge.style.borderColor = "var(--alpaca-yellow)";
        sigBadge.style.backgroundColor = "var(--alpaca-yellow-dim)";
        document.getElementById("hero-signal").style.color = "var(--alpaca-yellow)";
      }

      // Tournament winner pill
      if (a.winning_strategy) {
        document.getElementById("hero-strategy-name").textContent = a.winning_strategy.name;
        document.getElementById("hero-strategy-stats").textContent = `${a.winning_strategy.win_rate}% Win Rate · ${a.winning_strategy.profit_factor}x PF`;
      }

      // 2. Render Native HTML5 Canvas Candlestick Chart
      renderNativeCandlestickChart(currentCandles, currentPlan);

      // 3. Render Strategy Tournament Battle Arena
      renderTournamentArena(a.tournament, a.winning_strategy);

      // 4. AI Thesis
      document.getElementById("thesis-headline").textContent = a.headline || `Huarizo AI Quantitative Verdict: ${a.signal}`;
      const tList = document.getElementById("thesis-points");
      tList.innerHTML = "";
      (a.thesis_points || []).forEach((pt) => {
        const li = document.createElement("li");
        li.textContent = pt;
        tList.appendChild(li);
      });

      // 5. Technical Matrix
      const t = a.technical || {};
      document.getElementById("val-trend").textContent = t.trend ? t.trend.regime : "Neutral";
      document.getElementById("val-rsi").textContent = OrderReview.formatRsiState(t);
      if (t.stochastic) {
        document.getElementById("val-stoch").textContent = `${t.stochastic.k.toFixed(1)} / ${t.stochastic.d.toFixed(1)} (${t.stochastic.zone})`;
      }
      document.getElementById("val-squeeze").textContent = t.volatility ? t.volatility.squeeze_status : "--";
      
      const cp = a.patterns ? a.patterns.candle : null;
      document.getElementById("val-candle-pattern").textContent = cp ? `${cp.pattern} (${cp.type})` : "Consolidation";
      const chp = a.patterns ? a.patterns.chart : null;
      document.getElementById("val-chart-pattern").textContent = chp ? chp.name : "Range / Channel";

      // 6. Live News
      renderNews(a.news || []);

      // 7. Calibrated Bracket Order Form
      if (a.bracket_order_plan) {
        document.getElementById("order-tp").value = `$${a.bracket_order_plan.take_profit_price.toFixed(2)}`;
        document.getElementById("order-sl").value = `$${a.bracket_order_plan.stop_loss_price.toFixed(2)}`;
        document.getElementById("chart-tag-tp").textContent = `Take-Profit: $${a.bracket_order_plan.take_profit_price.toFixed(2)}`;
        document.getElementById("chart-tag-sl").textContent = `Stop-Loss: $${a.bracket_order_plan.stop_loss_price.toFixed(2)}`;
      }

    } else {
      currentAnalysis = null;
      currentPlan = null;
      document.getElementById("hero-signal").textContent = "UNAVAILABLE";
      document.getElementById("hero-confidence").textContent = data.error || "No data in Alpaca.";
      setManualExecutionState("unavailable", `Analysis unavailable: ${data.error || "No data in Alpaca."}`);
    }
  } catch (e) {
    currentAnalysis = null;
    currentPlan = null;
    console.error("Error analyzing ticker:", e);
    setManualExecutionState("unavailable", "Analysis failed — check your connection and retry.");
  }
}

// ── 4. NATIVE HIGH-PERFORMANCE HTML5 CANVAS CANDLESTICK ENGINE ──────────────

function renderNativeCandlestickChart(candles, plan) {
  const canvas = document.getElementById("native-candlestick-canvas");
  const wrapper = document.getElementById("chart-wrapper");
  const tooltip = document.getElementById("chart-tooltip");
  if (!canvas || !wrapper) return;

  if (!candles || candles.length === 0) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#64748B";
    ctx.font = "12px monospace";
    ctx.fillText("No historical candle data available for this symbol.", 20, 50);
    return;
  }

  // High-DPI support
  const dpr = window.devicePixelRatio || 1;
  const width = wrapper.clientWidth || 600;
  const height = wrapper.clientHeight || 250;

  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const paddingTop = 25;
  const paddingBottom = 25;
  const paddingRight = 65;
  const paddingLeft = 10;
  const plotWidth = width - paddingLeft - paddingRight;
  const plotHeight = height - paddingTop - paddingBottom;

  // Compute price ranges
  let minPrice = Infinity;
  let maxPrice = -Infinity;

  candles.forEach((c) => {
    if (c.low < minPrice) minPrice = c.low;
    if (c.high > maxPrice) maxPrice = c.high;
  });

  if (plan && plan.take_profit_price) {
    if (plan.take_profit_price > maxPrice) maxPrice = plan.take_profit_price * 1.01;
  }
  if (plan && plan.stop_loss_price) {
    if (plan.stop_loss_price < minPrice) minPrice = plan.stop_loss_price * 0.99;
  }

  // Margin buffer
  const priceRange = (maxPrice - minPrice) || 1;
  minPrice -= priceRange * 0.05;
  maxPrice += priceRange * 0.05;
  const finalRange = maxPrice - minPrice;

  function getY(p) {
    return paddingTop + plotHeight - ((p - minPrice) / finalRange) * plotHeight;
  }

  // 1. Draw Grid Lines
  ctx.fillStyle = "#07090E";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "#1A212E";
  ctx.lineWidth = 1;

  const numGrid = 5;
  ctx.fillStyle = "#64748B";
  ctx.font = "10px monospace";
  ctx.textAlign = "left";

  for (let i = 0; i <= numGrid; i++) {
    const p = minPrice + (finalRange / numGrid) * i;
    const y = getY(p);
    ctx.beginPath();
    ctx.moveTo(paddingLeft, y);
    ctx.lineTo(width - paddingRight, y);
    ctx.stroke();

    ctx.fillText(`$${p.toFixed(2)}`, width - paddingRight + 5, y + 3);
  }

  // 2. Draw Candlesticks
  const n = candles.length;
  const candleWidth = Math.max(2, (plotWidth / n) * 0.7);
  const gap = plotWidth / n;

  candles.forEach((c, idx) => {
    const x = paddingLeft + idx * gap + gap / 2;
    const isUp = c.close >= c.open;
    const color = isUp ? "#00D084" : "#FF4D4D";

    // Wick
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(x, getY(c.high));
    ctx.lineTo(x, getY(c.low));
    ctx.stroke();

    // Body
    const yOpen = getY(c.open);
    const yClose = getY(c.close);
    const bodyTop = Math.min(yOpen, yClose);
    const bodyHeight = Math.max(2, Math.abs(yClose - yOpen));

    ctx.fillStyle = color;
    ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
  });

  // 3. Draw Take-Profit Line (Green Dashed)
  if (plan && plan.take_profit_price) {
    const yTP = getY(plan.take_profit_price);
    ctx.strokeStyle = "#00D084";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(paddingLeft, yTP);
    ctx.lineTo(width - paddingRight, yTP);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = "#00D084";
    ctx.font = "bold 9px monospace";
    ctx.fillText(`TP $${plan.take_profit_price.toFixed(2)}`, width - paddingRight + 5, yTP + 3);
  }

  // 4. Draw Stop-Loss Line (Red Dashed)
  if (plan && plan.stop_loss_price) {
    const ySL = getY(plan.stop_loss_price);
    ctx.strokeStyle = "#FF4D4D";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(paddingLeft, ySL);
    ctx.lineTo(width - paddingRight, ySL);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = "#FF4D4D";
    ctx.font = "bold 9px monospace";
    ctx.fillText(`SL $${plan.stop_loss_price.toFixed(2)}`, width - paddingRight + 5, ySL + 3);
  }

  // 5. Interactive Mouse Hover Crosshair & Tooltip
  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const candleIdx = Math.floor((mouseX - paddingLeft) / gap);

    if (candleIdx >= 0 && candleIdx < candles.length) {
      const c = candles[candleIdx];
      const chg = ((c.close - c.open) / c.open) * 100;
      const chgColor = chg >= 0 ? "#00D084" : "#FF4D4D";

      tooltip.classList.remove("hidden");
      tooltip.innerHTML = `
        <strong>${c.time}</strong> | O: $${c.open.toFixed(2)} H: $${c.high.toFixed(2)} L: $${c.low.toFixed(2)} C: <strong>$${c.close.toFixed(2)}</strong> (<span style="color:${chgColor}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>)
      `;
    }
  };

  canvas.onmouseleave = () => {
    tooltip.classList.add("hidden");
  };
}

// ── 5. STRATEGY TOURNAMENT ARENA ────────────────────────────────────────────

function renderTournamentArena(tournamentData, winner) {
  const container = document.getElementById("tournament-bars-container");
  if (!container) return;
  container.innerHTML = "";

  let list = [];
  if (tournamentData && Array.isArray(tournamentData.tournament) && tournamentData.tournament.length > 0) {
    list = [...tournamentData.tournament];
  } else {
    // Antes este bloque inyectaba una tabla hardcodeada (win_rate 51.6,
    // composite_score 40.5…) y coronaba un "WINNER #1" sobre datos que nadie
    // había calculado. Si no hay torneo real, se muestra el estado vacío.
    container.innerHTML =
      '<div class="tournament-empty">No valid tournament: at least ' +
      '20 trades and out-of-sample validation are required.</div>';
    return;
  }

  // Sort descending by historical Composite Score
  list.sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0));

  const maxScore = Math.max(...list.map((x) => x.composite_score || 1), 50);

  list.forEach((item, idx) => {
    // Sólo se corona si el backend lo marcó elegible (Task 6: muestra mínima,
    // OOS y profit factor definido). Sin elegibilidad, ninguna barra es
    // "ganadora": el orden no es evidencia de nada.
    const isWinner = idx === 0 && item.eligibility && item.eligibility.eligible === true;
    const barElem = document.createElement("div");
    barElem.className = "strat-bar-item";

    const winnerBadge = isWinner
      ? `<span class="text-yellow">Validated winner</span>`
      : `<span class="text-muted">#${idx + 1}</span>`;
    const scoreVal = Number(item.composite_score || 0).toFixed(1);
    const winRateVal = Number(item.win_rate || 0).toFixed(1);
    // Desde la Task 6 profit_factor puede ser null (sin pérdidas => indeterminado).
    // Un "1.00x" por defecto sería inventar el dato otra vez.
    const pfVal = (item.profit_factor === null || item.profit_factor === undefined)
      ? "n/d" : `${Number(item.profit_factor).toFixed(2)}x`;
    const tradesVal = item.total_trades !== undefined ? `${item.total_trades} trades` : '';

    // Relative bar width
    const pctWidth = Math.min(100, Math.max(12, ((item.composite_score || 0) / maxScore) * 100));

    barElem.innerHTML = `
      <div class="strat-bar-header">
        <span>${isWinner ? '»' : '•'} <strong>${item.name}</strong> ${winnerBadge}</span>
        <span class="text-muted">${winRateVal}% Win Rate · ${pfVal}x PF · ${tradesVal} · <strong>Score: ${scoreVal}</strong></span>
      </div>
      <div class="strat-bar-track">
        <div class="strat-bar-fill ${isWinner ? 'winner' : ''}" style="width: ${pctWidth}%;"></div>
      </div>
    `;

    container.appendChild(barElem);
  });
}

// ── 6. BACKTEST SIMULATOR & EQUITY CANVAS ───────────────────────────────────

async function runCurrentBacktest() {
  const stratKey = document.getElementById("backtest-strategy-select").value;
  const btn = document.getElementById("btn-run-backtest");
  btn.textContent = "Running Backtest...";

  try {
    // Ruta corregida: `/api/backtest/run` no existe en app.py y devolvía 404.
    const r = await fetch("/api/agent/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: currentTicker, strategy_key: stratKey, days: 250 })
    });
    const data = await r.json();
    btn.textContent = "Run Strategy Backtest";

    if (data.success && data.results) {
      const res = data.results;
      document.getElementById("backtest-results-container").classList.remove("hidden");

      document.getElementById("bt-winrate").textContent = `${res.win_rate.toFixed(1)}%`;
      document.getElementById("bt-profitfactor").textContent = `${res.profit_factor.toFixed(2)}x`;
      document.getElementById("bt-return").textContent = `${res.total_return_pct >= 0 ? '+' : ''}${res.total_return_pct.toFixed(2)}%`;
      document.getElementById("bt-drawdown").textContent = `-${res.max_drawdown_pct.toFixed(2)}%`;

      const diff = res.total_return_pct - res.buy_and_hold_return_pct;
      const diffClass = diff >= 0 ? "text-green" : "text-red";
      document.getElementById("bt-vs-bh").className = diffClass;
      document.getElementById("bt-vs-bh").textContent = `${diff >= 0 ? '+' : ''}${diff.toFixed(2)}% vs B&H (${res.buy_and_hold_return_pct.toFixed(2)}%)`;

      renderEquityCurve(res.equity_curve || []);
    }
  } catch (e) {
    btn.textContent = "Run Strategy Backtest";
    console.error("Error running backtest:", e);
  }
}

function renderEquityCurve(curve) {
  const container = document.getElementById("equity-chart-box");
  if (!container) return;

  if (!curve || curve.length === 0) {
    container.innerHTML = `<div class="text-muted text-center" style="font-size:0.75rem; padding-top:2rem;">No trades generated in backtest period.</div>`;
    return;
  }

  container.innerHTML = `<canvas id="equity-canvas" style="width:100%; height:100%;"></canvas>`;
  const canvas = document.getElementById("equity-canvas");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = container.clientWidth * dpr;
  canvas.height = 110 * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const w = container.clientWidth;
  const h = 110;

  let minE = Math.min(...curve);
  let maxE = Math.max(...curve);
  if (minE === maxE) { minE *= 0.95; maxE *= 1.05; }
  const range = maxE - minE;

  ctx.strokeStyle = "#FFC72C";
  ctx.lineWidth = 2;
  ctx.beginPath();

  curve.forEach((val, idx) => {
    const x = (idx / (curve.length - 1)) * w;
    const y = h - ((val - minE) / range) * (h - 20) - 10;
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Final value badge
  const finalVal = curve[curve.length - 1];
  ctx.fillStyle = finalVal >= curve[0] ? "#00D084" : "#FF4D4D";
  ctx.font = "bold 10px monospace";
  ctx.fillText(`$${Math.round(finalVal).toLocaleString()}`, w - 60, 15);
}

// ── 7. CONTINUOUS AUTO-PILOT DAEMON CONTROLS ────────────────────────────────

async function toggleAutonomousAutoPilot() {
  const btn = document.getElementById("btn-run-autopilot");

  if (!autoPilotRunning) {
    const intervalSec = parseInt(document.getElementById("autopilot-interval-select").value) || 60;
    const autoSubmit = document.getElementById("autopilot-submit-toggle").checked;

    btn.disabled = true;
    btn.innerHTML = `<span>Starting Auto-Pilot...</span>`;

    try {
      const r = await fetch("/api/agent/auto-pilot/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interval_seconds: intervalSec, auto_submit: autoSubmit })
      });
      const data = await r.json();
      btn.disabled = false;

      if (data.success) {
        autoPilotRunning = true;
        btn.className = "btn btn-secondary btn-block";
        btn.innerHTML = `<span>Stop Continuous Auto-Pilot</span>`;
        const badge = document.getElementById("autopilot-status-badge") || document.getElementById("hub-autopilot-badge");
        if (badge) {
          badge.textContent = `Running (${intervalSec}s Loop)`;
          badge.className = "badge-mini text-green";
        }
        pollAutoPilotStatus();
      }
    } catch (e) {
      btn.disabled = false;
      btn.innerHTML = `<span>Start Continuous Auto-Pilot</span>`;
      console.error("Error starting auto-pilot:", e);
    }
  } else {
    btn.disabled = true;
    btn.innerHTML = `<span>Stopping...</span>`;

    try {
      const r = await fetch("/api/agent/auto-pilot/stop", { method: "POST" });
      const data = await r.json();
      btn.disabled = false;

      if (data.success) {
        autoPilotRunning = false;
        btn.className = "btn btn-primary btn-block";
        btn.innerHTML = `<span>Start Continuous Auto-Pilot</span>`;
        const badge = document.getElementById("autopilot-status-badge") || document.getElementById("hub-autopilot-badge");
        if (badge) {
          badge.textContent = `Idle`;
          badge.className = "badge-mini text-cyan";
        }
      }
    } catch (e) {
      btn.disabled = false;
      console.error("Error stopping auto-pilot:", e);
    }
  }
}

// Refleja en la interfaz la política options-only. El backend rechaza las
// órdenes de acciones con 403 mientras el autopilot de acciones esté apagado,
// así que el panel se pinta DESACTIVADO por defecto y solo se habilita si el
// servidor confirma la bandera. Nunca al revés: un botón que parece vivo y
// responde 403 es peor que uno deshabilitado con el motivo a la vista.
function applyStockOrderPolicy(enabled) {
  const panel = document.getElementById("stock-order-panel");
  const inputs = document.getElementById("stock-order-inputs");
  const tag = document.getElementById("stock-order-tag");
  const heading = document.getElementById("stock-order-heading");
  const note = document.getElementById("stock-order-note");
  const btn = document.getElementById("btn-execute-order");
  const btnLabel = document.getElementById("btn-execute-order-label");
  if (!panel || !btn) return;

  if (enabled) {
    panel.classList.remove("is-disabled");
    if (inputs) inputs.classList.remove("is-disabled");
    if (tag) { tag.textContent = "Paper Trading Live"; tag.classList.remove("order-tag-disabled"); }
    if (heading) heading.textContent = "Calibrated Bracket Order Execution";
    if (note) note.hidden = true;
    btn.disabled = false;
    if (btnLabel) btnLabel.textContent = "Submit Bracket Order";
  } else {
    panel.classList.add("is-disabled");
    if (inputs) inputs.classList.add("is-disabled");
    if (tag) { tag.textContent = "Fail-closed · options-only"; tag.classList.add("order-tag-disabled"); }
    if (heading) heading.textContent = "Stock Bracket Orders — Disabled";
    if (note) note.hidden = false;
    btn.disabled = true;
    if (btnLabel) btnLabel.textContent = "Equity Orders Disabled";
  }
}

async function pollAutoPilotStatus() {
  try {
    const r = await fetch("/api/agent/auto-pilot/status");
    const data = await r.json();

    if (data.success) {
      // La bandera viaja en la misma respuesta que ya se consulta cada 3 s:
      // no hace falta un endpoint nuevo ni otro temporizador.
      applyStockOrderPolicy(data.stock_autopilot_enabled === true);
      autoPilotRunning = data.running;
      const btn = document.getElementById("btn-run-autopilot");
      const badge = document.getElementById("autopilot-status-badge") || document.getElementById("hub-autopilot-badge");

      if (data.running) {
        if (btn) {
          btn.className = "btn btn-secondary btn-block";
          btn.innerHTML = `<span>Stop Continuous Auto-Pilot</span>`;
        }
        if (badge) {
          badge.textContent = `Running (${data.interval_seconds}s Loop)`;
          badge.className = "badge-mini text-green";
        }
      } else {
        if (btn) {
          btn.className = "btn btn-primary btn-block";
          btn.innerHTML = `<span>Start Continuous Auto-Pilot</span>`;
        }
        if (badge) {
          badge.textContent = `Idle`;
          badge.className = "badge-mini text-cyan";
        }
      }

      // Update logs in real time
      if (data.logs && data.logs.length > 0) {
        const logBox = document.getElementById("autopilot-logs");
        if (logBox) {
          logBox.innerHTML = "";
          data.logs.slice(-30).forEach((l) => {
            const entry = document.createElement("div");
            entry.className = "autopilot-log-entry";
            if (l.includes("ORDER SENT")) entry.style.color = "var(--alpaca-green)";
            else if (l.includes("Signal: STRONG BUY") || l.includes("Signal: ACCUMULATE")) entry.style.color = "var(--alpaca-yellow)";
            else if (l.includes("Error")) entry.style.color = "var(--alpaca-red)";
            else if (l.includes("TRAILING STOP")) entry.style.color = "var(--ai-cyan)";
            entry.textContent = l;
            logBox.appendChild(entry);
          });
          logBox.scrollTop = logBox.scrollHeight;
        }
      }
    }
  } catch (e) {
    console.error("Error polling auto-pilot status:", e);
  }

  // Re-poll every 3 seconds
  clearTimeout(autoPilotPollTimer);
  autoPilotPollTimer = setTimeout(pollAutoPilotStatus, 3000);
}

// ── 8. 1-CLICK BRACKET ORDER EXECUTION (SAFETY GATE) ────────────────────────

const REVIEW_IDS = {
  backdrop: "order-review-backdrop",
  ticker: "order-review-ticker",
  side: "order-review-side",
  qty: "order-review-qty",
  entry: "order-review-entry",
  notional: "order-review-notional",
  sl: "order-review-sl",
  tp: "order-review-tp",
  maxLoss: "order-review-maxloss",
  gain: "order-review-gain",
  rr: "order-review-rr",
  ack: "order-review-ack",
  error: "order-review-error",
  cancel: "order-review-cancel",
  confirm: "order-review-confirm"
};

let reviewSubmitting = false;
let reviewOpener = null;
let reviewQty = null;
let reviewSnapshot = null;

function money(v) {
  return "$" + Number(v).toFixed(2);
}

function openOrderReview() {
  if (!currentAnalysis || !currentPlan) return;
  const qtyRaw = document.getElementById("order-qty").value;
  const res = OrderReview.computeOrderReview({
    analysis: currentAnalysis,
    plan: currentPlan,
    qtyRaw: qtyRaw,
    ticker: currentTicker
  });
  const fb = document.getElementById("order-feedback");
  if (!res.ok) {
    fb.className = "order-feedback text-red";
    fb.textContent = res.error;
    return;
  }
  const r = res.review;
  const el = (id) => document.getElementById(id);
  el(REVIEW_IDS.ticker).textContent = r.ticker;
  el(REVIEW_IDS.side).textContent = r.side.toUpperCase();
  el(REVIEW_IDS.qty).textContent = String(r.qty);
  el(REVIEW_IDS.entry).textContent = money(r.entryPrice);
  el(REVIEW_IDS.notional).textContent = money(r.notional);
  el(REVIEW_IDS.sl).textContent = money(r.stopLoss);
  el(REVIEW_IDS.tp).textContent = money(r.takeProfit);
  el(REVIEW_IDS.maxLoss).textContent = money(r.maxLoss);
  el(REVIEW_IDS.gain).textContent = money(r.potentialGain);
  el(REVIEW_IDS.rr).textContent = r.riskReward.toFixed(2);
  el(REVIEW_IDS.ack).checked = false;
  el(REVIEW_IDS.confirm).disabled = true;
  el(REVIEW_IDS.cancel).disabled = false;
  el(REVIEW_IDS.error).classList.add("hidden");
  el(REVIEW_IDS.error).textContent = "";
  reviewQty = r.qty;
  reviewSnapshot = { ticker: r.ticker, qty: r.qty, tp: currentPlan.take_profit_price, sl: currentPlan.stop_loss_price };
  reviewOpener = document.activeElement;
  el(REVIEW_IDS.backdrop).classList.remove("hidden");
  el(REVIEW_IDS.ack).focus();
}

function closeOrderReview() {
  if (reviewSubmitting) return;
  document.getElementById(REVIEW_IDS.backdrop).classList.add("hidden");
  if (reviewOpener && typeof reviewOpener.focus === "function") reviewOpener.focus();
  reviewOpener = null;
  reviewQty = null;
  reviewSnapshot = null;
}

async function confirmReviewedOrder() {
  if (reviewSubmitting) return;
  const ack = document.getElementById(REVIEW_IDS.ack);
  if (!ack.checked || reviewSnapshot === null) return;
  reviewSubmitting = true;
  const confirmBtn = document.getElementById(REVIEW_IDS.confirm);
  const cancelBtn = document.getElementById(REVIEW_IDS.cancel);
  const errBox = document.getElementById(REVIEW_IDS.error);
  const fb = document.getElementById("order-feedback");
  confirmBtn.disabled = true;
  cancelBtn.disabled = true;
  confirmBtn.innerHTML = `<span>Submitting to Alpaca...</span>`;
  try {
    const r = await fetch("/api/orders/bracket", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: reviewSnapshot.ticker,
        qty: reviewSnapshot.qty,
        side: "buy",
        take_profit_price: reviewSnapshot.tp,
        stop_loss_price: reviewSnapshot.sl
      })
    });
    const data = await r.json();
    if (data.success && data.order) {
      reviewSubmitting = false;
      confirmBtn.disabled = false;
      confirmBtn.innerHTML = `<span>Confirm & Submit</span>`;
      closeOrderReview();
      fb.className = "order-feedback text-green";
      fb.textContent = `Bracket Order placed successfully in Alpaca! (ID: ${data.order.id.substring(0, 8)}...)`;
      loadPositions();
      loadOrdersHistory();
      loadAccount();
    } else {
      reviewSubmitting = false;
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
      confirmBtn.innerHTML = `<span>Confirm & Submit</span>`;
      // 403 = rechazo de política (Task 2: options-only). Se explica en
      // lenguaje del producto en vez de escupir el error crudo del backend.
      let msg = data.error || "Failed to place order";
      if (r.status === 403) {
        msg = "Stock orders are disabled: the autonomous flow is options-only. "
            + "Use the Options module to trade.";
        confirmBtn.disabled = true;
        confirmBtn.innerHTML = `<span>Options only</span>`;
      }
      errBox.textContent = msg;
      errBox.classList.remove("hidden");
      fb.className = "order-feedback text-red";
      fb.textContent = msg;
    }
  } catch (e) {
    reviewSubmitting = false;
    confirmBtn.disabled = false;
    cancelBtn.disabled = false;
    confirmBtn.innerHTML = `<span>Confirm & Submit</span>`;
    const msg = `Network Error: ${e}`;
    errBox.textContent = msg;
    errBox.classList.remove("hidden");
    fb.className = "order-feedback text-red";
    fb.textContent = msg;
  }
}

// ── 9. LIVE NEWS HELPER ────────────────────────────────────────────────────

function renderNews(newsList) {
  const c = document.getElementById("agent-news-list");
  c.innerHTML = "";
  if (newsList.length === 0) {
    c.innerHTML = `<div class="text-muted">No recent news available on Alpaca stream.</div>`;
    return;
  }
  newsList.forEach((n) => {
    const d = document.createElement("div");
    d.style.marginBottom = "0.35rem";
    d.innerHTML = `• <a href="${n.url || '#'}" target="_blank" style="color:var(--text-main); text-decoration:none;">${n.headline}</a> <span class="text-muted">(${n.source || 'Benzinga'})</span>`;
    c.appendChild(d);
  });
}

// ══════════ OPTIONS ALPHA AGENT ══════════
const OptState = { ticker: null, proposal: null, chain: null, expiry: null };

function optFmt(v, d = 2) {
    return (v === null || v === undefined || isNaN(v)) ? "-" : Number(v).toFixed(d);
}

async function optLoadChain() {
    const t = document.getElementById("opt-ticker-input").value.trim().toUpperCase();
    if (!t) return alert("Enter a ticker");
    try {
        const j = await (await fetch(`/api/options/chain/${t}`)).json();
        if (!j.success) return alert(j.error);
        OptState.ticker = t;
        document.getElementById("opt-chain-summary").innerHTML = `
            <div class="opt-stat-tile">
                <span class="opt-stat-lbl">Spot</span>
                <span class="opt-stat-val">$${optFmt(j.spot)}</span>
                <span class="opt-stat-sub">${t}</span>
            </div>
            <div class="opt-stat-tile">
                <span class="opt-stat-lbl">ATM IV</span>
                <span class="opt-stat-val">${j.atm_iv_avg ? (j.atm_iv_avg * 100).toFixed(1) + "%" : "N/A"}</span>
                <span class="opt-stat-sub">avg near-the-money</span>
            </div>
            <div class="opt-stat-tile">
                <span class="opt-stat-lbl">Contracts</span>
                <span class="opt-stat-val">${j.contracts.length}</span>
                <span class="opt-stat-sub">all expiries fetched</span>
            </div>`;
        const hint = document.getElementById("opt-summary-hint");
        if (hint) hint.classList.add("hidden");
        optRenderChain(j);
    } catch (e) {
        alert(`Error loading chain: ${e.message}`);
    }
}

// Calendar days to expiry from a YYYY-MM-DD string. Null if unparseable.
function optDte(expiryStr) {
    if (!expiryStr) return null;
    const exp = new Date(`${String(expiryStr).slice(0, 10)}T00:00:00`);
    if (isNaN(exp.getTime())) return null;
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((exp - today) / 86400000);
}

// Re-render the chain already in memory for the newly selected expiry.
function optOnExpiryChange() {
    const sel = document.getElementById("opt-expiry-select");
    OptState.expiry = sel.value || null;
    if (OptState.chain) optRenderChain(OptState.chain, OptState.expiry);
}

function optRenderChain(payload, wantExpiry) {
    OptState.chain = payload;
    const tbody = document.querySelector("#opt-chain-table tbody");
    tbody.innerHTML = "";
    const byKey = {};
    payload.contracts.forEach(c => {
        const k = `${c.expiry}|${c.strike}`;
        byKey[k] = byKey[k] || { expiry: c.expiry, strike: c.strike };
        byKey[k][c.type] = c;
    });
    const expiries = [...new Set(Object.values(byKey).map(r => r.expiry))].sort();
    if (!expiries.length) {
        optRenderOiChart();
        return;
    }

    // Selected expiry: caller's request if it exists in the chain, else nearest.
    const expiry = expiries.includes(wantExpiry) ? wantExpiry : expiries[0];
    OptState.expiry = expiry;

    // Populate the picker so every expiry in the payload is reachable.
    const sel = document.getElementById("opt-expiry-select");
    sel.disabled = false;
    sel.innerHTML = expiries.map(e => {
        const d = optDte(e);
        const label = d === null ? e : `${e} (${d}d)`;
        return `<option value="${e}"${e === expiry ? " selected" : ""}>${label}</option>`;
    }).join("");
    const dte = optDte(expiry);
    document.getElementById("opt-expiry-dte").textContent =
        dte === null ? "— DTE" : `${dte} DTE`;

    // OCC symbol of the contract the engine proposed, if any, so we can mark it.
    const proposedOcc = (OptState.proposal && OptState.proposal.contract)
        ? OptState.proposal.contract.occ_symbol : null;

    const rows = Object.values(byKey)
        .filter(r => r.expiry === expiry)
        .sort((a, b) => Math.abs(a.strike - payload.spot) - Math.abs(b.strike - payload.spot))
        .slice(0, 25)
        .sort((a, b) => a.strike - b.strike); // final presentation: strikes ascending
    rows.forEach(r => {
        const call = r.call || {}, put = r.put || {};
        const midStrike = call.strike ?? put.strike;
        const atm = payload.spot > 0 && Math.abs(midStrike - payload.spot) / payload.spot < 0.01 ? "atm-cell" : "";
        const tr = document.createElement("tr");
        if (proposedOcc && (call.occ_symbol === proposedOcc || put.occ_symbol === proposedOcc)) {
            tr.className = "opt-proposed-row";
            tr.title = "Contract selected by the engine";
        }
        tr.innerHTML = `
            <td class="${atm}">${optFmt(call.delta)}</td>
            <td class="${atm}">${call.iv != null ? (call.iv * 100).toFixed(0) + "%" : "-"}</td>
            <td class="${atm}">${call.open_interest ?? "-"}</td>
            <td class="${atm}">${optFmt(call.bid)}</td>
            <td class="${atm}">${optFmt(call.ask)}</td>
            <td class="${atm}">${optFmt(r.strike, 0)}</td>
            <td>${optFmt(put.bid)}</td>
            <td>${optFmt(put.ask)}</td>
            <td>${put.open_interest ?? "-"}</td>
            <td>${put.iv != null ? (put.iv * 100).toFixed(0) + "%" : "-"}</td>
            <td>${optFmt(put.delta)}</td>`;
        tbody.appendChild(tr);
    });
    optRenderOiChart();
}

// Human-readable strategy name + risk character for the proposal card.
const OPT_STRATEGY_LABELS = {
    long_call: { name: "Long Call", risk: "defined risk (debit)" },
    long_put: { name: "Long Put", risk: "defined risk (debit)" },
    cash_secured_put: { name: "Cash-Secured Put", risk: "collateral committed (credit)" },
    covered_call: { name: "Covered Call", risk: "upside capped (credit)" },
};

function optStrategyLabel(direction) {
    const s = OPT_STRATEGY_LABELS[direction];
    if (!s) return "Directional option";
    return `${s.name} — ${s.risk}`;
}

function optSyncStrategyControls() {
    const wrap = document.getElementById("opt-premium-wrap");
    const typeEl = document.getElementById("opt-strategy-type");
    if (!wrap || !typeEl) return;
    wrap.classList.toggle("hidden", typeEl.value !== "premium_income");
}

function optRenderPremiumProposal(p) {
    const card = document.getElementById("opt-proposal-card");
    const m = p.metrics || {};
    const c = p.contract || {};
    const isCsp = p.strategy === "cash_secured_put";
    const breakEvenLbl = isCsp ? "Break-even" : "Effective Exit";
    const breakEvenVal = isCsp ? m.effective_entry : m.effective_exit;
    const capitalLbl = isCsp ? "Capital Committed" : "Shares Covered";
    const capitalVal = isCsp
        ? "$" + optFmt(m.cash_collateral)
        : optFmt((p.coverage && p.coverage.shares_owned) || 0, 0);
    const maxGain = isCsp
        ? "$" + optFmt(m.premium_received)
        : "$" + optFmt((m.max_upside_from_current || 0) * 100 * (p.quantity || 0));
    // Backend returns premium_yield as a decimal fraction; multiply by 100 once.
    const yieldTxt = optFmt(m.premium_yield * 100, 2) + "%";
    const thesisTxt = typeof p.thesis === "string" ? p.thesis : (p.thesis && p.thesis.headline) || "";
    card.innerHTML = `
        <h3>${thesisTxt}</h3>
        <div class="opt-stats-grid">
            <div class="opt-stat-tile"><span class="opt-stat-lbl">Strategy</span>
                <span class="opt-stat-val">${optStrategyLabel(p.strategy)}</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">Contract</span>
                <span class="opt-stat-val">${c.occ_symbol || "-"}</span>
                <span class="opt-stat-sub">$${optFmt(c.strike, 0)} | ${c.expiry || "-"} | ${c.dte ?? "-"} DTE</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">Quantity</span>
                <span class="opt-stat-val">${p.quantity}</span>
                <span class="opt-stat-sub">${p.action || "sell_to_open"} @ $${optFmt(p.limit_price)}</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">Premium Received</span>
                <span class="opt-stat-val">$${optFmt(m.premium_received)}</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">${breakEvenLbl}</span>
                <span class="opt-stat-val">$${optFmt(breakEvenVal)}</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">${capitalLbl}</span>
                <span class="opt-stat-val">${capitalVal}</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">Premium Yield</span>
                <span class="opt-stat-val">${yieldTxt}</span></div>
            <div class="opt-stat-tile"><span class="opt-stat-lbl">Max Gain</span>
                <span class="opt-stat-val">${maxGain}</span></div>
        </div>
        <span class="opt-proposal-only">${p.account_snapshot_note || ""}</span>
        <span class="opt-proposal-only">Manual confirmation required - no order is placed</span>
        ${optPayoffBlockHtml(p)}`;
}

// Chart A: perfil de open interest por strike del expiry seleccionado. Informativo.
//
// Antes el dashboard ofrecia un toggle OI / Volume. Volume siempre estaba en
// cero en el plan gratuito de Alpaca (medido 2026-09-03: 0 de 3648 contratos
// de SPY), asi que el boton solo llevaba a una pantalla de "No volume data".
// Quitarlo deja un solo modo (OI) y un mensaje honesto cuando no hay datos.

function optOiExpiryHasData(chain, expiry) {
    return Array.isArray(chain && chain.oi_expiries) &&
        chain.oi_expiries.indexOf(expiry) !== -1;
}

function optRenderOiChart() {
    const block = document.getElementById("opt-oi-chart-block");
    const holder = document.getElementById("opt-oi-chart");
    if (!block || !holder) return;
    const chain = OptState.chain, expiry = OptState.expiry;
    if (!chain || !expiry) {
        block.classList.add("hidden");
        holder.innerHTML = "";
        return;
    }
    block.classList.remove("hidden");
    if (typeof OptionsCharts === "undefined") {
        block.classList.add("hidden");
        return;
    }
    if (!optOiExpiryHasData(chain, expiry)) {
        const others = Array.isArray(chain.oi_expiries) ? chain.oi_expiries : [];
        const hint = others.length
            ? `Open-interest data is not available for ${expiry}. Try ${others.join(", ")}.`
            : `No open-interest data is available for any expiry in this chain.`;
        holder.innerHTML = `<div class="opt-oi-note">${hint}</div>`;
        return;
    }
    const profile = OptionsCharts.computeOiProfile(chain.contracts, expiry, chain.spot);
    holder.innerHTML = profile.available
        ? OptionsCharts.renderOiProfileSvg(profile, "oi")
        : '<div class="opt-oi-note">No data for this expiry.</div>';
}

// Chart C: payoff al vencimiento de la estrategia propuesta. Informativo.
function optPayoffBlockHtml(p) {
    if (typeof OptionsCharts === "undefined") return "";
    const leg = OptionsCharts.computePayoff(p);
    if (!leg || leg.available === false) return "";
    const series = OptionsCharts.payoffSeries(leg);
    if (!series || series.available === false) return "";
    const svg = OptionsCharts.renderPayoffSvg(series);
    if (!svg) return "";
    const beTxt = series.breakevens.map(b => "$" + optFmt(b)).join(" · ") || "—";
    const maxGain = series.maxProfit === null ? "Unlimited" : "$" + optFmt(series.maxProfit);
    const kindLabel = { long_call: "Long Call", long_put: "Long Put",
        short_put: "Cash-Secured Put", covered_call: "Covered Call" }[series.kind] || series.kind;
    const note = series.kind === "covered_call" ? " (P/L from current price)" : "";
    return `<div class="opt-payoff-block">
        <div class="opt-payoff-title">Payoff at Expiration — ${kindLabel}${note}</div>
        ${svg}
        <div class="opt-payoff-meta">
            <span>Break-even: ${beTxt}</span>
            <span>Max gain: ${maxGain}</span>
            <span>Max loss: $${optFmt(Math.abs(series.maxLoss))}</span>
        </div>
    </div>`;
}

// Embudo del selector de opciones.
//
// El backend devuelve `filter_stats` en TODAS las respuestas del selector,
// incluidos los rechazos. Sin esto la UI sólo mostraba "No proposal: <motivo>"
// y el escrutinio real —412 contratos revisados, 14 descartados por theta— era
// invisible. Mostrarlo es lo que convierte un "no opero" en una decisión
// auditable en vez de un capricho, y es la diferencia entre un filtro que se
// puede defender y uno que hay que creer a ciegas.
function optFilterStatsHtml(stats) {
    if (!stats || typeof stats !== "object" || !Object.keys(stats).length) return "";

    const num = (v) => (typeof v === "number" && isFinite(v)) ? v : null;
    const step = (v, label) => (v === null ? "" :
        `<div class="opt-funnel-step"><b>${v}</b><span>${label}</span></div>`);
    const arrow = '<span class="opt-funnel-arrow">&rarr;</span>';
    const steps = [
        step(num(stats.chain_size), "in chain"),
        step(num(stats.candidates), "pass DTE+liq"),
        step(num(stats.priced), "priced"),
        step(num(stats.survivors), "survive rules"),
    ].filter(Boolean).join(arrow);

    const notes = [];
    const worst = (rows) => {
        const vals = rows.map(x => num(x[1])).filter(v => v !== null);
        return vals.length ? Math.max(...vals) : null;
    };

    // R3 · sangría de theta. Es el filtro que más contratos mata, así que va
    // primero y con el peor valor: "14 descartados" sin el número no dice nada.
    const rejTheta = Array.isArray(stats.rejected_theta) ? stats.rejected_theta : [];
    if (rejTheta.length) {
        const w = worst(rejTheta);
        notes.push(`<span class="opt-funnel-chip">R3 theta</span> ${rejTheta.length} dropped`
            + (w === null ? "" : ` on theta burn (worst <b>${w.toFixed(2)}%/day</b>)`));
    }
    // R2 · prima cara: IV muy por encima de la volatilidad realizada.
    const rejIv = Array.isArray(stats.rejected_iv_rv) ? stats.rejected_iv_rv : [];
    if (rejIv.length) {
        const w = worst(rejIv);
        notes.push(`<span class="opt-funnel-chip">R2 IV/RV</span> ${rejIv.length} dropped`
            + (w === null ? "" : ` as rich premium (worst <b>${w.toFixed(2)}x</b> realized)`));
    }
    const term = (stats.term && typeof stats.term === "object") ? stats.term : null;
    if (term && term.applied) {
        const ratio = num(term.ratio);
        const tail = ratio === null ? "" : ` (<b>${ratio}x</b> near/far)`;
        notes.push(term.inverted
            ? `<span class="opt-funnel-chip">R5 term</span> IV term structure inverted${tail} — an event is priced in, buying premium here means eating the vol crush`
            : `<span class="opt-funnel-chip ok">R5 term</span> IV term structure normal${tail}`);
    }
    // Degradación declarada, no oculta: el feed no trajo griegas utilizables.
    if (stats.greeks_degraded) {
        notes.push('<span class="opt-funnel-chip warn">degraded</span> no usable IV/theta from the feed — traded on delta + liquidity rather than faking a judgement we cannot make');
    }
    if (stats.atm_fallback) {
        notes.push('<span class="opt-funnel-chip warn">ATM fallback</span> no contract with usable delta — took the strike closest to spot');
    }

    if (!steps && !notes.length) return "";
    return `<div class="opt-filter-funnel">
        <div class="opt-funnel-head">Options screening funnel</div>
        ${steps ? `<div class="opt-funnel-steps">${steps}</div>` : ""}
        ${notes.length ? `<div class="opt-funnel-note">${notes.join("<br>")}</div>` : ""}
    </div>`;
}

// Motivo del gate de riesgo, explicado.
//
// `submit_authorized_option_order` devuelve el reason en crudo ("order not
// authorized: underlying_concentration_limit"). Mostrarlo tal cual es justo lo
// que se le recrimina a un agente: una negativa sin fundamento. R6 merece
// explicacion completa porque es la regla que impide apilar la misma apuesta
// bajo dos contratos distintos.
function optRiskReasonHtml(verdict) {
    if (!verdict || typeof verdict !== "object") return "";
    const reason = String(verdict.reason || "");
    if (!reason) return "";
    const n = (v) => (typeof v === "number" && isFinite(v))
        ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : "?";

    const R = {
        underlying_concentration_limit: () =>
            `<b>R6 concentration</b> — ${verdict.underlying || "this name"} already carries `
            + `${n(verdict.open_in_underlying)} open position(s), cap is ${n(verdict.limit)}. `
            + `Two contracts on the same ticker at the same expiry are one bet split into two `
            + `tickets, not a diversified book: correlation is ~1.0, so a 1% move hits both.`,
        already_in_portfolio: () =>
            `<b>Duplicate</b> — this exact contract is already in the book.`,
        order_already_in_flight: () =>
            `<b>Duplicate</b> — an order for this contract was already sent in this cycle.`,
        max_open_positions_reached: () =>
            `<b>Position cap</b> — ${n(verdict.open_positions)} open + `
            + `${n(verdict.in_flight)} in flight, limit ${n(verdict.limit)}.`,
        gross_exposure_limit: () =>
            `<b>Gross exposure</b> — ${n(verdict.exposure_pct)}% would breach the `
            + `${n(verdict.limit_pct)}% ceiling.`,
        position_size_pct_limit: () =>
            `<b>Position size</b> — this order is above the per-position ceiling.`,
        notional_cap_exceeded: () =>
            `<b>Notional cap</b> — $${n(verdict.notional)} over the $${n(verdict.cap)} hard ceiling.`,
        insufficient_options_buying_power: () =>
            `<b>Options buying power</b> — $${n(verdict.notional)} needed, only `
            + `$${n(verdict.usable_options_buying_power)} usable after the buffer.`,
        options_buying_power_unknown: () =>
            `<b>Unknown options buying power</b> — refusing rather than silently falling `
            + `back to the general buying power, which is levered.`,
        daily_loss_breach: () =>
            `<b>Daily loss</b> — the account is paused for the day.`,
    };
    const body = R[reason] ? R[reason]() : `<b>${reason}</b>`;
    return `<div class="opt-filter-funnel">
        <div class="opt-funnel-head">Risk gate refused this order</div>
        <div class="opt-funnel-note">${body}</div>
    </div>`;
}

async function optRequestProposal() {
    const t = document.getElementById("opt-ticker-input").value.trim().toUpperCase();
    if (!t) return alert("Enter a ticker");
    const card = document.getElementById("opt-proposal-card");
    card.classList.remove("hidden");
    card.classList.remove("proposal-empty");
    card.innerHTML = "<em>Analyzing technical signal + options chain…</em>";
    try {
        const body = { ticker: t };
        const typeEl = document.getElementById("opt-strategy-type");
        if (typeEl && typeEl.value === "premium_income") {
            body.strategy_type = "premium_income";
            body.premium_strategy = document.getElementById("opt-premium-strategy").value;
            body.target_strike = document.getElementById("opt-target-strike").value;
        }
        const res = await fetch("/api/options/proposal", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        const j = await res.json();
        if (!j.success || !j.proposal.available) {
            // El embudo se pinta AQUÍ también, y es donde más importa: un
            // rechazo sin el escrutinio detrás es indistinguible de un capricho.
            const rejStats = (j.proposal && j.proposal.filter_stats) || null;
            card.innerHTML =
                `<span style="color:#FF4D4D">No proposal: ${j.error || (j.proposal && j.proposal.reason)}</span>`
                + optFilterStatsHtml(rejStats);
            return;
        }
        OptState.proposal = j.proposal;
        const p = j.proposal;
        if (p.branch === "premium_income") {
            if (OptState.chain && p.contract && p.contract.expiry
                    && OptState.chain.ticker === p.ticker) {
                optRenderChain(OptState.chain, p.contract.expiry);
            }
            optRenderPremiumProposal(p);
            return;
        }
        // If the chain on screen belongs to this ticker, jump to the expiry the
        // engine picked so the proposed contract is actually visible and marked.
        if (OptState.chain && p.contract && p.contract.expiry
                && OptState.chain.ticker === p.ticker) {
            optRenderChain(OptState.chain, p.contract.expiry);
        }
        card.innerHTML = `
            <h3>${p.thesis.headline}</h3>
            <ul>${p.thesis.points.map(x => `<li>${x}</li>`).join("")}</ul>
            <div class="prop-row">
                <span><b>Strategy:</b> ${optStrategyLabel(p.direction)}</span>
                <span><b>Contract:</b> ${p.contract.occ_symbol}</span>
                <span><b>Expiry:</b> ${p.contract.expiry} (${p.contract.dte}d)</span>
                <span><b>Qty:</b> ${p.sizing.qty}</span>
                <span><b>Premium Paid:</b> $${optFmt(p.sizing.premium_usd)}</span>
                <span><b>Risk at Stop:</b> $${optFmt(p.sizing.risk_usd)}</span>
                <span><b>Risk Budget:</b> $${optFmt(p.sizing.risk_budget_usd)}</span>
                <span><b>Confidence:</b> ${p.thesis.risk_level}</span>
            </div>
            ${optFilterStatsHtml(p.filter_stats)}
            <button class="btn btn-primary" onclick="optExecute()">Execute Option Order (Paper)</button>
            ${optPayoffBlockHtml(p)}`;
    } catch (e) {
        card.innerHTML = `<span style="color:#FF4D4D">Error: ${e.message}</span>`;
    }
}

async function optExecute() {
    if (!OptState.proposal) return;
    const p = OptState.proposal;
    if (!confirm(`Send BUY TO OPEN of ${p.sizing.qty}x ${p.contract.occ_symbol} to Alpaca Paper?`)) return;
    try {
        const res = await fetch("/api/options/order", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(p)
        });
        const j = await res.json();
        alert(j.success ? "Order submitted. Exit Manager monitors TP/SL/time-stop."
                        : j.error);
        // El alert avisa; la tarjeta EXPLICA. Un rechazo del gate sin su
        // desglose es indistinguible de un capricho, y un `alert` no puede
        // mostrarlo.
        if (!j.success) {
            const card = document.getElementById("opt-proposal-card");
            if (card) {
                card.classList.remove("hidden", "proposal-empty");
                card.innerHTML =
                    `<span style="color:#FF4D4D">${j.error}</span>`
                    + optRiskReasonHtml(j.risk_verdict);
            }
        }
        if (j.success) OptState.proposal = null;
        optLoadJournal();
    } catch (e) {
        alert(`Network error: ${e.message}`);
    }
}

async function optToggleAutopilot(ev) {
    const enabled = ev.target.checked;
    try {
        const res = await fetch("/api/options/autopilot", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled })
        });
        const j = await res.json();
        if (!j.success) { ev.target.checked = false; alert(j.error); }
        optLoadAutopilotStatus();
    } catch (e) {
        ev.target.checked = false;
        alert(`Error: ${e.message}`);
    }
}

async function optLoadAutopilotStatus() {
    try {
        const j = await (await fetch("/api/options/autopilot")).json();
        document.getElementById("opt-autopilot-status").textContent = j.state.status_label;
        document.getElementById("opt-autopilot-toggle").checked =
            j.state.enabled === true && !j.state.paused_reason;
    } catch (e) { /* silent */ }
}

async function optLoadJournal() {
    try {
        const j = await (await fetch("/api/options/journal")).json();
        if (!j.success) return;
        const tb = document.querySelector("#opt-journal-table tbody");
        tb.innerHTML = "";
        j.entries.slice().reverse().forEach(e => {
            const pnlCls = (e.pnl_usd || 0) >= 0 ? "pnl-pos" : "pnl-neg";
            const tr = document.createElement("tr");
            tr.innerHTML = `<td>${e.id}</td><td>${e.ticker}</td>
                <td style="font-size:11px">${e.occ}</td><td>${e.direction || "-"}</td>
                <td>${e.qty}</td><td>${optFmt(e.entry_ask)}</td>
                <td>${e.status}</td><td>${e.reason || "-"}</td>
                <td class="${e.status === "closed" ? pnlCls : ""}">
                    ${e.status === "closed" ? "$" + optFmt(e.pnl_usd) : "-"}</td>`;
            tb.appendChild(tr);
        });
        if (j.summary) {
            console.log(`Options Journal — open: ${j.summary.open}, closed: ${j.summary.closed}, realized P&L: $${j.summary.realized_pnl}`);
        }
    } catch (e) { /* silent */ }
}

document.getElementById("opt-load-chain-btn")?.addEventListener("click", optLoadChain);
document.getElementById("opt-expiry-select")?.addEventListener("change", optOnExpiryChange);
document.getElementById("opt-proposal-btn")?.addEventListener("click", optRequestProposal);
document.getElementById("opt-autopilot-toggle")?.addEventListener("change", optToggleAutopilot);
document.getElementById("opt-strategy-type")?.addEventListener("change", optSyncStrategyControls);
optSyncStrategyControls();
optLoadAutopilotStatus();
optLoadJournal();
setInterval(optLoadJournal, 60000);
