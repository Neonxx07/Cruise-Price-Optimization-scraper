// adapter_goccl.js — CruiseIntel Optimization
// GoCCL Navigator — Carnival Cruise Line travel-agent portal
//
// Read-only discovery: search booking -> read current price/offer/category ->
// Modify Booking -> Change Offer/Rate -> read all offer codes x stateroom-type
// prices. NEVER clicks a final purchase/confirm button, and never enters any
// kind of "edit mode" lock the way NCL does — there is nothing to unlock here.
//
// This mirrors platform/scraper/goccl.py's check_booking(): it can only
// surface an UNCONFIRMED candidate offer code (see calculateGOCCL in
// calculator.js for why), never a confirmed net saving.

const GOCCL_SEARCH_URL = 'https://www.goccl.com/BookingEngine/BookingSearch/SearchForReservations.aspx';

// ── Search for a booking ───────────────────────────────────────
function fn_goccl_search(bookingId) {
  const input = document.getElementById('ctl00_DefaultContent_txtBookingNumber');
  if (!input) return { ok: false, error: 'Booking number input not found' };
  input.value = bookingId;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));

  const searchBtn = document.getElementById('ctl00_DefaultContent_btnSearchBookingNumber');
  if (searchBtn) { searchBtn.click(); return { ok: true, method: 'search-button' }; }

  const form = input.closest('form');
  if (form) { form.submit(); return { ok: true, method: 'form.submit()' }; }

  return { ok: false, error: 'Could not find a way to submit the search' };
}

// ── Read the current booking's price, offer code, category, stateroom ──
function fn_goccl_readCurrentPriceAndSelection() {
  try {
    const parsePrice = (t) => { const c = (t || '').replace(/[^\d.]/g, ''); return c ? parseFloat(c) : 0; };

    const priceEl = document.querySelector('span.price__number');
    const offerEl = document.querySelector("[data-component='category-rate-header-rate-name']");
    const categoryEl = document.querySelector(
      'article.booking-details-bar__category--category span.booking-details-bar__category-value'
    );
    const stateroomEl = document.querySelector('span.booking-details-bar__category-metaname');

    if (!priceEl || !offerEl || !categoryEl || !stateroomEl) {
      return { ok: false, error: 'Booking summary bar elements not found' };
    }

    return {
      ok: true,
      currentPriceGross: parsePrice(priceEl.textContent),
      currentOfferCode: (offerEl.textContent || '').trim(),
      currentCategory: (categoryEl.textContent || '').trim(),
      currentStateroomType: (stateroomEl.textContent || '').trim(),
    };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// ── Click "Modify Booking" ──────────────────────────────────────
function fn_goccl_openModifyBooking() {
  const link = Array.from(document.querySelectorAll('a'))
    .find(a => a.textContent.trim() === 'Modify Booking');
  if (!link) return { ok: false, error: '"Modify Booking" link not found' };
  link.click();
  return { ok: true };
}

// ── Click "Change Offer/Rate" ───────────────────────────────────
function fn_goccl_openChangeOfferRate() {
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => /change offer\/rate/i.test(b.textContent || ''));
  if (!btn) return { ok: false, error: '"Change Offer/Rate" button not found' };
  btn.click();
  return { ok: true };
}

// ── Read the offer-code comparison screen ───────────────────────
// Confirmed container: section.rate__container, with stateroom-type prices
// as numbered buttons (1=Upper/Lower, 2=Interior, 3=Ocean View, 4=Balcony,
// 5=Suite — order confirmed from a recorded click on button index 4
// landing on Balcony).
function fn_goccl_readOfferCodeComparison() {
  try {
    const stateroomOrder = ['UPPER_LOWER', 'INTERIOR', 'OCEAN_VIEW', 'BALCONY', 'SUITE'];
    const parsePrice = (t) => { const c = (t || '').replace(/[^\d.]/g, ''); return c ? parseFloat(c) : 0; };

    const rows = Array.from(document.querySelectorAll('section.rate__container > div > div'));
    const results = [];
    for (const row of rows) {
      const nameEl = row.querySelector('h6, .offer-name');
      const offerName = nameEl ? nameEl.textContent.trim() : '';
      const codeEl = row.querySelector(".offer-code, [data-component='offer-code']");
      const offerCode = codeEl ? codeEl.textContent.trim() : '';

      const buttons = Array.from(row.querySelectorAll('button'));
      buttons.forEach((button, idx) => {
        if (idx >= stateroomOrder.length) return;
        const priceEl = button.querySelector('span.price__number');
        if (!priceEl) return;
        results.push({
          offerName, offerCode,
          stateroomType: stateroomOrder[idx],
          pricePerPerson: parsePrice(priceEl.textContent),
        });
      });
    }
    return { ok: true, offerCodes: results };
  } catch (e) {
    return { ok: false, error: e.message, offerCodes: [] };
  }
}

// ── Optimize-flow helpers (human-reviewed, never auto-confirmed) ──
// Click the candidate offer code's price button, then CONTINUE — this
// mirrors platform/scraper/goccl.py's preview_fare_code() first step.
// Stops on the category table for the human to pick "Keep Same Stateroom"
// and review before ever touching a purchase/confirm control.
function fn_goccl_selectOfferAndContinue(offerCode) {
  try {
    const target = (offerCode || '').toUpperCase();
    const btn = Array.from(document.querySelectorAll('button'))
      .find(b => ((b.getAttribute('aria-label') || b.textContent || '').toUpperCase()).includes(target));
    if (!btn) return { ok: false, error: `Offer code button for "${offerCode}" not found` };
    btn.scrollIntoView({ block: 'center' });
    btn.click();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function fn_goccl_clickContinue() {
  const btn = Array.from(document.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim().toUpperCase() === 'CONTINUE');
  if (!btn) return { ok: false, error: 'CONTINUE button not found' };
  btn.click();
  return { ok: true };
}

// ═══════════════════════════════════════════════════════════════
// MAIN GOCCL BOOKING HANDLER
// Called by background.js router. Read-only end to end — nothing to unlock.
// ═══════════════════════════════════════════════════════════════
async function handleGOCCLBooking(bookingId, tabId) {
  const log = (step, status, detail) => _bgLog(bookingId, step, status, detail);
  let priceCategory = null;

  try {
    log('NAVIGATE', 'INFO', 'Opening GoCCL Navigator search...');
    await navigateTo(tabId, GOCCL_SEARCH_URL);
    await waitForEl(tabId, '#ctl00_DefaultContent_txtBookingNumber', 15000);
    log('NAVIGATE', 'OK', 'Search page ready');

    log('SEARCH', 'INFO', `Searching for booking ${bookingId}...`);
    const sr = await runInPage(tabId, fn_goccl_search, bookingId);
    if (!sr?.ok) throw new Error('GoCCL search submit failed: ' + (sr?.error || 'unknown'));
    await waitForEl(tabId, '#booked-root', 20000);
    log('SEARCH', 'OK', 'Booking summary loaded');

    const current = await runInPage(tabId, fn_goccl_readCurrentPriceAndSelection);
    if (!current?.ok) throw new Error('Could not read current price/selection: ' + (current?.error || 'unknown'));
    priceCategory = current.currentCategory;
    log('BOOKING_INFO', 'OK',
      `cat="${priceCategory}" stateroom="${current.currentStateroomType}" offer="${current.currentOfferCode}" gross=$${current.currentPriceGross}`);

    log('MODIFY_BOOKING', 'INFO', 'Opening Modify Booking...');
    const modifyResult = await runInPage(tabId, fn_goccl_openModifyBooking);
    if (!modifyResult?.ok) throw new Error(modifyResult?.error || 'Modify Booking failed');
    await sleep(800);

    log('CHANGE_OFFER_RATE', 'INFO', 'Opening Change Offer/Rate...');
    const changeRateResult = await runInPage(tabId, fn_goccl_openChangeOfferRate);
    if (!changeRateResult?.ok) throw new Error(changeRateResult?.error || 'Change Offer/Rate failed');
    await waitForEl(tabId, "section.rate__container, div[class*='rate']", 15000);
    log('CHANGE_OFFER_RATE', 'OK', 'Offer-code comparison loaded');

    const comparison = await runInPage(tabId, fn_goccl_readOfferCodeComparison);
    if (!comparison?.ok) throw new Error('Could not read offer-code comparison: ' + (comparison?.error || 'unknown'));
    log('OFFER_CODES', 'OK', `${comparison.offerCodes.length} price row(s) found`);

    const result = calculateGOCCL(
      bookingId, priceCategory, current.currentStateroomType, current.currentOfferCode,
      current.currentPriceGross, comparison.offerCodes, 2,
    );
    log('RESULT', result.status, `net=$${result.netSaving} | ${result.note}`);
    if (result.status === 'NO_SAVING') await cacheNoSaving('GOCCL', bookingId);
    return result;

  } catch (e) {
    log('ERROR', 'ERROR', e.message);
    return makeErrorResult(bookingId, priceCategory, 'GOCCL', e.message);
  }
}
