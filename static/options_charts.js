/* Huarizo AI — Options Charts (pure functions + SVG string builders, no DOM).
   UMD export: browser -> window.OptionsCharts, Node -> module.exports.
   Data semantics: oi/volume null = NO DATA (never coerced to 0);
   0 = a real zero for the day. Measured on 2026-09-03 against the Alpaca
   free data plan: contract volume was 0 for all 3,648 SPY contracts, and
   open interest was present on only 0.27% of the 7-45 DTE window. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.OptionsCharts = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DISPLAY_WINDOW_PCT = 0.15;
  var MIN_DISPLAY_STRIKES = 12;

  function isFiniteNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  // Agrega contratos de un vencimiento por strike (calls/puts) y recorta a la
  // ventana visual ±windowPct del spot, expandiendo a minStrikes si queda thin.
  // Filas: [{strike, call:{oi,volume}|null, put:{oi,volume}|null}] strikes asc.
  function computeOiProfile(contracts, expiry, spot, opts) {
    opts = opts || {};
    var winPct = isFiniteNum(opts.displayWindowPct) ? opts.displayWindowPct : DISPLAY_WINDOW_PCT;
    var minStrikes = isFiniteNum(opts.minStrikes) ? Math.max(1, opts.minStrikes) : MIN_DISPLAY_STRIKES;
    if (!Array.isArray(contracts) || contracts.length === 0 || !expiry) {
      return { available: false, reason: "no contracts" };
    }
    var spotF = isFiniteNum(spot) && spot > 0 ? spot : null;
    var exp = String(expiry).slice(0, 10);
    var byStrike = {};
    var inExpiry = 0;
    for (var i = 0; i < contracts.length; i++) {
      var c = contracts[i];
      if (!c || String(c.expiry || "").slice(0, 10) !== exp) continue;
      var k = Number(c.strike);
      if (!isFiniteNum(k) || k <= 0) continue;
      inExpiry++;
      var row = byStrike[k] || (byStrike[k] = { strike: k, call: null, put: null });
      if (c.type !== "call" && c.type !== "put") continue;
      row[c.type] = {
        oi: isFiniteNum(c.open_interest) && c.open_interest >= 0 ? c.open_interest : null,
        volume: isFiniteNum(c.volume) && c.volume >= 0 ? c.volume : null,
      };
    }
    if (inExpiry === 0) {
      return { available: false, reason: "no contracts for expiry " + exp };
    }
    var strikes = Object.keys(byStrike).map(Number).sort(function (a, b) { return a - b; });
    var center = spotF !== null ? spotF : strikes[Math.floor((strikes.length - 1) / 2)];
    var winAmt = center * winPct;
    var lo = center - winAmt, hi = center + winAmt;
    var shown = strikes.filter(function (s) { return s >= lo && s <= hi; });
    if (shown.length < Math.min(minStrikes, strikes.length)) {
      var byDist = strikes.slice().sort(function (a, b) {
        return Math.abs(a - center) - Math.abs(b - center);
      });
      var keep = {};
      byDist.slice(0, Math.min(minStrikes, strikes.length)).forEach(function (s) { keep[s] = true; });
      shown = strikes.filter(function (s) { return keep[s]; });
    }
    var rows = shown.map(function (s) { return byStrike[s]; });
    var maxOi = 0, maxVol = 0;
    rows.forEach(function (r) {
      ["call", "put"].forEach(function (side) {
        var slot = r[side];
        if (!slot) return;
        if (slot.oi !== null && slot.oi > maxOi) maxOi = slot.oi;
        if (slot.volume !== null && slot.volume > maxVol) maxVol = slot.volume;
      });
    });
    return {
      available: true, expiry: exp, rows: rows, spot: spotF,
      maxOi: maxOi, maxVol: maxVol, windowed: shown.length < strikes.length,
      totalStrikes: strikes.length,
    };
  }

  // Normaliza cualquier propuesta soportada a UNA pata de payoff.
  // Directional: {direction:"long_call"|"long_put", contract{strike,ask}, sizing{qty}, spot}
  // Premium: {branch:"premium_income", strategy, contract{strike,bid}, quantity, spot}
  // Convención covered_call: stockEntry = spot ACTUAL (P/L desde hoy; el cost
  // basis original del usuario no es conocido por el backend).
  function computePayoff(proposal) {
    if (!proposal || proposal.available !== true) {
      return { available: false, reason: (proposal && proposal.reason) || "proposal unavailable" };
    }
    var c = proposal.contract || {};
    var K = Number(c.strike);
    function money(v) { return isFiniteNum(v) && v > 0; }
    if (!money(K)) return { available: false, reason: "invalid strike" };
    if (proposal.branch === "premium_income") {
      var prem = Number(c.bid);
      var q = Number(proposal.quantity);
      var S0 = Number(proposal.spot);
      if (!money(prem)) return { available: false, reason: "invalid bid" };
      if (!Number.isInteger(q) || q <= 0) return { available: false, reason: "invalid quantity" };
      if (!money(S0)) return { available: false, reason: "invalid spot" };
      if (proposal.strategy === "cash_secured_put") {
        return { available: true, kind: "short_put", strike: K, premium: prem, qty: q, spot: S0 };
      }
      if (proposal.strategy === "covered_call") {
        return { available: true, kind: "covered_call", strike: K, premium: prem, qty: q, stockEntry: S0, spot: S0 };
      }
      return { available: false, reason: "unsupported strategy " + proposal.strategy };
    }
    var kind = proposal.direction === "long_put" ? "long_put"
      : proposal.direction === "long_call" ? "long_call" : null;
    if (!kind) return { available: false, reason: "unsupported direction " + proposal.direction };
    var prem2 = Number(c.ask);
    var q2 = proposal.sizing ? Number(proposal.sizing.qty) : NaN;
    var S02 = Number(proposal.spot);
    if (!money(prem2)) return { available: false, reason: "invalid ask" };
    if (!Number.isInteger(q2) || q2 <= 0) return { available: false, reason: "invalid qty" };
    if (!money(S02)) return { available: false, reason: "invalid spot" };
    return { available: true, kind: kind, strike: K, premium: prem2, qty: q2, spot: S02 };
  }

  var CONTRACT_MULT = 100;

  // Serie P/L al vencimiento en USD. Rango [spot*(1-r), spot*(1+r)], r=0.30.
  // maxProfit null = ganancia no acotada (long call). maxLoss finito y NEGATIVO en los 4 kinds.
  function payoffSeries(leg, opts) {
    opts = opts || {};
    var r = isFiniteNum(opts.rangePct) && opts.rangePct > 0 ? opts.rangePct : 0.30;
    var N = isFiniteNum(opts.points) ? Math.max(20, Math.min(400, Math.round(opts.points))) : 120;
    if (!leg || leg.available !== true) {
      return { available: false, reason: (leg && leg.reason) || "leg unavailable" };
    }
    var K = leg.strike, prem = leg.premium, q = leg.qty, S0 = leg.spot;
    function ok(v) { return isFiniteNum(v) && v > 0; }
    if (!ok(K) || !ok(prem) || !(isFiniteNum(q) && q > 0) || !ok(S0)) {
      return { available: false, reason: "invalid leg numbers" };
    }
    if (leg.kind !== "long_call" && leg.kind !== "long_put"
        && leg.kind !== "short_put" && leg.kind !== "covered_call") {
      return { available: false, reason: "unsupported kind " + leg.kind };
    }
    var se = leg.kind === "covered_call" ? Number(leg.stockEntry) : null;
    if (leg.kind === "covered_call" && !ok(se)) {
      return { available: false, reason: "invalid stockEntry" };
    }
    function plAt(S) {
      var per;
      if (leg.kind === "long_call") per = Math.max(S - K, 0) - prem;
      else if (leg.kind === "long_put") per = Math.max(K - S, 0) - prem;
      else if (leg.kind === "short_put") per = prem - Math.max(K - S, 0);
      else per = Math.min(S, K) - se + prem; // covered_call
      return per * CONTRACT_MULT * q;
    }
    var lo = S0 * (1 - r), hi = S0 * (1 + r);
    var points = [];
    for (var i = 0; i < N; i++) {
      var S = lo + (hi - lo) * i / (N - 1);
      points.push({ price: S, pl: plAt(S) });
    }
    var be;
    if (leg.kind === "long_call") be = K + prem;
    else if (leg.kind === "long_put" || leg.kind === "short_put") be = K - prem;
    else be = se - prem;
    var breakevens = isFiniteNum(be) && be >= lo && be <= hi ? [be] : [];
    var maxProfit, maxLoss;
    if (leg.kind === "long_call") {
      maxProfit = null;
      maxLoss = -prem * CONTRACT_MULT * q;
    } else if (leg.kind === "long_put") {
      maxProfit = (K - prem) * CONTRACT_MULT * q;
      maxLoss = -prem * CONTRACT_MULT * q;
    } else if (leg.kind === "short_put") {
      maxProfit = prem * CONTRACT_MULT * q;
      maxLoss = -(K - prem) * CONTRACT_MULT * q;
    } else {
      maxProfit = (K - se + prem) * CONTRACT_MULT * q;
      maxLoss = -(se - prem) * CONTRACT_MULT * q;
    }
    return {
      available: true, kind: leg.kind, points: points, breakevens: breakevens,
      maxProfit: maxProfit, maxLoss: maxLoss, spot: S0, strike: K, range: [lo, hi],
    };
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmtK(v) {
    return String(Math.round(v * 100) / 100);
  }
  function fmtPl(v) {
    if (!isFiniteNum(v)) return "-";
    var sign = v > 0 ? "+" : (v < 0 ? "-" : "");
    var abs = Math.abs(v);
    if (abs >= 1000000) return sign + "$" + (abs / 1000000).toFixed(1) + "M";
    if (abs >= 10000) return sign + "$" + (abs / 1000).toFixed(1) + "k";
    if (abs >= 1000) return sign + "$" + (abs / 1000).toFixed(2) + "k";
    return sign + "$" + abs.toFixed(0);
  }

  // Perfil OI/Volumen: barras horizontales por strike (calls verde a la
  // izquierda, puts roja a la derecha del eje central). Escala lineal por
  // maxAbs del modo. oi/volume null → marca "n/d" gris; 0 → tick en el eje.
  function renderOiProfileSvg(profile, mode) {
    if (!profile || profile.available !== true || !Array.isArray(profile.rows)) return "";
    var m = mode === "volume" ? "volume" : "oi";
    var maxV = m === "volume" ? profile.maxVol : profile.maxOi;
    if (!(maxV > 0)) {
      return '<div class="opt-oi-note">No ' + (m === "volume" ? "volume" : "OI") + " data.</div>";
    }
    var W = 560, rowH = 20, padT = 6, padB = 6, halfW = 210, centerGap = 22;
    var cx = W / 2;
    var n = profile.rows.length;
    var H = padT + padB + n * rowH;
    var maxBarW = halfW - centerGap - 6;
    var out = ['<svg viewBox="0 0 ' + W + " " + H + '" class="opt-oi-svg" xmlns="http://www.w3.org/2000/svg" role="img">'];
    out.push('<line x1="' + cx + '" y1="' + padT + '" x2="' + cx + '" y2="' + (H - padB) +
      '" stroke="#1c2230" stroke-width="1"/>');
    var spotY = null;
    if (isFiniteNum(profile.spot) && n > 1) {
      for (var si = 0; si < n - 1; si++) {
        var lo = profile.rows[si].strike, hi = profile.rows[si + 1].strike;
        if (profile.spot >= lo && profile.spot <= hi) {
          var idx = si + (profile.spot - lo) / (hi - lo);
          spotY = padT + (n - 1 - idx) * rowH + rowH / 2;
          break;
        }
      }
    }
    for (var i = 0; i < n; i++) {
      var row = profile.rows[i];
      var yMid = padT + (n - 1 - i) * rowH + rowH / 2;
      ["call", "put"].forEach(function (side) {
        var slot = row[side];
        if (!slot) return;
        var v = slot[m];
        var color = side === "call" ? "#22c55e" : "#ef4444";
        var barH = 12;
        if (v === null) {
          var mx = side === "call" ? cx - centerGap - maxBarW / 2 : cx + centerGap + maxBarW / 2;
          out.push('<text x="' + mx + '" y="' + (yMid + 3) + '" text-anchor="middle" font-size="9" fill="#4b5568">n/d</text>');
        } else if (v === 0) {
          var x0 = side === "call" ? cx - centerGap - 3 : cx + centerGap;
          out.push('<rect x="' + x0 + '" y="' + (yMid - barH / 2) + '" width="3" height="' + barH +
            '" fill="' + color + '" opacity="0.35" rx="1"/>');
        } else {
          var len = Math.max(1, (v / maxV) * maxBarW);
          var bx = side === "call" ? cx - centerGap - len : cx + centerGap;
          out.push('<rect x="' + bx + '" y="' + (yMid - barH / 2) + '" width="' + len.toFixed(1) +
            '" height="' + barH + '" fill="' + color + '" opacity="0.8" rx="1"/>');
        }
      });
      var isAtm = isFiniteNum(profile.spot) && Math.abs(row.strike - profile.spot) / profile.spot < 0.01;
      out.push('<text x="' + cx + '" y="' + (yMid + 3) + '" text-anchor="middle" font-size="9.5" font-weight="600" fill="' +
        (isAtm ? "#FFC72C" : "#8b93a7") + '">' + esc(fmtK(row.strike)) + "</text>");
    }
    if (spotY !== null) {
      out.push('<line x1="10" y1="' + spotY.toFixed(1) + '" x2="' + (W - 10) + '" y2="' + spotY.toFixed(1) +
        '" stroke="#FFC72C" stroke-width="1" stroke-dasharray="4 3" opacity="0.9"/>');
      out.push('<text x="' + (W - 10) + '" y="' + (spotY - 4).toFixed(1) +
        '" text-anchor="end" font-size="9" fill="#FFC72C">Spot ' + esc(fmtK(profile.spot)) + "</text>");
    }
    out.push("</svg>");
    return out.join("");
  }

  // Payoff: línea de P/L (verde sobre cero, roja bajo cero), línea cero,
  // triángulos de breakeven, línea vertical del spot. Ejes min/max etiquetados.
  function renderPayoffSvg(series) {
    if (!series || series.available !== true || !Array.isArray(series.points) || series.points.length < 2) {
      return "";
    }
    var W = 560, H = 220, padL = 56, padR = 16, padT = 18, padB = 24;
    var pts = series.points;
    var xMin = pts[0].price, xMax = pts[pts.length - 1].price;
    var yVals = pts.map(function (p) { return p.pl; });
    if (series.maxProfit !== null) yVals.push(series.maxProfit);
    if (series.maxLoss !== null) yVals.push(series.maxLoss);
    var yMin = Math.min.apply(null, yVals.concat([0]));
    var yMax = Math.max.apply(null, yVals.concat([0]));
    if (yMin === yMax) { yMin -= 1; yMax += 1; }

    // Proporción visual mínima (al menos 30% del lado opuesto) para que
    // el piso de pérdida o el techo de ganancia nunca se aplasten contra el eje.
    var posMax = Math.max(0, yMax);
    var negMin = Math.abs(Math.min(0, yMin));
    var effNeg = Math.max(negMin, posMax * 0.30);
    var effPos = Math.max(posMax, negMin * 0.30);
    var scaleYMin = -effNeg;
    var scaleYMax = effPos;
    var spanY = scaleYMax - scaleYMin;
    scaleYMin -= spanY * 0.05;
    scaleYMax += spanY * 0.05;

    function X(price) { return padL + (price - xMin) / (xMax - xMin) * (W - padL - padR); }
    function Y(pl) { return padT + (scaleYMax - pl) / (scaleYMax - scaleYMin) * (H - padT - padB); }
    var out = ['<svg viewBox="0 0 ' + W + " " + H + '" class="opt-payoff-svg" xmlns="http://www.w3.org/2000/svg" role="img">'];

    // Gradientes de área (sombra suave de ganancia/pérdida)
    out.push("<defs>");
    out.push('<linearGradient id="opt-payoff-grad-green" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="#22c55e" stop-opacity="0.25"/>' +
      '<stop offset="100%" stop-color="#22c55e" stop-opacity="0.02"/>' +
      "</linearGradient>");
    out.push('<linearGradient id="opt-payoff-grad-red" x1="0" y1="1" x2="0" y2="0">' +
      '<stop offset="0%" stop-color="#ef4444" stop-opacity="0.25"/>' +
      '<stop offset="100%" stop-color="#ef4444" stop-opacity="0.02"/>' +
      "</linearGradient>");
    out.push("</defs>");

    // Ejes de referencia sutiles
    out.push('<line x1="' + padL + '" y1="' + padT + '" x2="' + padL + '" y2="' + (H - padB) +
      '" stroke="#1c2230" stroke-width="1"/>');
    out.push('<line x1="' + padL + '" y1="' + (H - padB) + '" x2="' + (W - padR) + '" y2="' + (H - padB) +
      '" stroke="#1c2230" stroke-width="1"/>');

    // Línea de cero (continua limpia) y etiqueta $0
    var zeroY = Y(0).toFixed(1);
    out.push('<line x1="' + padL + '" y1="' + zeroY + '" x2="' + (W - padR) + '" y2="' + zeroY +
      '" stroke="#2a3145" stroke-width="1"/>');
    out.push('<text x="' + (padL - 6) + '" y="' + (Number(zeroY) + 3).toFixed(1) +
      '" text-anchor="end" font-size="8.5" fill="#64748B">$0</text>');

    // Segmentos por signo (verde >= 0, rojo < 0), cortados exactos en cruces.
    var runs = [];
    var cur = null;
    for (var i = 0; i < pts.length; i++) {
      var sign = pts[i].pl >= 0 ? 1 : -1;
      if (!cur || cur.sign !== sign) {
        var xpt = null;
        if (cur && i > 0) {
          var a = pts[i - 1], b = pts[i];
          if (a.pl !== b.pl) {
            var t = (0 - a.pl) / (b.pl - a.pl);
            xpt = { price: a.price + (b.price - a.price) * t, pl: 0 };
            cur.points.push(xpt);
          }
        }
        cur = { sign: sign, points: xpt ? [xpt] : [] };
        runs.push(cur);
      }
      cur.points.push(pts[i]);
    }
    runs.forEach(function (r) {
      if (r.points.length < 2) return;
      var color = r.sign >= 0 ? "#22c55e" : "#ef4444";
      var gradId = r.sign >= 0 ? "url(#opt-payoff-grad-green)" : "url(#opt-payoff-grad-red)";
      var lineD = r.points.map(function (p, idx) {
        return (idx ? "L" : "M") + X(p.price).toFixed(1) + " " + Y(p.pl).toFixed(1);
      }).join(" ");
      var startX = X(r.points[0].price).toFixed(1);
      var endX = X(r.points[r.points.length - 1].price).toFixed(1);
      var areaD = lineD + " L " + endX + " " + zeroY + " L " + startX + " " + zeroY + " Z";
      out.push('<path d="' + areaD + '" fill="' + gradId + '"/>');
      out.push('<path d="' + lineD + '" fill="none" stroke="' + color +
        '" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>');
    });

    // Breakevens
    series.breakevens.forEach(function (b) {
      var bx = X(b).toFixed(1), by = Y(0).toFixed(1);
      out.push('<path d="M ' + bx + " " + by + " l -4 7 l 8 0 Z" + '" fill="#FFC72C"/>');
      out.push('<text x="' + bx + '" y="' + (Number(by) + 16).toFixed(1) +
        '" text-anchor="middle" font-size="9" font-weight="600" fill="#FFC72C">' + esc(fmtK(b)) + "</text>");
    });

    // Spot line (dashed vertical)
    if (isFiniteNum(series.spot) && series.spot >= xMin && series.spot <= xMax) {
      var sx = X(series.spot).toFixed(1);
      out.push('<line x1="' + sx + '" y1="' + padT + '" x2="' + sx + '" y2="' + (H - padB) +
        '" stroke="#FFC72C" stroke-width="1.2" stroke-dasharray="3 3" opacity="0.6"/>');
    }

    // Eje X: precios min y max
    out.push('<text x="' + padL + '" y="' + (H - 6) + '" font-size="9" fill="#64748B">' + esc(fmtK(xMin)) + "</text>");
    out.push('<text x="' + (W - padR) + '" y="' + (H - 6) + '" text-anchor="end" font-size="9" fill="#64748B">' + esc(fmtK(xMax)) + "</text>");

    // Eje Y: P/L max y min (alineados a la derecha contra el margen izquierdo)
    out.push('<text x="' + (padL - 6) + '" y="' + Math.max(padT + 9, Number(Y(yMax).toFixed(1)) + 3) +
      '" text-anchor="end" font-size="8.5" fill="#64748B">' + esc(fmtPl(yMax)) + "</text>");
    out.push('<text x="' + (padL - 6) + '" y="' + Math.min(H - padB - 2, Number(Y(yMin).toFixed(1)) + 3) +
      '" text-anchor="end" font-size="8.5" fill="#64748B">' + esc(fmtPl(yMin)) + "</text>");

    out.push("</svg>");
    return out.join("");
  }

  return {
    computeOiProfile: computeOiProfile,
    computePayoff: computePayoff,
    payoffSeries: payoffSeries,
    renderOiProfileSvg: renderOiProfileSvg,
    renderPayoffSvg: renderPayoffSvg,
    DISPLAY_WINDOW_PCT: DISPLAY_WINDOW_PCT,
    MIN_DISPLAY_STRIKES: MIN_DISPLAY_STRIKES,
  };
});
