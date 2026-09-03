const { test } = require("node:test");
const assert = require("node:assert");
const OrderReview = require("../../static/order_review.js");

const BASE = {
  analysis: { price: 217.54 },
  plan: { take_profit_price: 239.05, stop_loss_price: 203.2 },
  qtyRaw: "10"
};

test("computeOrderReview valid case computes exposure", () => {
  const res = OrderReview.computeOrderReview(BASE);
  assert.strictEqual(res.ok, true);
  const r = res.review;
  assert.strictEqual(r.side, "buy");
  assert.strictEqual(r.entryPrice.toFixed(2), "217.54");
  assert.strictEqual(r.qty, 10);
  assert.strictEqual(r.notional.toFixed(2), "2175.40");
  assert.strictEqual(r.stopLoss.toFixed(2), "203.20");
  assert.strictEqual(r.takeProfit.toFixed(2), "239.05");
  assert.strictEqual(r.maxLoss.toFixed(2), "143.40");
  assert.strictEqual(r.potentialGain.toFixed(2), "215.10");
  assert.strictEqual(r.riskReward.toFixed(2), "1.50");
});

test("computeOrderReview rounds money to 2 decimals", () => {
  const res = OrderReview.computeOrderReview({
    analysis: { price: 33.33 },
    plan: { take_profit_price: 39.99, stop_loss_price: 30.01 },
    qtyRaw: "3"
  });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.review.maxLoss.toFixed(2), "9.96");
  assert.strictEqual(res.review.potentialGain.toFixed(2), "19.98");
  assert.strictEqual(res.review.riskReward.toFixed(2), "2.01");
});

test("computeOrderReview rejects invalid quantities", () => {
  for (const bad of ["0", "-5", "2.5", "abc", "", "  "]) {
    const res = OrderReview.computeOrderReview({ ...BASE, qtyRaw: bad });
    assert.strictEqual(res.ok, false, `qtyRaw=${JSON.stringify(bad)} must be rejected`);
  }
});

test("computeOrderReview rejects missing analysis", () => {
  const res = OrderReview.computeOrderReview({ analysis: null, plan: BASE.plan, qtyRaw: "10" });
  assert.strictEqual(res.ok, false);
});

test("computeOrderReview rejects non-finite or non-positive price", () => {
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: { price: NaN }, plan: BASE.plan, qtyRaw: "10" }).ok, false);
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: { price: 0 }, plan: BASE.plan, qtyRaw: "10" }).ok, false);
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: { price: -3 }, plan: BASE.plan, qtyRaw: "10" }).ok, false);
});

test("computeOrderReview rejects missing or invalid bracket plan", () => {
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: BASE.analysis, plan: null, qtyRaw: "10" }).ok, false);
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: BASE.analysis, plan: {}, qtyRaw: "10" }).ok, false);
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: BASE.analysis, plan: { take_profit_price: 239.05 }, qtyRaw: "10" }).ok, false);
  assert.strictEqual(OrderReview.computeOrderReview({ analysis: BASE.analysis, plan: { take_profit_price: NaN, stop_loss_price: 203.2 }, qtyRaw: "10" }).ok, false);
});

test("computeOrderReview rejects zero-risk denominator (stop at or above entry)", () => {
  const atEntry = OrderReview.computeOrderReview({ analysis: BASE.analysis, plan: { take_profit_price: 239.05, stop_loss_price: 217.54 }, qtyRaw: "10" });
  assert.strictEqual(atEntry.ok, false);
  const aboveEntry = OrderReview.computeOrderReview({ analysis: BASE.analysis, plan: { take_profit_price: 239.05, stop_loss_price: 220 }, qtyRaw: "10" });
  assert.strictEqual(aboveEntry.ok, false);
});

test("formatRsiState renders rsi.state, never 'undefined'", () => {
  assert.strictEqual(OrderReview.formatRsiState({ rsi: { value: 52.3, state: "NEUTRAL" } }), "52.3 (NEUTRAL)");
  assert.strictEqual(OrderReview.formatRsiState({ rsi: { value: 52.3 } }), "52.3 (N/A)");
  assert.strictEqual(OrderReview.formatRsiState({}), "--");
  assert.strictEqual(OrderReview.formatRsiState(null), "--");
  assert.strictEqual(OrderReview.formatRsiState({ rsi: { value: NaN, state: "NEUTRAL" } }), "--");
});
