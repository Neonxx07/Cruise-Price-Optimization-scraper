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

// Mirrors config/settings.py's settings.goccl_default_guests_count. Neither
// side reads the booking's actual guest count from the page — both use
// this fixed default to convert offer-code comparison's "Average Per
// Person" price into a gross total comparable with currentPriceGross. Keep
// this in sync with the Python default if that ever changes.
const GOCCL_DEFAULT_GUESTS_COUNT = 2;

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
// Reads window.initialData — a JSON blob the booking page embeds on load
// with the full invoice/rate/category detail (same pattern as NCL's
// window.__preloaded_data). The CSS selectors this replaced (recorded via
// DevTools) didn't match anything on a real live booking: confirmed against
// booking DEMO02 that "[data-component='category-rate-header-rate-name']"
// and "booking-details-bar__category*" don't exist anywhere on the page —
// the rate/offer code in particular is never rendered as visible text at
// all, only present in this JSON (initialData.rate.code).
function fn_goccl_readCurrentPriceAndSelection() {
  try {
    const data = window.initialData;
    if (!data) return { ok: false, error: 'window.initialData not found on page' };

    const gross = (data.invoiceSummary && data.invoiceSummary.grossAmount && data.invoiceSummary.grossAmount.amount);
    const rate = data.rate || {};
    const category = data.category || {};
    const stateroomType = category.stateroomType || {};

    return {
      ok: true,
      currentPriceGross: gross != null ? Number(gross) : 0,
      currentOfferCode: rate.code || '',
      currentCategory: category.code || '',
      currentStateroomType: stateroomType.name || '',
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
// Confirmed against a real booking (DEMO02): each offer is a
// div.rate-code-tile carrying data-rate-code/data-rate-name directly as
// attributes, and each stateroom-type price is a
// button.rate-code-tile__price-button carrying data-rate-meta-name/
// data-rate-meta-price/data-rate-meta-soldout. Reading these attributes
// directly is exact and order-independent.
//
// This replaced an earlier button-index-position guess (a fixed
// UPPER_LOWER/INTERIOR/OCEAN_VIEW/BALCONY/SUITE list assumed to line up
// with button order) that silently misaligned columns whenever a cell
// was sold out/N-A and shifted the index — confirmed against real data:
// it reported a "BALCONY" candidate that was actually an OCEAN VIEW
// price, a stateroom downgrade masquerading as a same-category fare-code
// swap.
function fn_goccl_readOfferCodeComparison() {
  try {
    const parsePrice = (t) => { const c = (t || '').replace(/[^\d.]/g, ''); return c ? parseFloat(c) : 0; };

    const tiles = Array.from(document.querySelectorAll('div.rate-code-tile'));
    const results = [];
    for (const tile of tiles) {
      const offerCode = tile.getAttribute('data-rate-code') || '';
      const offerName = tile.getAttribute('data-rate-name') || '';

      const priceButtons = Array.from(tile.querySelectorAll('button.rate-code-tile__price-button'));
      for (const button of priceButtons) {
        if (button.getAttribute('data-rate-meta-soldout') === 'true') continue;
        const stateroomName = button.getAttribute('data-rate-meta-name') || '';
        const priceAttr = button.getAttribute('data-rate-meta-price');
        if (!priceAttr) continue;
        results.push({
          offerName, offerCode,
          stateroomType: stateroomName,
          pricePerPerson: parsePrice(priceAttr),
        });
      }
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
      current.currentPriceGross, comparison.offerCodes, GOCCL_DEFAULT_GUESTS_COUNT,
    );
    log('RESULT', result.status, `net=$${result.netSaving} | ${result.note}`);
    if (result.status === 'NO_SAVING') await cacheNoSaving('GOCCL', bookingId);
    return result;

  } catch (e) {
    log('ERROR', 'ERROR', e.message);
    return makeErrorResult(bookingId, priceCategory, 'GOCCL', e.message);
  }
}
