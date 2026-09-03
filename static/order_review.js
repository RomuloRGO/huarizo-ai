/* Huarizo AI — Order Review Safety Gate (pure functions, no DOM access).
   UMD export: browser -> window.OrderReview, Node -> module.exports. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.OrderReview = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function round2(v) {
    return Number(Number(v).toFixed(2));
  }

  function isFinitePositive(v) {
    return typeof v === "number" && isFinite(v) && v > 0;
  }

  function parseQty(qtyRaw) {
    const n = Number(String(qtyRaw == null ? "" : qtyRaw).trim());
    return Number.isInteger(n) && n > 0 ? n : null;
  }

  function formatRsiState(technical) {
    const rsi = technical && technical.rsi;
    if (!rsi || typeof rsi.value !== "number" || !isFinite(rsi.value)) return "--";
    return rsi.value.toFixed(1) + " (" + (rsi.state || "N/A") + ")";
  }

  function computeOrderReview(params) {
    const analysis = params && params.analysis;
    const plan = params && params.plan;
    const qty = parseQty(params && params.qtyRaw);

    if (!analysis || !plan) {
      return { ok: false, error: "Run an analysis first — no current data for execution." };
    }
    if (!isFinitePositive(analysis.price)) {
      return { ok: false, error: "Analysis price is invalid — rerun the analysis." };
    }
    if (!isFinitePositive(plan.take_profit_price) || !isFinitePositive(plan.stop_loss_price)) {
      return { ok: false, error: "Bracket plan is incomplete — rerun the analysis." };
    }
    if (qty === null) {
      return { ok: false, error: "Enter a positive whole number of shares." };
    }

    const entry = analysis.price;
    const tp = plan.take_profit_price;
    const sl = plan.stop_loss_price;

    if (entry - sl <= 0) {
      return { ok: false, error: "Stop-loss is not below the analysis price — risk cannot be computed. Rerun the analysis." };
    }

    const maxLoss = round2((entry - sl) * qty);
    const potentialGain = round2((tp - entry) * qty);

    return {
      ok: true,
      review: {
        ticker: params.ticker,
        side: "buy",
        entryPrice: round2(entry),
        qty: qty,
        notional: round2(entry * qty),
        stopLoss: round2(sl),
        takeProfit: round2(tp),
        maxLoss: maxLoss,
        potentialGain: potentialGain,
        riskReward: round2(potentialGain / maxLoss)
      }
    };
  }

  return { formatRsiState: formatRsiState, computeOrderReview: computeOrderReview };
});
