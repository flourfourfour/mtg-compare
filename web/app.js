'use strict';

const $ = (id) => document.getElementById(id);
const money = (n) => '£' + Number(n || 0).toFixed(2);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const SAMPLE = `4 Lightning Bolt
4 Counterspell
2 Snapcaster Mage
1 Sol Ring
3 Brainstorm
2 Swords to Plowshares
1 Rhystic Study
4 Arcane Signet`;

let cards = [];
let jobId = null;
let poller = null;
let storeList = [];
let manualStores = [];

async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({ error: 'the server sent something unreadable' }));
  if (!response.ok) throw new Error(payload.error || ('HTTP ' + response.status));
  return payload;
}

function currentOptions() {
  const maxShops = $('opt-max-shops').value;
  return {
    max_condition_rank: Number($('opt-condition').value),
    foil: $('opt-foil').value,
    max_shops: maxShops ? Number(maxShops) : null,
  };
}

/* ---------- step 1: parse and validate ---------- */

$('load-sample').onclick = () => { $('decklist').value = SAMPLE; };

$('check').onclick = async () => {
  const text = $('decklist').value.trim();
  if (!text) return;
  $('check').disabled = true;
  $('parse-status').textContent = 'checking names against Scryfall…';
  try {
    const result = await api('/api/parse', { text });
    cards = result.cards;
    renderParse(result);
    $('panel-options').hidden = cards.length === 0;
    $('parse-status').textContent = '';
  } catch (err) {
    $('parse-status').textContent = 'Could not check names: ' + err.message;
  } finally {
    $('check').disabled = false;
  }
};

function renderParse(result) {
  const total = result.cards.reduce((sum, c) => sum + c.qty, 0);
  const parts = [`<p class="note"><strong>${result.cards.length}</strong> distinct cards,
    <strong>${total}</strong> in total.</p>`];
  if (result.unknown.length) {
    parts.push(`<p class="note warn">Not recognised as Magic cards, so they were left out:
      ${result.unknown.map(esc).join(', ')}</p>`);
  }
  parts.push('<div class="pills">' +
    result.cards.map((c) => `<span class="pill">${c.qty}× ${esc(c.name)}</span>`).join('') +
    '</div>');
  $('parse-output').innerHTML = parts.join('');
}

/* ---------- step 2: sweep the shops ---------- */

$('go').onclick = async () => {
  if (!cards.length) return;
  $('go').disabled = true;
  $('progress').hidden = false;
  $('search-status').textContent = 'starting…';
  try {
    const started = await api('/api/search', {
      cards, options: currentOptions(), fresh: $('opt-fresh').checked,
    });
    jobId = started.id;
    clearInterval(poller);
    poller = setInterval(poll, 900);
    poll();
  } catch (err) {
    $('search-status').textContent = 'Could not start: ' + err.message;
    $('go').disabled = false;
  }
};

async function poll() {
  if (!jobId) return;
  let snapshot;
  try {
    snapshot = await api('/api/search/' + jobId);
  } catch (err) {
    $('search-status').textContent = err.message;
    clearInterval(poller);
    $('go').disabled = false;
    return;
  }

  const pct = snapshot.total ? Math.round((snapshot.done / snapshot.total) * 100) : 0;
  $('progress-bar').style.width = pct + '%';
  const found = snapshot.results.filter((r) => r.offers.length).length;
  const retrying = (snapshot.retrying || []).length
    ? (snapshot.retry_wait
        ? ` · ${snapshot.retrying.join(', ')} asked us to slow down — waiting ${snapshot.retry_wait}s to retry`
        : ` · retrying ${snapshot.retrying.join(', ')}`)
    : '';
  $('search-status').textContent = snapshot.running
    ? `${pct}% · ${found} of ${snapshot.results.length} cards found · ${snapshot.elapsed}s${retrying}`
    : `done in ${snapshot.elapsed}s · ${found} of ${snapshot.results.length} cards found`;

  renderGrid(snapshot.results);
  $('panel-all').hidden = false;

  if (!snapshot.running) {
    clearInterval(poller);
    $('go').disabled = false;
    $('progress').hidden = true;
    renderPlan(snapshot);
  }
  renderStoreErrors(snapshot.store_errors);
}

function renderStoreErrors(errors) {
  const messages = Object.values(errors || {});
  const box = $('store-warning');
  box.innerHTML = messages.length
    ? 'Shops that did not answer this run (so they show as “no stock”, which may not be true): ' +
      messages.map(esc).join('; ')
    : '';
  box.hidden = !box.innerHTML;
}

/* ---------- step 3: the buy plan ---------- */

function renderPlan(snapshot) {
  const plan = snapshot.plan;
  if (!plan || !plan.shops.length) {
    $('panel-plan').hidden = false;
    $('plan-summary').innerHTML = '<p class="note warn">Nothing on your list was found in stock.</p>';
    $('plan-export').hidden = true;
    $('plan-shops').innerHTML = '';
    $('plan-missing').innerHTML = '';
    return;
  }

  $('panel-plan').hidden = false;
  $('plan-export').hidden = false;
  $('export-csv').href = '/api/export/' + jobId + '.csv';
  $('export-txt').href = '/api/export/' + jobId + '.txt';
  $('plan-summary').innerHTML = `
    <div class="summary">
      <span><span class="big">${money(plan.total)}</span> <span class="sub">for the cards</span></span>
      <span class="sub">${plan.shops.length} order${plan.shops.length === 1 ? '' : 's'}</span>
      <span class="sub">delivery charged separately by each shop</span>
    </div>`;

  $('plan-shops').innerHTML = plan.shops.map((shop) => {
    const link = snapshot.cart_links[shop.id];
    const rows = shop.lines.map((line) => `
      <tr>
        <td>${line.qty}×</td>
        <td><a href="${esc(line.product_url)}" target="_blank" rel="noreferrer">${esc(line.name)}</a>
          <span class="tag">${esc(line.condition)}</span>
          ${line.foil ? '<span class="tag foil">foil</span>' : ''}
          ${line.condition_known ? '' : '<span class="tag guess">condition not stated</span>'}
        </td>
        <td class="num">${money(line.unit_price)}</td>
        <td class="num">${money(line.line_total)}</td>
      </tr>`).join('');
    return `
      <div class="shop">
        <div class="shop-head">
          <h3>${esc(shop.name)}</h3>
          <span class="shop-cost"><strong>${money(shop.subtotal)}</strong></span>
          ${link ? `<a href="${esc(link)}" target="_blank" rel="noreferrer"><button class="primary">Open basket at ${esc(shop.name)}</button></a>` : ''}
        </div>
        <table><tbody>${rows}</tbody></table>
      </div>`;
  }).join('');

  const notes = [];
  if (plan.unavailable.length) {
    notes.push(`<p class="note warn">Not in stock at any shop we checked:
      ${plan.unavailable.map(esc).join(', ')}</p>`);
  }
  if ((plan.filtered_out || []).length) {
    // In stock, but not in the condition or finish you asked for. That's a
    // choice to make, not a card nobody has.
    const rows = plan.filtered_out.map((c) =>
      `<li><a href="${esc(c.url)}" target="_blank" rel="noreferrer">${esc(c.name)}</a> —
        ${money(c.cheapest)} at ${esc(c.shop)}, but ${esc(c.condition)}${c.foil ? ' foil' : ''}</li>`).join('');
    notes.push(`<p class="note warn">In stock, but not in the condition or finish you asked for.
      Loosen "condition" or "finish" above to include them:<ul>${rows}</ul></p>`);
  }
  if ((plan.excluded || []).length) {
    // These are buyable — they just didn't fit the shop limit. Saying so is the
    // difference between "can't have it" and "raise the limit by one".
    const rows = plan.excluded.map((c) =>
      `<li>${esc(c.name)} — from ${money(c.cheapest)} at ${c.shops.map(esc).join(' or ')}</li>`).join('');
    notes.push(`<p class="note warn">In stock, but left out because you capped the order at
      ${$('opt-max-shops').options[$('opt-max-shops').selectedIndex].text.toLowerCase()}.
      Raise "split across at most" to include them:<ul>${rows}</ul></p>`);
  }
  $('plan-missing').innerHTML = notes.join('');
  renderManual(plan);
}

/* ---------- shops we don't search automatically ---------- */

function renderManual(plan) {
  if (!manualStores.length) return;

  // Worth a look by hand: anything we couldn't get, plus anything that came
  // back dear enough that a second opinion might pay for itself.
  const gaps = [
    ...plan.unavailable.map((name) => ({ name, why: 'not in stock anywhere we searched' })),
    ...(plan.excluded || []).map((c) => ({ name: c.name, why: `over your shop limit, £${c.cheapest.toFixed(2)} elsewhere` })),
  ];
  const cards = gaps.length ? gaps : null;

  $('manual-why').innerHTML = manualStores
    .map((s) => `<strong>${esc(s.name)}</strong> (${esc(s.reason)})`).join(' · ') +
    ' — so they are not searched automatically. These links open their own search, one card at a time.';

  if (!cards) {
    $('manual-links').innerHTML = `<p class="note">Everything on your list was found, so there's
      nothing outstanding to chase. You can still search either shop by hand if you want a second price.</p>
      <div class="pills">${manualStores.map((s) =>
        `<a class="pill" href="https://${esc(s.host)}" target="_blank" rel="noreferrer">${esc(s.name)} →</a>`).join('')}</div>`;
  } else {
    $('manual-links').innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Card</th><th>Why</th>${manualStores.map((s) => `<th>${esc(s.name)}</th>`).join('')}</tr></thead>
      <tbody>${cards.map((c) => `
        <tr>
          <td>${esc(c.name)}</td>
          <td class="sub">${esc(c.why)}</td>
          ${manualStores.map((s) => `<td><a href="${esc(s.search.replace('{card}', encodeURIComponent(c.name)))}"
             target="_blank" rel="noreferrer">search →</a></td>`).join('')}
        </tr>`).join('')}</tbody></table></div>`;
  }
  $('panel-manual').hidden = false;
}

/* ---------- every price found ---------- */

function renderGrid(results) {
  const head = `<thead><tr>
      <th>Card</th><th class="num">Need</th><th class="num">Cheapest</th>
      <th>Shop</th><th class="num">Shops with stock</th></tr></thead>`;
  const body = results.map((row) => {
    if (!row.best) {
      return `<tr class="missing"><td>${esc(row.name)}</td><td class="num">${row.qty}</td>
        <td class="num">—</td><td>not found in stock</td><td class="num">0</td></tr>`;
    }
    return `<tr>
      <td><a href="${esc(row.best.product_url)}" target="_blank" rel="noreferrer">${esc(row.name)}</a>
        <span class="tag">${esc(row.best.condition)}</span>
        ${row.best.foil ? '<span class="tag foil">foil</span>' : ''}</td>
      <td class="num">${row.qty}</td>
      <td class="num best">${money(row.best.price)}</td>
      <td>${esc(row.best.store_name)}</td>
      <td class="num">${row.shops_with_stock}</td>
    </tr>`;
  }).join('');
  $('grid').innerHTML = head + '<tbody>' + body + '</tbody>';
}

/* ---------- catalogue index ---------- */

let indexPoller = null;

async function loadIndex() {
  const payload = await api('/api/index');
  const shops = payload.shops;
  const built = shops.filter((s) => s.built_at);
  $('index-table').innerHTML = `
    <thead><tr><th>Shop</th><th class="num">Products indexed</th><th>Last built</th></tr></thead>
    <tbody>${shops.map((s) => `
      <tr>
        <td>${esc(s.name)}</td>
        <td class="num">${s.products ? s.products.toLocaleString() : '—'}</td>
        <td>${s.built_at ? describeAge(s.age_hours) : '<span class="tag guess">never built</span>'}</td>
      </tr>`).join('')}</tbody>`;

  const build = payload.build;
  if (build && build.running) {
    const pct = build.shops_total ? Math.round((build.shops_done / build.shops_total) * 100) : 0;
    $('index-progress').hidden = false;
    $('index-bar').style.width = pct + '%';
    $('index-status').textContent = `${build.current || 'starting'} — ${build.shops_done} of ${build.shops_total} shops`;
    $('build-index').disabled = true;
    $('stop-index').hidden = false;
    if (!indexPoller) indexPoller = setInterval(() => loadIndex().catch(() => {}), 1500);
  } else {
    clearInterval(indexPoller);
    indexPoller = null;
    $('index-progress').hidden = true;
    $('build-index').disabled = false;
    $('stop-index').hidden = true;
    const errors = (build && build.errors) || [];
    $('index-status').textContent = built.length
      ? `${built.length} of ${shops.length} shops indexed` + (errors.length ? ` · ${errors.length} failed` : '')
      : 'No catalogues yet — searches will fall back to asking each shop per card, which is much slower.';
  }
}

function describeAge(hours) {
  if (hours == null) return '—';
  if (hours < 1) return 'just now';
  if (hours < 48) return Math.round(hours) + 'h ago';
  return Math.round(hours / 24) + ' days ago';
}

$('build-index').onclick = async () => {
  $('build-index').disabled = true;
  try { await api('/api/index/build', {}); } catch (err) { $('index-status').textContent = err.message; }
  loadIndex().catch(() => {});
};

$('stop-index').onclick = async () => {
  try { await api('/api/index/stop', {}); } catch (err) { /* nothing useful to say */ }
  loadIndex().catch(() => {});
};

/* ---------- shops ---------- */

async function loadStores() {
  const payload = await api('/api/stores');
  storeList = payload.stores;
  manualStores = payload.manual || [];
  $('stores-table').innerHTML = `
    <thead><tr><th>Include</th><th>Shop</th><th>Site</th></tr></thead>
    <tbody>${storeList.map((s) => `
      <tr>
        <td><input type="checkbox" data-store="${esc(s.id)}" data-field="enabled" ${s.enabled ? 'checked' : ''}></td>
        <td>${esc(s.name)}</td>
        <td><a href="https://${esc(s.host)}" target="_blank" rel="noreferrer">${esc(s.host)}</a></td>
      </tr>`).join('')}</tbody>`;
  renderStoreErrors({});
}

$('save-stores').onclick = async () => {
  const updates = {};
  document.querySelectorAll('#stores-table [data-store]').forEach((input) => {
    updates[input.dataset.store] = { enabled: input.checked };
  });
  $('stores-status').textContent = 'saving…';
  try {
    const payload = await api('/api/stores', { stores: updates });
    storeList = payload.stores;
    await loadStores();
    $('stores-status').textContent = 'saved';
    if (jobId) {
      const snapshot = await api('/api/replan', { id: jobId, options: currentOptions() });
      renderPlan(snapshot);
      $('stores-status').textContent = 'saved — buy plan updated';
    }
  } catch (err) {
    $('stores-status').textContent = 'Could not save: ' + err.message;
  }
};

// Changing what you'll accept re-costs the plan without re-searching the shops.
['opt-condition', 'opt-foil', 'opt-max-shops'].forEach((id) => {
  $(id).addEventListener('change', async () => {
    if (!jobId) return;
    try {
      renderPlan(await api('/api/replan', { id: jobId, options: currentOptions() }));
    } catch (err) { /* the search expired; the next search will sort it out */ }
  });
});

loadStores().catch(() => { $('stores-status').textContent = 'Could not load the shop list.'; });
loadIndex().catch(() => { $('index-status').textContent = 'Could not read the catalogue index.'; });
