// adapter_espresso.js — CruiseIntel Optimization v6.3

async function espresso_waitForLogin(tabId, bookingId) {
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    try {
      const tabs = await chrome.tabs.get(tabId);
      const url = tabs.url || '';
      if (url.includes('cruisingpower.com') && !url.includes('login') && !url.includes('signin')) return { ok: true };
      if (url.includes('login') || url.includes('signin')) return { ok: false, error: 'Not logged in — please log into ESPRESSO first' };
    } catch (e) { }
    await new Promise(r => setTimeout(r, 600));
  }
  return { ok: false, error: 'Login check timed out' };
}

function fn_espresso_checkPaidStatus() {
  try {
    const totalEl = document.querySelector('[class*="totalPrice"] .amount, .total-price .amount, #totalPrice');
    const paidEl = document.querySelector('[class*="paymentsReceived"] .amount, .payments-received .amount, #paymentsReceived');
    if (totalEl && paidEl) {
      const total = parseFloat(totalEl.textContent.replace(/[^0-9.]/g, '')) || 0;
      const paid = parseFloat(paidEl.textContent.replace(/[^0-9.]/g, '')) || 0;
      if (total > 0 && paid >= total) return { isPaid: true, paymentsReceived: paid, totalPrice: total };
    }
    const finalDue = document.querySelector('#finalPaymentDue, .final-payment-due, [class*="finalPayment"]');
    if (finalDue) {
      const amt = parseFloat(finalDue.textContent.replace(/[^0-9.]/g, '')) || -1;
      if (amt === 0) return { isPaid: true, paymentsReceived: 0, totalPrice: 0 };
    }
    const bodyText = document.body?.innerText || '';
    if (/paid\s+in\s+full/i.test(bodyText)) return { isPaid: true, paymentsReceived: 0, totalPrice: 0 };
    const rows = document.querySelectorAll('.reservationSummary tr, table tr');
    let totalPrice = 0, paymentsReceived = 0;
    for (const row of rows) {
      const cells = row.querySelectorAll('td, th');
      if (cells.length >= 2) {
        const label = cells[0].textContent.trim().toLowerCase();
        const value = parseFloat(cells[cells.length - 1].textContent.replace(/[^0-9.]/g, '')) || 0;
        if (label.includes('total price')) totalPrice = value;
        if (label.includes('payments received')) paymentsReceived = value;
      }
    }
    if (totalPrice > 0 && paymentsReceived >= totalPrice) return { isPaid: true, paymentsReceived, totalPrice };
    return { isPaid: false, paymentsReceived, totalPrice };
  } catch (e) { return { isPaid: false, error: e.message }; }
}

function fn_espresso_search(bookingId) {
  const input = document.getElementById('reservationid');
  if (!input) return { ok: false, error: 'reservationid not found' };
  input.value = '';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.value = bookingId;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  document.getElementById('searchReservationBtn')?.click();
  return { ok: true };
}

function fn_espresso_readCategory() {
  const h = document.getElementById('currentPriceCat');
  if (h?.value?.trim()) return { found: true, priceCategory: h.value.trim(), method: 'hidden' };
  const s = document.querySelector('#groupInfoBlock > section.category.borderRight > div.priceCategory > span.value.ng-binding') || document.querySelector('[class*="priceCategory"] [class*="value"]') || document.querySelector('.priceCategory .value');
  if (s?.textContent?.trim()) return { found: true, priceCategory: s.textContent.trim(), method: 'CSS' };
  return { found: false, priceCategory: null, method: 'not found' };
}

function fn_espresso_clickCategories() {
  const a = Array.from(document.querySelectorAll('a')).find(el => el.textContent.trim() === 'Categories') || document.querySelector('#sideBar a[href*="catAvail"]') || document.querySelector('a[href*="categor"]');
  if (a) { a.click(); return { ok: true }; }
  return { ok: false, error: 'Categories link not found' };
}

function fn_espresso_checkWLT(cat) {
  const tbody = document.querySelector('#catAvailCategoryList tbody') || document.querySelector('[id*="catAvail"] tbody');
  if (!tbody) return { isWLT: false, status: 'table not found' };
  for (const row of tbody.querySelectorAll('tr')) {
    const icon = row.querySelector('td.c1 div.categoryIcon span, .categoryIcon span');
    if (icon && icon.textContent.trim() === cat) {
      const st = row.querySelector('td.c2.rooms .svCabin .status, .svCabin .status')?.textContent?.trim();
      return { isWLT: st === 'WLT', status: st };
    }
  }
  return { isWLT: false, status: 'unknown' };
}

async function fn_espresso_readPageData(cat) {
  const m = location.href.match(/execution=(e\d+s\d+)/);
  const token = m ? m[1] : null;
  let radio = '1';
  const sel0 = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]');
  const beforeJson = sel0?.value || '';
  const tbody = document.querySelector('#catAvailCategoryList tbody') || document.querySelector('[id*="catAvail"] tbody');
  if (tbody) {
    for (const row of tbody.querySelectorAll('tr')) {
      const icon = row.querySelector('td.c1 div.categoryIcon span, .categoryIcon span');
      if (icon && icon.textContent.trim() === cat) {
        const r = row.querySelector('input[name="rbCategorySelection"][data-columnindex="0"]') || row.querySelector('input[type="radio"]');
        if (r) {
          radio = r.value;
          r.checked = true;
          r.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
          r.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
          r.click();
          r.dispatchEvent(new Event('change', { bubbles: true }));
          r.dispatchEvent(new Event('input', { bubbles: true }));
          break;
        }
      }
    }
  }
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    await new Promise(res => setTimeout(res, 100));
    const cur = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]')?.value || '';
    if (cur && cur !== beforeJson && cur !== '[]') break;
  }
  await new Promise(res => setTimeout(res, 150));
  const selFinal = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]');
  return { executionToken: token, selectionJSON: selFinal?.value || '[]', radioValue: radio };
}

async function fn_espresso_executeAPICalls(token, json, radio) {
  try {
    const b1 = new URLSearchParams({ 'columnSelection': 'on', 'rbCategorySelection': radio || '1', '_eventId': 'saveCategories', 'categorySingleViewFormModel.selectionJSON': json || '[]' }).toString();
    const r1 = await fetch(`/espresso/protected/reservations.do?execution=${token}&_eventId=allocate&ajaxSource=true`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest' }, body: b1, credentials: 'include' });
    const allocText = await r1.text();
    if (!r1.ok) return { ok: false, error: `Allocate HTTP ${r1.status}: ${allocText.substring(0, 200)}` };
    await new Promise(res => setTimeout(res, 300));
    const r2 = await fetch(`/espresso/protected/repriceModalController.do/showRepriceModalCheck?execution=${token}`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/javascript, */*' }, body: `execution=${token}`, credentials: 'include' });
    if (!r2.ok) return { ok: false, error: `Reprice HTTP ${r2.status}` };
    const text = await r2.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { return { ok: false, error: 'Not JSON: ' + text.substring(0, 200) }; }
    return { ok: true, data, dataLength: text.length, allocText: allocText.substring(0, 200) };
  } catch (e) { return { ok: false, error: e.message }; }
}

async function fn_espresso_allocateOnly(token, json, radio) {
  try {
    const b1 = new URLSearchParams({ 'columnSelection': 'on', 'rbCategorySelection': radio || '1', '_eventId': 'saveCategories', 'categorySingleViewFormModel.selectionJSON': json || '[]' }).toString();
    const r1 = await fetch(`/espresso/protected/reservations.do?execution=${token}&_eventId=allocate&ajaxSource=true`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest' }, body: b1, credentials: 'include' });
    return { ok: r1.ok, status: r1.status };
  } catch (e) { return { ok: false, error: e.message }; }
}

function fn_espresso_clickContinue() {
  const btn = document.getElementById('submitToContinue') || Array.from(document.querySelectorAll('a,button')).find(el => el.textContent.trim() === 'Continue');
  if (btn) { btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window })); return { ok: true }; }
  return { ok: false, error: 'Continue button not found' };
}