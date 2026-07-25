// calculator.js — CruiseIntel Optimization v6.3
function safeFloat(v) { const n = parseFloat(v); return isNaN(n) ? 0 : n; }
function round2(x) { return Math.round(safeFloat(x) * 100) / 100; }
function normStr(s) { return (s || '').trim().toUpperCase(); }

const ESPRESSO_FEE_TYPES = new Set([
  'VACATION_TOTAL', 'OBC_TOTAL', 'PORT_CHARGE', 'PORT_EXPENSES',
  'GOVERNMENT_TAX', 'TAXES_AND_FEES', 'NCF', 'NCCF', 'CRUISE', 'CRUISEFARE',
  'GRATUITIES', 'TAX', 'FEE'
]);
function espressoIsFee(item) {
  if (ESPRESSO_FEE_TYPES.has(normStr(item.type))) return true;
  const n = normStr(item.name || item.normalizedName || '');
  if (n.match(/^(NCCF|NCF|PORT|TAX|FEE|GOVERNMENT|GRATUIT)/)) return true;
  if (n.includes(' OBC') || n.endsWith('OBC') || n.startsWith('OBC ')) return true;
  return false;
}
function espressoGetTotal(items, type) {
  for (const i of items)
    if (i.paxId === 'total' && normStr(i.type) === type) return safeFloat(i.amount);
  return 0;
}
function espressoGetCruiseFare(items) {
  for (const i of items)
    if (i.paxId === 'total' && ['CRUISE', 'CRUISEFARE', 'cruise'].includes(i.type || ''))
      return safeFloat(i.amount);
  const SKIP = new Set(['VACATION_TOTAL', 'OBC_TOTAL', 'TAXES_AND_FEES', 'PORT_CHARGE', 'PORT_EXPENSES', 'GOVERNMENT_TAX', 'NCF', 'NCCF']);
  let best = 0;
  for (const i of items) {
    if (i.paxId !== 'total') continue;
    if (SKIP.has(normStr(i.type))) continue;
    const a = safeFloat(i.amount);
    if (a > best) best = a;
  }
  return best;
}
function espressoGetPackages(items) {
  return items.filter(i => i.paxId === 'total' && safeFloat(i.amount) > 0 && !espressoIsFee(i));
}

const NCL_ADDON_VALUES = {
  'wi-fi': 150, 'wifi': 150, 'internet': 150,
  'dining': 80, 'specialty dining': 80, 'restaurant': 80,
  'beverage': 200, 'bar': 200, 'drink': 200, 'open bar': 200,
  'excursion': 50, 'shore': 50,
};
function nclAddonValue(addonName) {
  const lower = (addonName || '').toLowerCase();
  const match = lower.match(/\$(\d+)/);
  if (match) return parseInt(match[1]);
  for (const [key, val] of Object.entries(NCL_ADDON_VALUES)) {
    if (lower.includes(key)) return val;
  }
  return 0;
}

const READDABLE_PATTERNS = [/email/i, /bonus/i, /promo/i, /loyalty/i, /coupon/i];
function isReAddable(fareName) { return READDABLE_PATTERNS.some(p => p.test(fareName)); }

// Minimum ratio of (price drop) to (OBC lost) before a repricing that
// forfeits OBC is treated as a genuine optimization rather than a wash.
const OBC_LOSS_MIN_RATIO = 3;

function calcConfidence(oldItems, newItems, net, oldTotal, lostPkgValue, obcChange) {
  try {
    const oc = espressoGetCruiseFare(oldItems);
    const nc = espressoGetCruiseFare(newItems);
    const fareChangePct = oc > 0 ? (nc - oc) / oc : 0;
    const netPct = oldTotal > 0 ? net / oldTotal : 0;
    let pts = 0;
    if (fareChangePct < -0.02) pts += 2;
    else if (fareChangePct < 0) pts += 1;
    else if (fareChangePct > 0.15) pts -= 2;
    else if (fareChangePct > 0.05) pts -= 1;
    if (netPct > 0.05) pts += 2;
    else if (netPct > 0.02) pts += 1;
    if (lostPkgValue <= 0) pts += 1;
    if (obcChange >= 0) pts += 1;
    const tbl = { '-2': 1, '-1': 1, '0': 2, '1': 2, '2': 2, '3': 3, '4': 4, '5': 5, '6': 5 };
    let score = tbl[String(Math.max(-2, Math.min(6, pts)))] || 3;
    if (fareChangePct >= 0.05 && score > 3) score = 3;
    if (fareChangePct > 0.10 && lostPkgValue > 0) score = Math.min(score, 2);
    return { score, fareChangePct: round2(fareChangePct * 100), oldCruise: oc, newCruise: nc };
  } catch (e) {
    return { score: 3, fareChangePct: 0, oldCruise: 0, newCruise: 0 };
  }
}

function calculateESPRESSO(raw, bookingId, priceCategory) {
  try {
    const data = raw.result || raw;
    const oldItems = (data.oldInvoice || {}).invoiceItems || [];
    const newItems = (data.newInvoice || {}).invoiceItems || [];

    const oldTotal = espressoGetTotal(oldItems, 'VACATION_TOTAL');
    const newTotal = espressoGetTotal(newItems, 'VACATION_TOTAL');
    const oldOBC = espressoGetTotal(oldItems, 'OBC_TOTAL');
    const newOBC = espressoGetTotal(newItems, 'OBC_TOTAL');

    const priceDrop = round2(oldTotal - newTotal);
    const obcChange = round2(newOBC - oldOBC);

    const oldPkgs = espressoGetPackages(oldItems);
    const newPkgNames = new Set(espressoGetPackages(newItems).map(i => normStr(i.name || i.normalizedName || '')).filter(Boolean));
    const lostPkgs = oldPkgs.filter(i => { const n = normStr(i.name || i.normalizedName || ''); return n && !newPkgNames.has(n); });
    const lostPkgValue = round2(lostPkgs.reduce((s, i) => s + safeFloat(i.amount), 0));
    const lostPkgNames = lostPkgs.map(i => i.name || i.normalizedName || '').filter(Boolean);

    const net = round2(priceDrop + obcChange - lostPkgValue);

    const normFare = s => normStr(s);
    const oldFareNames = (data.oldFares || []).map(f => f.name || '').filter(Boolean);
    const newFareNames = (data.newFares || []).map(f => f.name || '').filter(Boolean);
    const newFareSet = new Set(newFareNames.map(normFare));
    const oldFareSet = new Set(oldFareNames.map(normFare));
    const allLostFares = oldFareNames.filter(f => !newFareSet.has(normFare(f)));
    const reAddableFares = allLostFares.filter(f => isReAddable(f));
    const trulyLostFares = allLostFares.filter(f => !isReAddable(f));
    const gainedFares = newFareNames.filter(f => !oldFareSet.has(normFare(f)));

    const reAddNote = reAddableFares.length ? ' — re-add: ' + reAddableFares.join(', ') : '';
    let status, note;
    if (net > 0 && lostPkgValue > 0 && net < lostPkgValue) {
      // Net saving is positive on paper, but it's smaller than the value
      // of a package being given up to get it — not a real optimization.
      status = 'TRAP';
      note = 'trap - losing $' + Math.round(lostPkgValue) + ' perk for only $' + Math.round(net) + ' net' + reAddNote;
    } else if (net > 0 && obcChange < 0 && priceDrop < Math.abs(obcChange) * OBC_LOSS_MIN_RATIO) {
      // Net is positive on paper, but a chunk of it is OBC being
      // forfeited rather than a real fare reduction — only worth
      // recommending once the price drop clears the OBC lost by
      // OBC_LOSS_MIN_RATIO.
      status = 'NO_SAVING';
      note = 'no saving — $' + Math.round(priceDrop) + ' drop costs $' + Math.round(Math.abs(obcChange)) + ' OBC (need ' + OBC_LOSS_MIN_RATIO + 'x)' + reAddNote;
    } else if (net > 0) { status = 'OPTIMIZATION'; note = 'optimized $' + Math.round(net) + reAddNote; }
    else if (priceDrop > 0 && net <= 0) { status = 'TRAP'; note = 'trap - do not reprice' + reAddNote; }
    else { status = 'NO_SAVING'; note = 'no saving' + (reAddableFares.length ? ' — can re-add: ' + reAddableFares.join(', ') : ''); }

    const conf = calcConfidence(oldItems, newItems, net, oldTotal, lostPkgValue, obcChange);

    return {
      cruiseLine: 'ESPRESSO', status, note, bookingId, priceCategory, oldTotal, newTotal, priceDrop, obcChange,
      lostPkgValue, lostPkgNames, netSaving: net, lostFares: trulyLostFares, reAddableFares, gainedFares,
      confidence: conf.score, oldCruiseFare: conf.oldCruise, newCruiseFare: conf.newCruise, fareChangePct: conf.fareChangePct, error: null
    };
  } catch (e) { return { cruiseLine: 'ESPRESSO', status: 'ERROR', error: e.message, bookingId, priceCategory }; }
}

function calculateNCL(bookingId, priceCategory, invoiceTotal, newResTotal, addons, oldPromos, newPromos) {
  try {
    const oldTotal = round2(invoiceTotal);
    const newTotal = round2(newResTotal);
    const priceDrop = round2(oldTotal - newTotal);

    let lostAddonValue = 0;
    const lostAddonNames = [];
    const oldPromoStr = (oldPromos || '').toUpperCase();
    const newPromoStr = (newPromos || '').toUpperCase();
    const lostFOBC = oldPromoStr.includes('FOBC') && !newPromoStr.includes('FOBC');

    if (addons && addons.length > 0) {
      const uniqueAddons = [];
      const seen = new Set();
      for (const a of addons) { if (!seen.has(a.name)) { seen.add(a.name); uniqueAddons.push(a); } }
      for (const a of uniqueAddons) {
        const isOBCCert = /On-Board Credit Certificate/i.test(a.name) || /OBC Certificate/i.test(a.name);
        if (isOBCCert && lostFOBC) {
          const val = nclAddonValue(a.name);
          if (val > 0) { lostAddonValue += val; lostAddonNames.push(`${a.name} ($${val})`); }
        }
      }
    }
    lostAddonValue = round2(lostAddonValue);
    const net = round2(priceDrop - lostAddonValue);

    let status, note;
    if (net > 0) { status = 'OPTIMIZATION'; note = 'NCL optimized $' + Math.round(net) + (lostAddonNames.length ? ' — verify addons: ' + lostAddonNames.join(', ') : ''); }
    else if (priceDrop > 0 && net <= 0) { status = 'TRAP'; note = 'NCL trap — price drop offset by addon loss: ' + lostAddonNames.join(', '); }
    else { status = 'NO_SAVING'; note = 'NCL no saving'; }

    let confidence = 3;
    if (priceDrop > 0 && lostAddonValue === 0) confidence = 5;
    else if (priceDrop > 0 && lostAddonValue < priceDrop) confidence = 4;
    else if (priceDrop > 0 && lostAddonValue >= priceDrop) confidence = 2;
    else confidence = 2;

    return {
      cruiseLine: 'NCL', status, note, bookingId, priceCategory, oldTotal, newTotal, priceDrop, obcChange: 0,
      lostPkgValue: lostAddonValue, lostPkgNames: lostAddonNames, netSaving: net, lostFares: [], reAddableFares: [], gainedFares: [],
      confidence, oldCruiseFare: 0, newCruiseFare: 0, fareChangePct: 0, error: null
    };
  } catch (e) { return { cruiseLine: 'NCL', status: 'ERROR', error: e.message, bookingId, priceCategory }; }
}

// GoCCL's automatic discovery only reads the offer-code comparison screen
// (average-per-person prices by stateroom type) — never the confirmed,
// per-category GROSS AMOUNT that only appears after a human-reviewed
// preview click-through. So unlike ESPRESSO/NCL, this can only surface an
// UNCONFIRMED candidate offer code, capped at 1-star confidence.
const GOCCL_CANDIDATE_CONFIDENCE = 1;

function calculateGOCCL(bookingId, priceCategory, currentStateroomType, currentOfferCode, currentPriceGross, availableOfferCodes, guestsCount) {
  try {
    guestsCount = guestsCount || 2;
    const candidates = (availableOfferCodes || []).filter(o =>
      o.stateroomType === currentStateroomType &&
      o.offerCode !== currentOfferCode &&
      safeFloat(o.pricePerPerson) > 0
    );

    const oldTotal = round2(currentPriceGross);

    if (!candidates.length) {
      return {
        cruiseLine: 'GOCCL', status: 'NO_SAVING',
        note: `no saving — no cheaper offer code found for ${currentStateroomType}`,
        bookingId, priceCategory, oldTotal, newTotal: oldTotal, priceDrop: 0, obcChange: 0, netSaving: 0,
        lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0, error: null
      };
    }

    const cheapest = candidates.reduce((a, b) => safeFloat(a.pricePerPerson) <= safeFloat(b.pricePerPerson) ? a : b);
    const newTotal = round2(safeFloat(cheapest.pricePerPerson) * guestsCount);
    const priceDrop = round2(oldTotal - newTotal);

    if (priceDrop <= 0) {
      return {
        cruiseLine: 'GOCCL', status: 'NO_SAVING',
        note: "no saving — cheapest candidate offer code isn't actually lower once guest count is applied",
        bookingId, priceCategory, oldTotal, newTotal, priceDrop: 0, obcChange: 0, netSaving: 0,
        lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0, error: null
      };
    }

    return {
      cruiseLine: 'GOCCL', status: 'OPTIMIZATION',
      note: `candidate $${Math.round(priceDrop)} — offer code '${cheapest.offerCode || ''}' (${cheapest.offerName || ''}) — UNCONFIRMED, preview before repricing`,
      bookingId, priceCategory, oldTotal, newTotal, priceDrop, obcChange: 0, netSaving: priceDrop,
      lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0,
      confidence: GOCCL_CANDIDATE_CONFIDENCE, oldCruiseFare: 0, newCruiseFare: 0, fareChangePct: 0, error: null
    };
  } catch (e) { return { cruiseLine: 'GOCCL', status: 'ERROR', error: e.message, bookingId, priceCategory }; }
}

function makeWLTResult(bookingId, priceCategory, cruiseLine) { return { cruiseLine, status: 'WLT', note: 'WLT - waitlisted', bookingId, priceCategory, netSaving: 0, oldTotal: 0, newTotal: 0, priceDrop: 0, obcChange: 0, lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0, error: null }; }
function makePaidInFullResult(bookingId, priceCategory, cruiseLine, oldTotal) { return { cruiseLine, status: 'PAID_IN_FULL', note: '💳 Fully paid — repricing unavailable', bookingId, priceCategory, oldTotal: oldTotal || 0, newTotal: 0, priceDrop: 0, obcChange: 0, netSaving: 0, lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0, error: null }; }
// Confirmed via the page's own displayed price (not the reprice-modal
// API, which returns a short/non-JSON body in exactly this scenario).
function makeNoPriceChangeResult(bookingId, priceCategory, cruiseLine, price) { const p = Number(price) || 0; return { cruiseLine, status: 'NO_SAVING', note: `no saving — price unchanged ($${p.toFixed(2)})`, bookingId, priceCategory, oldTotal: p, newTotal: p, priceDrop: 0, obcChange: 0, netSaving: 0, lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0, error: null }; }
function makeSkippedResult(bookingId, priceCategory, cruiseLine, checkedHoursAgo) { const h = Math.round(checkedHoursAgo * 10) / 10; return { cruiseLine, status: 'SKIPPED_TODAY', note: `Checked ${h}h ago — no saving cached`, bookingId, priceCategory, oldTotal: 0, newTotal: 0, priceDrop: 0, obcChange: 0, netSaving: 0, lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0, error: null }; }
function makeErrorResult(bookingId, priceCategory, cruiseLine, errorMsg) { return { cruiseLine, status: 'ERROR', note: errorMsg, error: errorMsg, bookingId, priceCategory, oldTotal: 0, newTotal: 0, priceDrop: 0, obcChange: 0, netSaving: 0, lostFares: [], gainedFares: [], reAddableFares: [], lostPkgNames: [], lostPkgValue: 0, confidence: 0 }; }