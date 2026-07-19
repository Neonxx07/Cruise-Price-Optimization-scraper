// adapter_ncl.js — CruiseIntel Optimization v6.3
// Norwegian Cruise Line via SeaWeb portal
//
// ⚠️  THE 30-MINUTE LOCK — MUST READ
// When the bot clicks "Switch to Edit Mode", NCL locks the booking for 30 min.
// The finally block ALWAYS calls fn_ncl_cancelEdit to release it, even on error.

const NCL_SEARCH_URL = 'https://seawebagents.ncl.com/tva/search/';

// ── Search for a booking ───────────────────────────────────────
// CONFIRMED from scanner: button id="lookup-button" class="action swbutton"
// The form also has a separate submit button — both approaches used as fallback.
function fn_ncl_search(bookingId) {
  const input = document.getElementById('SWXMLForm_SearchReservation_ResID');
  if (!input) return { ok:false, error:'ResID input not found' };
  input.value = bookingId;
  input.dispatchEvent(new Event('input',  { bubbles:true }));
  input.dispatchEvent(new Event('change', { bubbles:true }));

  // PRIMARY: confirmed selector from live scanner data
  const lookupBtn = document.getElementById('lookup-button');
  if (lookupBtn) { lookupBtn.click(); return { ok:true, method:'lookup-button' }; }

  // FALLBACK 1: submit button inside the search form only
  const form = input.closest('form');
  if (form) {
    const submitBtn = form.querySelector('[type="submit"]');
    if (submitBtn) { submitBtn.click(); return { ok:true, method:'form-submit' }; }
    form.submit(); return { ok:true, method:'form.submit()' };
  }

  // FALLBACK 2: any visible search button
  const allBtns = Array.from(document.querySelectorAll('button, input[type="submit"]'));
  const searchBtn = allBtns.find(b =>
    (b.textContent || b.value || '').toLowerCase().includes('go') ||
    (b.textContent || b.value || '').toLowerCase().includes('search')
  );
  if (searchBtn) { searchBtn.click(); return { ok:true, method:'text-match' }; }

  return { ok:false, error:'Could not find submit button for search form' };
}

// ── Check for NCL error messages after search ──────────────────
function fn_ncl_checkSearchErrors() {
  const selectors = ['.error', '.alert', '#pageMessages', '.swmessage',
                     '[class*="error"]', '[class*="alert"]', '.field-error'];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const text = (el.innerText || el.textContent || '').trim();
      if (text.length > 3) return text;
    }
  }
  return null;
}

// ── Check if we are on the booking summary page ────────────────
// Returns true if ANY of the expected summary-page elements are present.
// Used to confirm that search navigation completed successfully.
function fn_ncl_isOnSummaryPage() {
  return !!(
    document.getElementById('res-switch-edit')      || // VIEW MODE button
    document.getElementById('res-edit-save')         || // EDIT MODE store button
    document.querySelector('.item.current')           || // booking sidebar item
    document.querySelector('[class*="ReservationSummary"]') ||
    document.querySelector('h2, h3')?.innerText?.includes?.('Reservation Summary')
  );
}

// ── Read booking state from __preloaded_data ───────────────────
function fn_ncl_readPreloadedData() {
  try {
    const d = window.__preloaded_data;
    if (!d) return { ok:false, error:'__preloaded_data not found on this page' };
    return {
      ok:           true,
      resId:        d.ResID || d.bi?.ResID,
      isPaid:       d.bi?.IsPaid || false,
      isLocked:     d.bi?.IsLocked || false,
      isEditMode:   d.IsEditMode || d.bi?.IsEditMode || false,
      category:     d.bi?.Category || d.category || null,
      invoiceTotal: d.bi?.InvoiceTotal || d.baseInvoice?.INVOICE_TOTAL || 0,
      grossDue:     d.bi?.GrossDue    || d.baseInvoice?.REAL_GROSS_DUE || 0,
      netDue:       d.baseInvoice?.REAL_NET_DUE || 0,
      shipCode:     d.bi?.ShipCode || null,
      ship:         d.bi?.Ship     || null,
      promos:       d.bi?.guests
                      ? Object.values(d.bi.guests || {}).map(g => g.Promos || '').join(',')
                      : '',
      currentPromos: (() => {
        const item = document.querySelector('.item.current');
        if (!item) return '';
        const row = Array.from(item.querySelectorAll('.row'))
                      .find(r => r.textContent.includes('Curr. Promos'));
        return row?.querySelector('.value')?.textContent?.trim() || '';
      })(),
      guestCount: d.bi?.guests ? Object.keys(d.bi.guests || {}).length : 1
    };
  } catch(e) {
    return { ok:false, error:e.message };
  }
}

// ── Switch to Edit Mode ────────────────────────────────────────
function fn_ncl_switchToEditMode() {
  const btn = document.getElementById('res-switch-edit');
  if (!btn) return { ok:false, notFound:true, error:'#res-switch-edit not found — may already be in edit mode' };
  btn.click();
  return { ok:true };
}

// ── Check if edit mode is confirmed ───────────────────────────
function fn_ncl_checkEditMode() {
  const storeBtn = document.getElementById('res-edit-save');
  const cancelBtn = document.getElementById('res-edit-cancel');
  // Edit mode: Store and Cancel Edit buttons are present
  const isEdit = !!(storeBtn && cancelBtn);
  const isView = !!document.getElementById('res-switch-edit');
  return { isEditMode: isEdit, isViewMode: isView, hasStore: !!storeBtn };
}

// ── ALWAYS call this in finally — releases the 30-min lock ────
function fn_ncl_cancelEdit() {
  // PRIMARY: confirmed ID from scanner
  const cancelBtn = document.getElementById('res-edit-cancel');
  if (cancelBtn) { cancelBtn.click(); return { ok:true, method:'#res-edit-cancel' }; }

  // FALLBACK 1: text match
  const byText = Array.from(document.querySelectorAll('a, button'))
                   .find(el => el.textContent.trim().toUpperCase() === 'CANCEL EDIT');
  if (byText) { byText.click(); return { ok:true, method:'text-CANCEL EDIT' }; }

  // FALLBACK 2: direct URL navigation (most reliable)
  const viewModeLink = document.querySelector('a[href*="viewMode"]');
  if (viewModeLink) { viewModeLink.click(); return { ok:true, method:'viewMode link' }; }

  // FALLBACK 3: navigate programmatically
  const currentPath = window.location.pathname;
  if (currentPath.includes('/edit/')) {
    const viewUrl = window.location.href.replace('/edit/', '/view/').split('?')[0] + 'doform/viewMode?';
    window.location.href = viewUrl;
    return { ok:true, method:'programmatic navigate' };
  }

  return { ok:false, error:'No cancel edit mechanism found' };
}

// ── Scrape addons table from summary page ─────────────────────
function fn_ncl_scrapeAddons() {
  try {
    let table = null;
    // Try precise confirmed selector first
    table = document.querySelector('#transformation > div > div > div:nth-child(3) > div.content.clearfix > table');
    // Fallback: any table with Addon Name header
    if (!table) {
      for (const t of document.querySelectorAll('table')) {
        const headers = Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim());
        if (headers.some(h => h.includes('Addon Name') || h.includes('Addon'))) {
          table = t; break;
        }
      }
    }
    if (!table) return { ok:true, addons:[], warning:'Addons table not found' };

    const addons = [];
    for (const row of table.querySelectorAll('tbody tr')) {
      const cells = row.querySelectorAll('td, th');
      if (cells.length >= 2) {
        const name = cells[1]?.textContent?.trim();
        const qty  = parseInt(cells[2]?.textContent?.trim()) || 1;
        if (name && name.length > 2) addons.push({ name, qty });
      }
    }
    return { ok:true, addons };
  } catch(e) {
    return { ok:true, addons:[], error:e.message };
  }
}

// ── Navigate to Category tab ───────────────────────────────────
function fn_ncl_clickCategoryTab() {
  // PRIMARY: confirmed from scanner — tab link with href containing /agent-edit-category/
  const categoryLink = Array.from(document.querySelectorAll('a'))
    .find(a => a.href?.includes('/agent-edit-category/') && a.textContent.trim() === 'Category');
  if (categoryLink) { categoryLink.click(); return { ok:true }; }

  // FALLBACK: any link to the category page
  const fallback = document.querySelector('a[href*="agent-edit-category"]');
  if (fallback) { fallback.click(); return { ok:true, method:'fallback' }; }

  return { ok:false, error:'Category tab link not found' };
}

// ── Read all categories from VX._form_12 (SlickGrid data model) ──
// This is the key insight: the entire category dataset lives in a JS object.
// No DOM scraping needed. The virtualized table is irrelevant.
function fn_ncl_readCategoryData() {
  try {
    const categories = window.VX?.get('_form_12');
    if (!categories || !Array.isArray(categories)) {
      return { ok:false, error:'VX._form_12 not available — may not be on Category page yet' };
    }
    const currentVal = window.VX?.get('_form_10')?.value?.[0] || null;
    return {
      ok: true,
      currentCategory: currentVal,
      categories: categories.map(c => ({
        category:        c.Category,
        resTotal:        parseFloat(c.ResTotal)  || 0,
        status:          c.Status,
        hasAvailability: c.HasAvailability,
        cabinAvailable:  c.CabinAvailable || 0,
        currentPromo:    c.CurrentPromo || '',
        description:     (c.Description || '').trim()
      }))
    };
  } catch(e) {
    return { ok:false, error:e.message };
  }
}

// ── Select a category via SlickGrid ───────────────────────────
async function fn_ncl_selectCategory(targetCategory) {
  try {
    const categories = window.VX?.get('_form_12');
    if (!categories) return { ok:false, error:'VX._form_12 not found' };

    const targetIdx = categories.findIndex(c => c.Category === targetCategory);
    if (targetIdx < 0) return { ok:false, error:`Category ${targetCategory} not found in data model` };

    const cat = categories[targetIdx];
    if (!cat.HasAvailability) return { ok:false, error:`Category ${targetCategory} has no availability` };

    // Scroll SlickGrid to render the target row
    try {
      const gridEl = document.querySelector('[id*="SWXMLForm_SelectCategory_category"]');
      if (gridEl && window.$ && window.$(gridEl).data) {
        const grid = window.$(gridEl).data('SlickGrid');
        if (grid?.scrollRowIntoView) {
          grid.scrollRowIntoView(targetIdx, false);
          await new Promise(res => setTimeout(res, 400));
        }
      }
    } catch(scrollErr) { /* SlickGrid scroll failed — try DOM anyway */ }

    await new Promise(res => setTimeout(res, 400));

    // Find rendered row and click its Select button
    const viewport = document.querySelector('.slick-viewport');
    if (!viewport) return { ok:false, error:'.slick-viewport not found in DOM' };

    let selectBtn = null;
    for (const row of viewport.querySelectorAll('.slick-row')) {
      const catCell = row.querySelector('.slick-cell.l0, .slick-cell:first-child');
      const catLink = catCell?.querySelector('a.infolink, a[href*="go/category"]');
      if (catLink && catLink.textContent.trim() === targetCategory) {
        selectBtn = row.querySelector('a[data-link-action="select"], a.navlink');
        break;
      }
    }

    if (!selectBtn) return { ok:false, error:`Select button for ${targetCategory} not visible — SlickGrid may not have rendered the row` };

    selectBtn.click();
    await new Promise(res => setTimeout(res, 600));
    return { ok:true, category:targetCategory };
  } catch(e) {
    return { ok:false, error:e.message };
  }
}

// ── Read new ResTotal from grid after selection ────────────────
function fn_ncl_readNewResTotalFromGrid(newCategory) {
  try {
    const cats = window.VX?.get('_form_12');
    if (!cats) return { ok:false, error:'VX._form_12 not available' };
    const cat = cats.find(c => c.Category === newCategory);
    if (!cat) return { ok:false, error:`Category ${newCategory} not in grid data` };
    return { ok:true, resTotal: parseFloat(cat.ResTotal) || 0, currentPromo: cat.CurrentPromo || '' };
  } catch(e) {
    return { ok:false, error:e.message };
  }
}

// ═══════════════════════════════════════════════════════════════
// MAIN NCL BOOKING HANDLER
// Called by background.js router. ALWAYS unlocks in finally.
// ═══════════════════════════════════════════════════════════════
async function handleNCLBooking(bookingId, tabId) {
  const log = (step, status, detail) => _bgLog(bookingId, step, status, detail);

  let inEditMode   = false;
  let addons       = [];
  let oldTotal     = 0;
  let currentCategory = null;
  let currentPromos   = '';

  try {
    // ── STEP 1: Navigate to NCL search ─────────────────────────
    log('NAVIGATE', 'INFO', 'Opening NCL SeaWeb search page...');
    await navigateTo(tabId, NCL_SEARCH_URL);
    await waitForEl(tabId, '#SWXMLForm_SearchReservation_ResID', 15000);
    log('NAVIGATE', 'OK', 'Search page ready');

    // ── STEP 2: Submit booking ID ───────────────────────────────
    log('SEARCH', 'INFO', `Searching for booking ${bookingId}...`);
    const sr = await runInPage(tabId, fn_ncl_search, bookingId);
    if (!sr?.ok) throw new Error('NCL search submit failed: ' + (sr?.error || 'unknown'));
    log('SEARCH', 'OK', `Search submitted (${sr.method || 'unknown method'})`);

    // ── STEP 3: Wait for summary page (robust multi-selector) ───
    // Increased to 20s. Also catches NCL error messages if booking not found.
    try {
      await waitForEl(
        tabId,
        '.item.current, #res-switch-edit, #res-edit-save, [class*="ReservationSummary"]',
        20000
      );
    } catch(timeoutErr) {
      // Check if NCL showed an error instead (invalid booking ID etc.)
      const errorText = await runInPage(tabId, fn_ncl_checkSearchErrors);
      if (errorText) throw new Error('NCL portal error: ' + errorText);
      throw new Error('Timeout waiting for booking summary page — check NCL login and booking ID');
    }
    log('SUMMARY', 'OK', 'Booking summary page loaded');

    // ── STEP 4: Read booking state from JS data ─────────────────
    const preload = await runInPage(tabId, fn_ncl_readPreloadedData);
    if (!preload?.ok) throw new Error('Could not read __preloaded_data: ' + (preload?.error || 'unknown'));

    if (preload.isPaid) {
      log('SKIP', 'INFO', 'Booking is fully paid — repricing unavailable');
      return makePaidInFullResult(bookingId, preload.category, 'NCL', preload.invoiceTotal);
    }

    currentCategory = preload.category;
    oldTotal        = preload.invoiceTotal;
    currentPromos   = preload.currentPromos || '';
    log('BOOKING_INFO', 'OK', `cat="${currentCategory}" total=$${oldTotal} promos="${currentPromos}"`);

    // ── STEP 5: Scrape addons BEFORE entering edit mode ─────────
    const addonResult = await runInPage(tabId, fn_ncl_scrapeAddons);
    addons = addonResult?.addons || [];
    log('ADDONS', 'OK', `Found ${addons.length} addon line${addons.length !== 1 ? 's' : ''}`);

    // ── STEP 6: Enter Edit Mode (locks booking for 30 min) ──────
    log('EDIT_MODE', 'INFO', 'Requesting edit mode...');
    const switchResult = await runInPage(tabId, fn_ncl_switchToEditMode);
    if (!switchResult?.ok && !switchResult?.notFound) {
      throw new Error('Could not click Switch to Edit Mode: ' + switchResult?.error);
    }
    // Wait for Store button to confirm edit mode activated
    await waitForEl(tabId, '#res-edit-save, a[href*="storeBooking"]', 12000);
    const editCheck = await runInPage(tabId, fn_ncl_checkEditMode);
    inEditMode = !!(editCheck?.isEditMode || editCheck?.hasStore);
    log('EDIT_MODE', 'OK', inEditMode ? 'Edit mode confirmed — booking is locked' : 'Edit mode uncertain but Store button present');

    // ── STEP 7: Navigate to Category tab ───────────────────────
    log('CATEGORY', 'INFO', 'Loading category grid...');
    const catClick = await runInPage(tabId, fn_ncl_clickCategoryTab);
    if (!catClick?.ok) throw new Error('Could not navigate to Category tab: ' + catClick?.error);
    await waitForEl(tabId, '#SWXMLForm_SelectCategory_category, .slick-viewport', 12000);
    await sleep(600); // let SlickGrid fully render

    // ── STEP 8: Read category data from VX._form_12 ─────────────
    const catData = await runInPage(tabId, fn_ncl_readCategoryData);
    if (!catData?.ok) throw new Error('Cannot read VX._form_12: ' + catData?.error);
    log('CATEGORY', 'OK', `${catData.categories.length} categories loaded, current: "${catData.currentCategory || currentCategory}"`);

    // ── STEP 9: Find cheapest available same-type category ──────
    const current = catData.categories.find(c => c.category === currentCategory);
    if (!current) throw new Error(`Current category "${currentCategory}" not found in grid data`);

    const cheaper = catData.categories
      .filter(c =>
        c.resTotal > 0 &&
        c.resTotal < current.resTotal &&
        c.status === 'OK' &&
        c.hasAvailability
      )
      .sort((a, b) => b.resTotal - a.resTotal); // highest-cheaper first = smallest drop

    if (cheaper.length === 0) {
      log('RESULT', 'NO_SAVING', `No cheaper available category vs "${currentCategory}" ($${current.resTotal})`);
      await cacheNoSaving('NCL', bookingId);
      return calculateNCL(bookingId, currentCategory, oldTotal, oldTotal, addons, currentPromos, currentPromos);
    }

    const targetCat = cheaper[0];
    log('SELECT', 'INFO', `Selecting: "${targetCat.category}" $${targetCat.resTotal} (was $${current.resTotal})`);

    // ── STEP 10: Select target category ─────────────────────────
    const selectResult = await runInPage(tabId, fn_ncl_selectCategory, targetCat.category);
    if (!selectResult?.ok) throw new Error('Category selection failed: ' + selectResult?.error);
    await sleep(800);

    // ── STEP 11: Read confirmed new total ───────────────────────
    const newTotalResult = await runInPage(tabId, fn_ncl_readNewResTotalFromGrid, targetCat.category);
    const newTotal  = newTotalResult?.resTotal  || targetCat.resTotal;
    const newPromos = newTotalResult?.currentPromo || targetCat.currentPromo || '';

    // ── STEP 12: Calculate ──────────────────────────────────────
    const result = calculateNCL(bookingId, currentCategory, oldTotal, newTotal, addons, currentPromos, newPromos);
    result.newPriceCategory = targetCat.category;
    log('RESULT', result.status, `net=$${result.netSaving} | ${result.note}`);

    if (result.status === 'NO_SAVING') await cacheNoSaving('NCL', bookingId);
    return result;

  } catch(e) {
    log('ERROR', 'ERROR', e.message);
    return makeErrorResult(bookingId, currentCategory, 'NCL', e.message);

  } finally {
    // ── ALWAYS UNLOCK — runs on success, error, AND exception ───
    if (inEditMode) {
      try {
        const cancelResult = await runInPage(tabId, fn_ncl_cancelEdit);
        _bgLog(bookingId, 'UNLOCK', cancelResult?.ok ? 'OK' : 'WARN',
          cancelResult?.ok
            ? `Booking unlocked (${cancelResult.method})`
            : 'Unlock failed: ' + cancelResult?.error
        );
        await sleep(500);
      } catch(unlockErr) {
        _bgLog(bookingId, 'UNLOCK', 'ERROR', 'CRITICAL — could not unlock booking: ' + unlockErr.message);
      }
    }
  }
}
