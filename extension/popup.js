// popup.js — CruiseIntel Optimization v6.3
let results = [];
let logData = [];
let cruiseLine = 'ESPRESSO';

function parseBookings(raw) {
  return raw.split(/[\n,]+/).map(s => s.trim().replace(/\D/g, '')).filter(s => s.length >= 5 && s.length <= 12);
}

function setStatus(msg, color) {
  const el = document.getElementById('statusMsg');
  el.textContent = msg; el.style.display = msg ? 'block' : 'none'; el.style.color = color || '#64748b';
}

function setProgress(done, total, running) {
  const bar = document.getElementById('progressBar');
  if (!total) { bar.className = 'progress'; return; }
  bar.className = 'progress show';
  document.getElementById('progFill').style.width = Math.round(done / total * 100) + '%';
  document.getElementById('progFill').className = 'prog-fill' + (cruiseLine === 'NCL' ? ' ncl-mode' : cruiseLine === 'GOCCL' ? ' goccl-mode' : '');
  document.getElementById('progText').textContent = running ? 'Checking...' : 'Done!';
  document.getElementById('progCount').textContent = done + ' / ' + total;
}

function addCard(bookingId, status, data) {
  const container = document.getElementById('results');
  const existing = document.getElementById('card_' + bookingId);
  if (existing) existing.remove();

  const card = document.createElement('div');
  card.id = 'card_' + bookingId; card.className = 'card ' + status;

  const badges = { OPTIMIZATION: '✅ Optimization', TRAP: '⚠️ Trap', NO_SAVING: '⏭ No saving', ERROR: '❌ Error', WLT: '⏭ WLT', CHECKING: 'Checking', PAID_IN_FULL: '💳 Paid in Full', SKIPPED_TODAY: '⏩ Cached' };
  const badgeCl = data?.cruiseLine || cruiseLine;
  const cl = badgeCl === 'NCL' ? '<span class="ncl-badge">NCL</span>'
    : badgeCl === 'GOCCL' ? '<span class="goccl-badge">GOCCL</span>' : '';

  if (status === 'CHECKING') {
    card.innerHTML = `<div class="card-top"><span class="card-id">${bookingId}${cl}</span><span class="card-badge">Checking</span></div><div class="card-saving"><span class="spinner"></span>Checking...</div>`;
    container.insertBefore(card, container.firstChild); return;
  }

  let savingHtml = '';
  if (status === 'OPTIMIZATION') savingHtml = `OPTIMIZATION $${(data?.netSaving || 0).toFixed(2)}`;
  else if (status === 'TRAP') savingHtml = `TRAP — net impact $${(data?.netSaving || 0).toFixed(2)}`;
  else if (status === 'NO_SAVING') savingHtml = (data?.netSaving || 0) < 0 ? `Price UP $${Math.abs(data.netSaving).toFixed(2)}` : 'No change';
  else if (status === 'WLT') savingHtml = 'Waitlisted — skipped';
  else if (status === 'PAID_IN_FULL') savingHtml = '💳 Fully paid — repricing blocked';
  else if (status === 'SKIPPED_TODAY') savingHtml = data?.note || 'Checked recently — cached';
  else savingHtml = (data?.error || 'Unknown error').substring(0, 80);

  let confHtml = '';
  if (data?.confidence && status === 'OPTIMIZATION') {
    const sc = data.confidence;
    const stars = '★'.repeat(sc) + '☆'.repeat(5 - sc);
    const colors = { 1: '#ef4444', 2: '#f59e0b', 3: '#3b82f6', 4: '#10b981', 5: '#059669' };
    const bgs = { 1: '#fee2e2', 2: '#fef3c7', 3: '#dbeafe', 4: '#d1fae5', 5: '#a7f3d0' };
    confHtml = `<div class="conf-row"><div class="conf-stars" style="color:${colors[sc]};background:${bgs[sc]};">${stars}</div></div>`;
  }

  let priceHtml = '';
  if (data?.oldTotal) {
    const catInfo = data.priceCategory ? ` · Cat: ${data.priceCategory}${data.newPriceCategory ? ' → ' + data.newPriceCategory : ''}` : '';
    priceHtml = `<div class="card-prices">$${data.oldTotal.toFixed(2)} → $${(data.newTotal || data.oldTotal).toFixed(2)}${catInfo}</div>`;
  }

  let pkgHtml = data?.lostPkgNames?.length ? `<div class="card-pkg-loss">⚠️ Lost package: <b>${data.lostPkgNames.join(', ')}</b> ($${(data.lostPkgValue || 0).toFixed(2)} deducted)</div>` : '';

  const noteHtml = data?.note ? `<div class="card-note"><div><div class="note-label">HubSpot note</div><div class="note-text">${data.note}</div></div><button class="copy-btn" data-note="${data.note.replace(/"/g, '&quot;')}">Copy</button></div>` : '';

  let actionHtml = '';
  if (!['CHECKING', 'ERROR', 'SKIPPED_TODAY', 'PAID_IN_FULL'].includes(status)) {
    const bCL = data?.cruiseLine || cruiseLine;
    const targetCat = data?.newPriceCategory || data?.priceCategory || '';
    let mainBtn = '';
    if (status === 'OPTIMIZATION') mainBtn = `<button class="optimize-btn" data-booking="${bookingId}" data-cl="${bCL}" data-cat="${targetCat}">⚡ Open Reprice Popup</button>`;
    actionHtml = `<div class="card-actions">${mainBtn}<button class="view-btn" data-booking="${bookingId}" data-cl="${bCL}">🌐 View in Portal</button></div>`;
  }

  card.innerHTML = `<div class="card-top"><span class="card-id">${bookingId}${cl}</span><span class="card-badge">${badges[status] || status}</span></div><div class="card-saving">${savingHtml}</div>${confHtml}${priceHtml}${pkgHtml}${noteHtml}${actionHtml}`;

  if (status === 'OPTIMIZATION') container.insertBefore(card, container.firstChild);
  else container.appendChild(card);
}

function updateSummary(res) {
  const opts = res.filter(r => r.status === 'OPTIMIZATION');
  document.getElementById('sOpt').textContent = opts.length;
  document.getElementById('sTrap').textContent = res.filter(r => r.status === 'TRAP').length;
  document.getElementById('sNos').textContent = res.filter(r => ['NO_SAVING', 'WLT', 'SKIPPED_TODAY'].includes(r.status)).length;
  document.getElementById('sPaid').textContent = res.filter(r => r.status === 'PAID_IN_FULL').length;
  document.getElementById('sErr').textContent = res.filter(r => r.status === 'ERROR').length;
  document.getElementById('sSaved').textContent = '$' + opts.reduce((s, r) => s + (r.data?.netSaving || 0), 0).toFixed(2);
  document.getElementById('summary').className = res.length ? 'show' : '';
}

function renderAllCards(res) {
  document.getElementById('results').innerHTML = '';
  res.forEach(r => addCard(r.data?.bookingId || '?', r.status, r.data || {}));
}

function renderLog() {
  const panel = document.getElementById('logPanel');
  if (!logData.length) { panel.innerHTML = '<div style="color:#475569">No log yet.</div>'; return; }
  panel.innerHTML = logData.map(e => `<div><span style="color:#475569">${e.time}</span> <span style="color:${e.status === 'OK' ? '#4ade80' : e.status === 'ERROR' ? '#f87171' : '#fbbf24'}">[${e.status}]</span> <span style="color:#7dd3fc">${e.bookingId}</span> <span style="color:#e2e8f0">${e.step}</span>: <span style="color:#cbd5e1">${e.detail}</span></div>`).join('');
  panel.scrollTop = panel.scrollHeight;
}

function applyState(s) {
  if (!s) return;
  results = s.results || []; logData = s.log || [];
  if (s.cruiseLine) updateCruiseLineUI(s.cruiseLine);

  const running = s.running;

  document.getElementById('clEspresso').disabled = running;
  document.getElementById('clNCL').disabled = running;
  document.getElementById('clGoccl').disabled = running;
  document.getElementById('clearBtn').disabled = running;

  renderAllCards(results); updateSummary(results);

  if (s.progress?.total > 0) {
    setProgress(s.progress.done, s.progress.total, running);
    if (running && s.progress.currentId) {
      setStatus(`⟳ Running — checking ${s.progress.currentId}`);
      addCard(s.progress.currentId, 'CHECKING', null);
      document.getElementById('runBtn').disabled = true;
      document.getElementById('stopBtn').className = 'btn btn-stop show';
    } else if (!running) {
      setStatus('');
      document.getElementById('runBtn').disabled = false;
      document.getElementById('stopBtn').className = 'btn btn-stop';
    }
  }
  const logEl = document.getElementById('logPanel');
  if (logEl && getComputedStyle(logEl).display !== 'none') renderLog();
}

function updateCruiseLineUI(cl) {
  cruiseLine = cl;
  const isNCL = cl === 'NCL', isGoccl = cl === 'GOCCL';
  document.getElementById('clEspresso').className = 'cl-btn espresso' + (!isNCL && !isGoccl ? ' active' : '');
  document.getElementById('clNCL').className = 'cl-btn ncl' + (isNCL ? ' active' : '');
  document.getElementById('clGoccl').className = 'cl-btn goccl' + (isGoccl ? ' active' : '');

  const subs = { NCL: 'Norwegian Cruise Line — SeaWeb', GOCCL: 'GoCCL — Carnival Cruise Line', ESPRESSO: 'ESPRESSO — Royal Caribbean & Celebrity' };
  const hints = { NCL: 'Must be logged into NCL SeaWeb before running', GOCCL: 'Must be logged into GoCCL Navigator before running', ESPRESSO: 'Must be logged into ESPRESSO before running' };
  document.getElementById('headerSub').textContent = subs[cl] || subs.ESPRESSO;
  document.getElementById('hintText').textContent = hints[cl] || hints.ESPRESSO;
  document.getElementById('runBtn').className = 'btn btn-run' + (isNCL ? ' ncl-mode' : isGoccl ? ' goccl-mode' : '');
}

document.addEventListener('DOMContentLoaded', async () => {
  chrome.storage.session.get(['bookingInput'], data => { if (data.bookingInput) document.getElementById('bookingInput').value = data.bookingInput; });
  chrome.runtime.sendMessage({ action: 'getState' }, applyState);

  document.getElementById('clEspresso').addEventListener('click', () => { updateCruiseLineUI('ESPRESSO'); chrome.runtime.sendMessage({ action: 'setCruiseLine', cruiseLine: 'ESPRESSO' }); });
  document.getElementById('clNCL').addEventListener('click', () => { updateCruiseLineUI('NCL'); chrome.runtime.sendMessage({ action: 'setCruiseLine', cruiseLine: 'NCL' }); });
  document.getElementById('clGoccl').addEventListener('click', () => { updateCruiseLineUI('GOCCL'); chrome.runtime.sendMessage({ action: 'setCruiseLine', cruiseLine: 'GOCCL' }); });
  document.getElementById('bookingInput').addEventListener('input', () => { chrome.storage.session.set({ bookingInput: document.getElementById('bookingInput').value }); });

  document.getElementById('runBtn').addEventListener('click', () => {
    const bookings = parseBookings(document.getElementById('bookingInput').value);
    if (!bookings.length) return setStatus('Please enter at least one booking number.');
    chrome.runtime.sendMessage({ action: 'startBatch', bookings, cruiseLine });
  });

  document.getElementById('stopBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'stopBatch' }); setStatus('Stopping after current booking...');
  });

  document.getElementById('clearBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'clearState' });
    document.getElementById('bookingInput').value = '';
    document.getElementById('results').innerHTML = '';
    document.getElementById('summary').className = '';
    document.getElementById('progressBar').className = 'progress';
    setStatus('');
    document.getElementById('logPanel').style.display = 'none';
    document.getElementById('logBtn').textContent = '📋 Log';
  });

  document.getElementById('logBtn').addEventListener('click', () => {
    const p = document.getElementById('logPanel');
    const open = p.style.display !== 'none';
    p.style.display = open ? 'none' : 'block';
    document.getElementById('logBtn').textContent = open ? '📋 Log' : '📋 Hide Log';
    if (!open) renderLog();
  });

  document.getElementById('exportBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'getAutoSaveCSV' }, data => {
      if (!data?.autoSaveCSV) return alert('No results to export.');
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([data.autoSaveCSV], { type: 'text/csv' }));
      a.download = `cruiseintel_${new Date().toISOString().substring(0, 10)}.csv`; a.click();
    });
  });

  document.getElementById('results').addEventListener('click', e => {
    const copyBtn = e.target.closest('.copy-btn');
    if (copyBtn) {
      navigator.clipboard.writeText(copyBtn.dataset.note || '').then(() => {
        copyBtn.textContent = '✓ Copied'; copyBtn.style.background = '#10b981';
        setTimeout(() => { copyBtn.textContent = 'Copy'; copyBtn.style.background = ''; }, 2000);
      }); return;
    }

    const optBtn = e.target.closest('.optimize-btn');
    if (optBtn) {
      optBtn.disabled = true; optBtn.textContent = '⏳ Automating...';
      setStatus(`Opening ${optBtn.dataset.cl} popup...`);
      chrome.runtime.sendMessage({ action: 'optimizeBooking', bookingId: optBtn.dataset.booking, cruiseLine: optBtn.dataset.cl, targetCategory: optBtn.dataset.cat }, () => {
        setTimeout(() => { optBtn.disabled = false; optBtn.textContent = '⚡ Open Reprice Popup'; setStatus(''); }, 12000);
      }); return;
    }

    const viewBtn = e.target.closest('.view-btn');
    if (viewBtn) chrome.runtime.sendMessage({ action: 'viewInPortal', bookingId: viewBtn.dataset.booking, cruiseLine: viewBtn.dataset.cl });
  });
});

chrome.runtime.onMessage.addListener(msg => { if (msg.action === 'stateUpdate') applyState(msg); });