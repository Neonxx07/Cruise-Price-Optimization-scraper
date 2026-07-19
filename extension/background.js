// background.js — CruiseIntel Optimization v6.3
// Traffic Cop: routes bookings, manages queue, owns the dedicated window.
// Fixes: clearState preserves cache, getResumable/getAutoSaveCSV handlers added,
//        handleOptimize creates safe new window, full log coverage, ghost-state reset.

importScripts('calculator.js', 'adapter_espresso.js', 'adapter_ncl.js');

// ── State ──────────────────────────────────────────────────────
let state = {
  running: false,
  bookings: [],
  index: 0,
  results: [],
  log: [],
  progress: { done: 0, total: 0, currentId: null },
  cruiseLine: 'ESPRESSO'
};
let dedicatedWinId = null;
let dedicatedTabId = null;
const CACHE_TTL_MS = 12 * 60 * 60 * 1000;

const BG_NCL_SEARCH_URL = 'https://seawebagents.ncl.com/tva/search/';
const BG_ESPRESSO_URL = 'https://secure.cruisingpower.com/espresso/protected/reservations.do';

// ── Keep-alive (MV3 workers die after 5 min without this) ─────
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'keepAlive') chrome.runtime.getPlatformInfo(() => { });
});

// ── Logging ────────────────────────────────────────────────────
function _bgLog(bookingId, step, status, detail) {
  const entry = {
    time: new Date().toLocaleTimeString('en-GB', { hour12: false }),
    bookingId: bookingId || '—',
    step, status,
    detail: typeof detail === 'object' ? JSON.stringify(detail) : String(detail || '')
  };
  state.log.push(entry);
  if (state.log.length > 600) state.log.shift();
  broadcastState();
}

function broadcastState() {
  chrome.runtime.sendMessage({ action: 'stateUpdate', ...getPublicState() }).catch(() => { });
}

function getPublicState() {
  return {
    running: state.running,
    results: state.results,
    log: state.log,
    progress: state.progress,
    cruiseLine: state.cruiseLine
  };
}

// ── Utilities ──────────────────────────────────────────────────
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function retry(fn, attempts = 3, delayMs = 3000, label = '') {
  for (let i = 0; i < attempts; i++) {
    try { return await fn(i); }
    catch (e) {
      if (i === attempts - 1) throw e;
      _bgLog('RETRY', label, 'WARN', `Attempt ${i + 1}/${attempts} failed: ${e.message}`);
      await sleep(delayMs);
    }
  }
}

function runInPage(tabId, fn, ...args) {
  return chrome.scripting.executeScript({
    target: { tabId },
    func: fn,
    args: args,
    world: 'MAIN'
  }).then(r => r?.[0]?.result);
}

async function navigateTo(tabId, url) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(); // resolve anyway — downstream waitForEl will catch if page didn't load
    }, 30000);
    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        clearTimeout(timeout);
        setTimeout(resolve, 300); // small buffer for JS to initialize
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.update(tabId, { url });
  });
}

async function waitForEl(tabId, selector, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await runInPage(tabId, sel => !!document.querySelector(sel), selector).catch(() => false))
      return true;
    await sleep(400);
  }
  throw new Error(`Timeout (${timeout}ms) for: ${selector}`);
}

// ── Dedicated window (minimized, for batch automation) ────────
async function ensureDedicatedWindow() {
  if (dedicatedWinId !== null) {
    try { await chrome.windows.get(dedicatedWinId); return; }
    catch (e) { dedicatedWinId = null; dedicatedTabId = null; }
  }
  const win = await chrome.windows.create({ url: 'about:blank', state: 'minimized', type: 'normal' });
  dedicatedWinId = win.id;
  dedicatedTabId = win.tabs?.[0]?.id || null;
  _bgLog('SYSTEM', 'WINDOW', 'OK', `Minimized window id=${dedicatedWinId}`);
}

async function getDedicatedTab() {
  if (dedicatedTabId !== null) {
    try { await chrome.tabs.get(dedicatedTabId); return dedicatedTabId; }
    catch (e) {
      dedicatedWinId = null; dedicatedTabId = null;
      _bgLog('SYSTEM', 'HEAL', 'WARN', 'Hidden tab was closed — spinning up a new one...');
    }
  }
  await ensureDedicatedWindow();
  return dedicatedTabId;
}

async function closeDedicatedWindow() {
  if (dedicatedWinId !== null) {
    try { await chrome.windows.remove(dedicatedWinId); } catch (e) { }
    dedicatedWinId = null; dedicatedTabId = null;
  }
}

// ── Smart Cache ────────────────────────────────────────────────
async function getCachedResult(cruiseLine, bookingId) {
  const key = `cache_${cruiseLine}_${bookingId}`;
  const data = await chrome.storage.local.get(key);
  if (!data[key]) return null;
  if (Date.now() - data[key].ts > CACHE_TTL_MS) {
    await chrome.storage.local.remove(key); return null;
  }
  return data[key];
}

async function cacheNoSaving(cruiseLine, bookingId) {
  const key = `cache_${cruiseLine}_${bookingId}`;
  await chrome.storage.local.set({ [key]: { ts: Date.now() } });
}

// ── Resume / AutoSave ──────────────────────────────────────────
async function saveResume(bookings, index, cruiseLine) {
  await chrome.storage.local.set({
    resumeBookings: bookings,
    resumeIndex: index,
    resumeTimestamp: Date.now(),
    resumeCruiseLine: cruiseLine
  });
}

async function clearResume() {
  await chrome.storage.local.remove([
    'resumeBookings', 'resumeIndex', 'resumeTimestamp', 'resumeCruiseLine'
  ]);
}

async function autoSaveCSV() {
  if (!state.results.length) return;
  const header = 'Booking ID,Cruise Line,Status,Net Saving,Old Total,New Total,Category,New Category,Note,Lost Packages';
  const rows = state.results.map(r => {
    const d = r.data || {};
    const e = v => '"' + String(v || '').replace(/"/g, '""') + '"';
    return [
      e(d.bookingId), e(d.cruiseLine || state.cruiseLine), e(r.status),
      e(d.netSaving?.toFixed(2) || 0), e(d.oldTotal?.toFixed(2) || 0), e(d.newTotal?.toFixed(2) || 0),
      e(d.priceCategory || ''), e(d.newPriceCategory || ''),
      e(d.note || ''), e((d.lostPkgNames || []).join('|'))
    ].join(',');
  }).join('\n');
  await chrome.storage.local.set({
    autoSaveCSV: header + '\n' + rows,
    autoSaveTime: new Date().toISOString()
  });
}

// ── ESPRESSO: full booking flow with per-attempt re-navigation ─
// Spring Web Flow tokens are single-use. On failure we re-navigate for a fresh token.
async function handleESPRESSOBooking(bookingId) {
  const tabId = await getDedicatedTab();
  let priceCategory = null;

  const apiResult = await retry(async (attemptNum) => {
    if (attemptNum > 0) {
      _bgLog(bookingId, 'RETRY', 'WARN', `Attempt ${attemptNum + 1}/3 — re-navigating for fresh token`);
    }

    _bgLog(bookingId, 'NAVIGATE', 'INFO', 'Loading ESPRESSO search...');
    await navigateTo(tabId, BG_ESPRESSO_URL);
    const loginState = await espresso_waitForLogin(tabId, bookingId);
    if (!loginState.ok) throw new Error(loginState.error);
    if (attemptNum === 0) _bgLog(bookingId, 'NAVIGATE', 'OK', 'Search page ready');

    _bgLog(bookingId, 'SEARCH', 'INFO', 'Submitting booking ID...');
    const sr = await runInPage(tabId, fn_espresso_search, bookingId);
    if (!sr?.ok) throw new Error('Search failed: ' + (sr?.error || 'unknown'));
    if (attemptNum === 0) _bgLog(bookingId, 'SEARCH', 'OK', '');
    await waitForEl(tabId, '#sideBar, [id*="sideBar"]', 15000);

    const catInfo = await runInPage(tabId, fn_espresso_readCategory);
    if (catInfo?.priceCategory) priceCategory = catInfo.priceCategory;
    if (attemptNum === 0) _bgLog(bookingId, 'PRICE_CATEGORY', catInfo?.found ? 'OK' : 'WARN', `code="${priceCategory}"`);

    const cr = await runInPage(tabId, fn_espresso_clickCategories);
    if (!cr?.ok) throw new Error('Categories link not found');
    await waitForEl(tabId, '#catAvailCategoryList, [id*="catAvail"]', 12000);
    if (attemptNum === 0) _bgLog(bookingId, 'CATEGORIES', 'OK', 'Table loaded');

    // WLT check — must run AFTER categories table is loaded
    if (priceCategory) {
      const wlt = await runInPage(tabId, fn_espresso_checkWLT, priceCategory);
      if (wlt?.isWLT) return { _wlt: true };
    }

    // Read page data — polls until selectionJSON changes (max 2000ms)
    const pageData = await runInPage(tabId, fn_espresso_readPageData, priceCategory);
    if (!pageData?.executionToken) throw new Error('No execution token in URL');
    _bgLog(bookingId, 'PAGE_DATA', 'OK',
      `token="${pageData.executionToken}" radio="${pageData.radioValue}" json_len=${pageData.selectionJSON?.length}`);

    _bgLog(bookingId, 'API_CALLS', 'INFO', 'Running fetch inside page context...');
    const r = await runInPage(tabId, fn_espresso_executeAPICalls,
      pageData.executionToken, pageData.selectionJSON, pageData.radioValue);
    if (!r?.ok) throw new Error(r?.error || 'API failed');

    if ((r.dataLength || 0) < 300) {
      const paidStatus = await runInPage(tabId, fn_espresso_checkPaidStatus);
      if (paidStatus?.isPaid) return { _paidInFull: true, oldTotal: paidStatus.totalPrice };
      throw new Error(`API returned only ${r.dataLength} chars — token likely expired`);
    }

    _bgLog(bookingId, 'API_CALLS', 'OK', `Got ${r.dataLength} chars`);
    return r;
  }, 3, 3000, `ESPRESSO ${bookingId}`);

  // Sentinels
  if (apiResult?._wlt) {
    _bgLog(bookingId, 'SKIP', 'INFO', 'WLT — waitlisted');
    return makeWLTResult(bookingId, priceCategory, 'ESPRESSO');
  }
  if (apiResult?._paidInFull) {
    _bgLog(bookingId, 'SKIP', 'INFO', '💳 Booking is fully paid');
    return makePaidInFullResult(bookingId, priceCategory, 'ESPRESSO', apiResult.oldTotal);
  }

  const result = calculateESPRESSO(apiResult.data, bookingId, priceCategory);
  _bgLog(bookingId, 'RESULT', result.status, `net=$${result.netSaving} | ${result.note}`);
  if (result.status === 'NO_SAVING') await cacheNoSaving('ESPRESSO', bookingId);
  return result;
}

// ── Main batch loop ────────────────────────────────────────────
async function runBatch(bookings, isSingle, startIndex = 0, cruiseLine = 'ESPRESSO') {
  state.running = true;
  state.bookings = bookings;
  state.cruiseLine = cruiseLine;
  state.progress = { done: startIndex, total: bookings.length, currentId: null };
  if (startIndex === 0) state.results = [];

  await ensureDedicatedWindow();
  await saveResume(bookings, startIndex, cruiseLine);

  for (let i = startIndex; i < bookings.length; i++) {
    if (!state.running) break;
    const bookingId = bookings[i];
    state.progress.currentId = bookingId;
    state.progress.done = i;
    broadcastState();

    _bgLog(bookingId, 'QUEUE', 'INFO', `${i + 1} of ${bookings.length}`);

    // Smart cache check
    const cached = await getCachedResult(cruiseLine, bookingId);
    if (cached) {
      const hoursAgo = (Date.now() - cached.ts) / 3600000;
      _bgLog(bookingId, 'SKIP', 'INFO', `Cached NO_SAVING from ${hoursAgo.toFixed(1)}h ago`);
      state.results.push({ status: 'SKIPPED_TODAY', data: makeSkippedResult(bookingId, null, cruiseLine, hoursAgo) });
      state.progress.done = i + 1;
      await autoSaveCSV();
      await saveResume(bookings, i + 1, cruiseLine);
      await sleep(10);
      continue;
    }

    _bgLog(bookingId, 'START', 'INFO', `Booking ${bookingId}`);

    let result;
    try {
      const tabId = await getDedicatedTab();
      result = cruiseLine === 'NCL'
        ? await handleNCLBooking(bookingId, tabId)
        : await handleESPRESSOBooking(bookingId);
    } catch (e) {
      _bgLog(bookingId, 'ERROR', 'ERROR', e.message);
      result = makeErrorResult(bookingId, null, cruiseLine, e.message);
    }

    state.results.push({ status: result.status, data: result });
    state.progress.done = i + 1;
    await autoSaveCSV();
    await saveResume(bookings, i + 1, cruiseLine);
    broadcastState();
    await sleep(500);
  }

  await closeDedicatedWindow();
  await clearResume();
  state.running = false;
  state.progress.currentId = null;
  broadcastState();
}

// ── handleOptimize — creates its OWN window, never hijacks batch ──
// V6.x bug: used getDedicatedTab() which could crash a running batch.
// Fix: always spawns a fresh visible window, completely isolated.
async function handleOptimize(bookingId, cruiseLine, targetCategory) {
  _bgLog(bookingId, 'OPTIMIZE', 'INFO', `Opening ${cruiseLine} in new window...`);
  let newWinId = null;
  try {
    const startUrl = cruiseLine === 'NCL' ? BG_NCL_SEARCH_URL : BG_ESPRESSO_URL;
    const win = await chrome.windows.create({ url: startUrl, state: 'normal', type: 'normal', width: 1200, height: 800 });
    newWinId = win.id;
    const tabId = win.tabs?.[0]?.id;
    if (!tabId) throw new Error('New window tab not available');

    if (cruiseLine === 'NCL') {
      await waitForEl(tabId, '#SWXMLForm_SearchReservation_ResID', 15000);
      await runInPage(tabId, fn_ncl_search, bookingId);
      await waitForEl(tabId, '.item.current, #res-switch-edit, #res-edit-save', 20000)
        .catch(() => { throw new Error('Booking summary did not load'); });

      if (targetCategory) {
        await runInPage(tabId, fn_ncl_switchToEditMode);
        await waitForEl(tabId, '#res-edit-save', 12000);
        await runInPage(tabId, fn_ncl_clickCategoryTab);
        await waitForEl(tabId, '#SWXMLForm_SelectCategory_category, .slick-viewport', 12000);
        await sleep(1500);
        await runInPage(tabId, fn_ncl_selectCategory, targetCategory);
        _bgLog(bookingId, 'OPTIMIZE', 'OK', `NCL: category "${targetCategory}" selected — review and click STORE`);
      } else {
        _bgLog(bookingId, 'OPTIMIZE', 'OK', 'NCL booking open — click Switch to Edit Mode to review');
      }
    } else {
      // ESPRESSO
      await espresso_waitForLogin(tabId, bookingId);
      await runInPage(tabId, fn_espresso_search, bookingId);
      await waitForEl(tabId, '#sideBar, [id*="sideBar"]', 15000);
      const catInfo = await runInPage(tabId, fn_espresso_readCategory);
      await runInPage(tabId, fn_espresso_clickCategories);
      await waitForEl(tabId, '#catAvailCategoryList, [id*="catAvail"]', 12000);
      const pageData = await runInPage(tabId, fn_espresso_readPageData, catInfo?.priceCategory);
      if (!pageData?.executionToken) throw new Error('No execution token for optimize flow');
      await runInPage(tabId, fn_espresso_allocateOnly,
        pageData.executionToken, pageData.selectionJSON, pageData.radioValue);
      await sleep(500);
      await runInPage(tabId, fn_espresso_clickContinue);
      await sleep(1500);
      _bgLog(bookingId, 'OPTIMIZE', 'OK', 'ESPRESSO reprice popup open — review and click Continue With New Rate');
    }

    await chrome.windows.update(newWinId, { focused: true });
  } catch (e) {
    _bgLog(bookingId, 'OPTIMIZE', 'ERROR', e.message);
  }
}

// ── Message handler ────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === 'getState') {
    sendResponse(getPublicState()); return true;
  }

  if (msg.action === 'startBatch') {
    // If force=true (second click when stuck), reset ghost state
    if (msg.force) { state.running = false; }
    if (state.running) { sendResponse({ ok: false, error: 'Already running' }); return true; }
    runBatch(msg.bookings, msg.isSingle, 0, msg.cruiseLine || state.cruiseLine);
    sendResponse({ ok: true }); return true;
  }

  if (msg.action === 'stopBatch') {
    state.running = false; broadcastState();
    sendResponse({ ok: true }); return true;
  }

  // FIX: clearState no longer wipes chrome.storage.local.clear() (that killed the smart cache)
  // Only removes run-specific keys. Cache entries (cache_*) are preserved.
  if (msg.action === 'clearState') {
    state.running = false;   // FIX: reset ghost running state
    state.results = [];
    state.log = [];
    state.progress = { done: 0, total: 0, currentId: null };
    chrome.storage.local.remove([
      'autoSaveCSV', 'autoSaveTime',
      'resumeBookings', 'resumeIndex', 'resumeTimestamp', 'resumeCruiseLine'
    ]);
    broadcastState();
    sendResponse({ ok: true }); return true;
  }

  if (msg.action === 'setCruiseLine') {
    state.cruiseLine = msg.cruiseLine;
    sendResponse({ ok: true }); return true;
  }

  if (msg.action === 'optimizeBooking') {
    handleOptimize(msg.bookingId, msg.cruiseLine || state.cruiseLine, msg.targetCategory);
    sendResponse({ ok: true }); return true;
  }

  if (msg.action === 'viewInPortal') {
    const bookingId = msg.bookingId || '';
    let url;
    if (msg.cruiseLine === 'NCL') {
      url = BG_NCL_SEARCH_URL;
    } else {
      url = BG_ESPRESSO_URL + (bookingId ? `?reservationid=${bookingId}` : '');
    }
    chrome.windows.create({ url, state: 'normal', type: 'normal', width: 1200, height: 800 });
    sendResponse({ ok: true }); return true;
  }

  // FIX: these were missing — popup.js calls both of these
  if (msg.action === 'getResumable') {
    chrome.storage.local.get([
      'resumeBookings', 'resumeIndex', 'resumeTimestamp', 'resumeCruiseLine'
    ]).then(s => sendResponse(s));
    return true;
  }

  if (msg.action === 'getAutoSaveCSV') {
    chrome.storage.local.get(['autoSaveCSV', 'autoSaveTime'])
      .then(s => sendResponse(s));
    return true;
  }
});
