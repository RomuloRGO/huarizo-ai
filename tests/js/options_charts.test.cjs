const { test } = require("node:test");
const assert = require("node:assert");
const OC = require("../../static/options_charts.js");

// ── computeOiProfile ─────────────────────────────────────────────────────

const C = (type, strike, expiry, extra) => ({
  occ_symbol: `XYZ${expiry.replace(/-/g, "")}${type[0].toUpperCase()}${String(Math.round(strike * 1000)).padStart(8, "0")}`,
  type, strike, expiry, bid: 1.0, ask: 1.1, delta: 0.5, iv: 0.3,
  volume: 100, open_interest: null, ...extra,
});
const EXP = "2026-09-18";

test("computeOiProfile agrupa por strike y lado", () => {
  const contracts = [
    C("call", 100, EXP, { open_interest: 5000, volume: 300 }),
    C("put", 100, EXP, { open_interest: 9000, volume: 700 }),
    C("call", 105, EXP, { open_interest: 1000, volume: 50 }),
  ];
  const p = OC.computeOiProfile(contracts, EXP, 101.0);
  assert.strictEqual(p.available, true);
  assert.deepStrictEqual(p.rows.map(r => r.strike), [100, 105]);
  assert.strictEqual(p.rows[0].call.oi, 5000);
  assert.strictEqual(p.rows[0].put.oi, 9000);
  assert.strictEqual(p.rows[0].put.volume, 700);
  assert.strictEqual(p.rows[1].put, null); // sin contrato put en 105
  assert.strictEqual(p.maxOi, 9000);
  assert.strictEqual(p.maxVol, 700);
});

test("computeOiProfile preserva null vs 0 (nunca coarcela)", () => {
  const contracts = [
    C("call", 100, EXP, { open_interest: 0, volume: 0 }),        // 0 real
    C("put", 100, EXP, { open_interest: null, volume: 250 }),    // sin dato OI
  ];
  const p = OC.computeOiProfile(contracts, EXP, 100);
  assert.strictEqual(p.rows[0].call.oi, 0);
  assert.strictEqual(p.rows[0].call.volume, 0);
  assert.strictEqual(p.rows[0].put.oi, null);
  assert.strictEqual(p.rows[0].put.volume, 250);
});

test("computeOiProfile ventana ±15% con expansión a minStrikes", () => {
  const contracts = [];
  for (let s = 50; s <= 150; s += 1) {
    contracts.push(C("call", s, EXP, { open_interest: 10 }), C("put", s, EXP, { open_interest: 20 }));
  }
  // 101 strikes de 50 a 150; spot 100 → ventana ±15% = [85..115] = 31 strikes
  const p = OC.computeOiProfile(contracts, EXP, 100);
  assert.strictEqual(p.available, true);
  assert.strictEqual(p.rows.length, 31);
  assert.strictEqual(p.rows[0].strike, 85);
  assert.strictEqual(p.rows[p.rows.length - 1].strike, 115);
  assert.strictEqual(p.windowed, true);
  assert.strictEqual(p.totalStrikes, 101);
});

test("computeOiProfile expande cuando la ventana queda thin", () => {
  // Strikes muy espaciados: ventana ±15% de 100 solo contiene 100.
  const contracts = [80, 95, 100, 105, 120].flatMap(s =>
    [C("call", s, EXP, { open_interest: 1 }), C("put", s, EXP, { open_interest: 2 })]);
  const p = OC.computeOiProfile(contracts, EXP, 100);
  assert.strictEqual(p.rows.length, 5); // expandido a minStrikes (todos los disponibles)
  assert.deepStrictEqual(p.rows.map(r => r.strike), [80, 95, 100, 105, 120]);
});

test("computeOiProfile fail-closed", () => {
  assert.strictEqual(OC.computeOiProfile([], EXP, 100).available, false);
  assert.strictEqual(OC.computeOiProfile([C("call", 100, EXP)], null, 100).available, false);
  const p = OC.computeOiProfile([C("call", 100, "2027-01-15")], EXP, 100);
  assert.strictEqual(p.available, false);
  assert.ok(p.reason.length > 0);
});

// ── computePayoff ────────────────────────────────────────────────────────

test("computePayoff normaliza propuesta premium CSP", () => {
  const prop = {
    available: true, branch: "premium_income", strategy: "cash_secured_put",
    contract: { type: "put", strike: 200, bid: 1.44, ask: 1.6, expiry: EXP },
    quantity: 2, spot: 215.0,
  };
  const leg = OC.computePayoff(prop);
  assert.strictEqual(leg.available, true);
  assert.strictEqual(leg.kind, "short_put");
  assert.strictEqual(leg.strike, 200);
  assert.strictEqual(leg.premium, 1.44);
  assert.strictEqual(leg.qty, 2);
  assert.strictEqual(leg.spot, 215.0);
  assert.strictEqual(leg.stockEntry, undefined);
});

test("computePayoff covered_call usa spot ACTUAL como stockEntry", () => {
  const prop = {
    available: true, branch: "premium_income", strategy: "covered_call",
    contract: { type: "call", strike: 220, bid: 1.5, ask: 1.7, expiry: EXP },
    quantity: 1, spot: 215.0,
  };
  const leg = OC.computePayoff(prop);
  assert.strictEqual(leg.available, true);
  assert.strictEqual(leg.kind, "covered_call");
  assert.strictEqual(leg.stockEntry, 215.0); // P/L desde el precio actual
});

test("computePayoff normaliza propuesta directional", () => {
  const prop = {
    available: true, direction: "long_call",
    contract: { type: "call", strike: 220, ask: 1.50, bid: 1.44, expiry: EXP },
    sizing: { qty: 3 }, spot: 215.0,
  };
  const leg = OC.computePayoff(prop);
  assert.strictEqual(leg.available, true);
  assert.strictEqual(leg.kind, "long_call");
  assert.strictEqual(leg.premium, 1.50); // ask, no bid
  assert.strictEqual(leg.qty, 3);
});

test("computePayoff fail-closed por campo inválido", () => {
  const base = {
    available: true, branch: "premium_income", strategy: "cash_secured_put",
    contract: { type: "put", strike: 200, bid: 1.44, expiry: EXP },
    quantity: 1, spot: 215.0,
  };
  assert.strictEqual(OC.computePayoff({ available: false, reason: "x" }).available, false);
  assert.strictEqual(OC.computePayoff(null).available, false);
  const badBid = JSON.parse(JSON.stringify(base)); badBid.contract.bid = 0;
  assert.strictEqual(OC.computePayoff(badBid).available, false);
  const badQty = JSON.parse(JSON.stringify(base)); badQty.quantity = 0;
  assert.strictEqual(OC.computePayoff(badQty).available, false);
  const badSpot = JSON.parse(JSON.stringify(base)); badSpot.spot = "abc";
  assert.strictEqual(OC.computePayoff(badSpot).available, false);
  const badStrat = JSON.parse(JSON.stringify(base)); badStrat.strategy = "iron_condor";
  assert.strictEqual(OC.computePayoff(badStrat).available, false);
  const badDir = { available: true, direction: "hold", contract: { strike: 100, ask: 1 }, sizing: { qty: 1 }, spot: 100 };
  assert.strictEqual(OC.computePayoff(badDir).available, false);
});

// ── payoffSeries ─────────────────────────────────────────────────────────

test("payoffSeries long_call hand-computed (maxProfit null)", () => {
  const leg = { available: true, kind: "long_call", strike: 220, premium: 1.50, qty: 1, spot: 215 };
  const s = OC.payoffSeries(leg);
  assert.strictEqual(s.available, true);
  assert.strictEqual(s.points.length, 120);
  const atLow = s.points[0];  // S = 215*0.7 = 150.5
  assert.strictEqual(atLow.pl, -150); // -prem*100*q
  const hi = s.points[s.points.length - 1]; // S = 279.5
  assert.strictEqual(hi.pl, (279.5 - 220 - 1.5) * 100);
  assert.deepStrictEqual(s.breakevens, [221.5]);
  assert.strictEqual(s.maxProfit, null);      // ganancia no acotada
  assert.strictEqual(s.maxLoss, -150);
});

test("payoffSeries long_put hand-computed", () => {
  const leg = { available: true, kind: "long_put", strike: 200, premium: 1.44, qty: 1, spot: 215 };
  const s = OC.payoffSeries(leg);
  assert.strictEqual(s.points[0].pl, (200 - 150.5 - 1.44) * 100); // S=150.5 profundo ITM
  assert.deepStrictEqual(s.breakevens, [198.56]);
  assert.strictEqual(s.maxProfit, (200 - 1.44) * 100); // en S→0
  assert.strictEqual(s.maxLoss, -144);
});

test("payoffSeries short_put qty=2 hand-computed", () => {
  const leg = { available: true, kind: "short_put", strike: 200, premium: 1.44, qty: 2, spot: 215 };
  const s = OC.payoffSeries(leg);
  const deepItm = s.points[0]; // S=150.5
  assert.strictEqual(deepItm.pl, (1.44 - (200 - 150.5)) * 200); // -9612 (perdida lineal bajo el strike)
  const atOrAbove = s.points.find(p => p.price >= 200); // NO usar === 200: ningun punto cae exacto
  assert.strictEqual(atOrAbove.pl, 1.44 * 200); // 288 (flat sobre el strike)
  assert.deepStrictEqual(s.breakevens, [198.56]);
  assert.strictEqual(s.maxProfit, 288);
  assert.strictEqual(s.maxLoss, -(200 - 1.44) * 200); // -39712
});

test("payoffSeries covered_call qty=2 hand-computed (escala x200)", () => {
  const leg = { available: true, kind: "covered_call", strike: 220, premium: 1.5, qty: 2, stockEntry: 215, spot: 215 };
  const s = OC.payoffSeries(leg);
  const atOrAbove = s.points.find(p => p.price >= 220); // primer punto >= strike (cap aplica)
  assert.strictEqual(atOrAbove.pl, (220 - 215 + 1.5) * 200); // 1300
  const below = s.points[0]; // S=150.5
  assert.strictEqual(below.pl, (150.5 - 215 + 1.5) * 200); // -12600
  assert.deepStrictEqual(s.breakevens, [213.5]);
  assert.strictEqual(s.maxProfit, 1300);
  assert.strictEqual(s.maxLoss, -(215 - 1.5) * 200); // -42700 (convencion negativa como los otros 3 kinds)
});

test("payoffSeries breakeven fuera del rango se excluye", () => {
  // K=150, spot=215: breakeven de short put = 148.56, fuera de [150.5, 279.5]
  const leg = { available: true, kind: "short_put", strike: 150, premium: 1.44, qty: 1, spot: 215 };
  const s = OC.payoffSeries(leg);
  assert.deepStrictEqual(s.breakevens, []);
});

test("payoffSeries fail-closed", () => {
  assert.strictEqual(OC.payoffSeries({ available: false }).available, false);
  assert.strictEqual(OC.payoffSeries(null).available, false);
  const bad = { available: true, kind: "long_call", strike: -1, premium: 1, qty: 1, spot: 100 };
  assert.strictEqual(OC.payoffSeries(bad).available, false);
});

// ── regresiones de guards (revisión de calidad Task 1) ──────────────────

test("computeOiProfile ignora strike inválido (null/0/negativo)", () => {
  const contracts = [
    C("call", 100, EXP, { open_interest: 10 }),
    { occ_symbol: "BAD1", type: "call", strike: null, expiry: EXP, bid: 1, ask: 1.1, volume: 5, open_interest: 7 },
  ];
  const p = OC.computeOiProfile(contracts, EXP, 100);
  assert.deepStrictEqual(p.rows.map(r => r.strike), [100]);
});

test("computeOiProfile trata OI negativo como sin dato (null)", () => {
  const contracts = [C("call", 100, EXP, { open_interest: -5 })];
  const p = OC.computeOiProfile(contracts, EXP, 100);
  assert.strictEqual(p.rows[0].call.oi, null);
});

// ── regresiones de revisión de calidad Task 2 ──────────────────────────

test("payoffSeries falla cerrada con kind desconocido", () => {
  const bad = { available: true, kind: "bull_call_spread", strike: 100, premium: 1, qty: 1, spot: 100 };
  const s = OC.payoffSeries(bad);
  assert.strictEqual(s.available, false);
  assert.strictEqual(s.points, undefined);
});

test("computePayoff rechaza qty fraccional", () => {
  const prop = {
    available: true, branch: "premium_income", strategy: "cash_secured_put",
    contract: { type: "put", strike: 200, bid: 1.44, expiry: EXP },
    quantity: 2.5, spot: 215.0,
  };
  assert.strictEqual(OC.computePayoff(prop).available, false);
});

test("payoffSeries covered_call sin stockEntry falla cerrada", () => {
  const leg = { available: true, kind: "covered_call", strike: 220, premium: 1.5, qty: 1, spot: 215 };
  assert.strictEqual(OC.payoffSeries(leg).available, false);
});

test("computePayoff directional sin sizing falla cerrada", () => {
  const prop = {
    available: true, direction: "long_call",
    contract: { type: "call", strike: 220, ask: 1.5, expiry: EXP },
    spot: 215.0,
  };
  assert.strictEqual(OC.computePayoff(prop).available, false);
});

// ── renderOiProfileSvg ───────────────────────────────────────────────────

test("renderOiProfileSvg dibuja barras, eje y nota n/d para OI null", () => {
  const contracts = [
    C("call", 100, EXP, { open_interest: 5000, volume: 300 }),
    C("put", 100, EXP, { open_interest: 9000, volume: 700 }),
    C("put", 105, EXP, { open_interest: null, volume: 20 }), // sin dato OI
  ];
  const p = OC.computeOiProfile(contracts, EXP, 101);
  const svgOi = OC.renderOiProfileSvg(p, "oi");
  assert.ok(svgOi.startsWith("<svg"));
  assert.ok(svgOi.includes("#22c55e"));   // calls verde
  assert.ok(svgOi.includes("#ef4444"));   // puts roja
  assert.ok(svgOi.includes("n/d"));       // OI null → marca sin dato
  assert.ok(svgOi.includes("100"));       // strike label
  assert.ok(!svgOi.includes("NaN"));
  const svgVol = OC.renderOiProfileSvg(p, "volume");
  assert.ok(svgVol.includes("#ef4444"));
  assert.ok(!svgVol.includes("n/d"));     // volume siempre presente
});

test("renderOiProfileSvg degrada cuando maxAbs es 0", () => {
  const p = OC.computeOiProfile([C("call", 100, EXP, { open_interest: 0, volume: 0 })], EXP, 100);
  const out = OC.renderOiProfileSvg(p, "oi");
  assert.ok(out.includes("Sin datos")); // mensaje, no svg vacío
});

test("renderPayoffSvg genera svg con linea cero, breakeven y spot", () => {
  const leg = { available: true, kind: "long_call", strike: 220, premium: 1.5, qty: 1, spot: 215 };
  const s = OC.payoffSeries(leg);
  const svg = OC.renderPayoffSvg(s);
  assert.ok(svg.startsWith("<svg"));
  assert.ok(svg.includes("stroke-dasharray")); // línea cero o spot punteada
  assert.ok(svg.includes("221.5"));            // breakeven etiquetado
  assert.ok(!svg.includes("NaN"));
});

// ── regresiones de revisión de calidad Task 3 ───────────────────────────

test("renderOiProfileSvg: línea spot interpola en índice de fila (grid no uniforme)", () => {
  const contracts = [
    C("call", 100, EXP, { open_interest: 10, volume: 1 }),
    C("put", 102.5, EXP, { open_interest: 10, volume: 1 }),
    C("put", 110, EXP, { open_interest: 10, volume: 1 }),
  ];
  const p = OC.computeOiProfile(contracts, EXP, 103);
  const svg = OC.renderOiProfileSvg(p, "oi");
  // filas: yMid(100)=56, yMid(102.5)=36, yMid(110)=16 (invertidas: mayor strike arriba)
  // spot 103 cae en el bracket [102.5, 110]: idx = 1 + 0.5/7.5 = 1.0667 → y = 6 + (2-1.0667)*20 + 10 ≈ 34.67
  const m = svg.match(/<line x1="10" y1="([\d.]+)"/);
  assert.ok(m, "línea spot presente");
  const y = Number(m[1]);
  assert.ok(Math.abs(y - 34.6667) < 0.1, "spotY=" + y + " esperado ~34.67 (no pegado al fondo)");
});

test("renderPayoffSvg: covered_call muestra verde y rojo + breakeven se-prem", () => {
  const leg = { available: true, kind: "covered_call", strike: 220, premium: 1.5, qty: 1, spot: 215, stockEntry: 215 };
  const s = OC.payoffSeries(leg);
  const svg = OC.renderPayoffSvg(s);
  assert.ok(svg.startsWith("<svg"));
  assert.ok(svg.includes("#22c55e") && svg.includes("#ef4444"));
  assert.ok(svg.includes("213.5")); // breakeven = stockEntry - premium
});

test("renderOiProfileSvg degrada con profile malformado (sin rows)", () => {
  assert.strictEqual(OC.renderOiProfileSvg({ available: true, rows: null, maxOi: 1 }, "oi"), "");
});
