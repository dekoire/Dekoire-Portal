/* Image Analyzer – Frontend Logic */

let currentFile   = null;
const prevValues  = {};
let currentData   = {};
let currentMjInfo = { mjId: null, uuid: null, variant: null };
let currentFileMeta = { name:'', date:'', dpiX:96, dpiY:96, sizeBytes:0 };

// ── File input ────────────────────────────────────────────────────────────────

const zone      = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');

zone.addEventListener('click', () => fileInput.click());
zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('dragover');
  const f = e.dataTransfer.files[0]; if (f) setFile(f);
});
fileInput.addEventListener('change', e => { if (e.target.files[0]) setFile(e.target.files[0]); });

// ── File selection ─────────────────────────────────────────────────────────────

async function setFile(file) {
  currentFile = file;
  document.getElementById('analyzeBtn').disabled = true;

  const exif = await readExif(file);

  const url = URL.createObjectURL(file);
  const prev = document.getElementById('preview');
  prev.src = url;
  prev.style.display = 'block';
  // Show preview section with heading
  const previewWrap = document.getElementById('sidebarPreview');
  if (previewWrap) previewWrap.style.display = 'block';
  const previewEmpty = document.getElementById('previewEmpty');
  if (previewEmpty) previewEmpty.style.display = 'none';

  const dims = await getImageDimensions(file);
  const dpiX = exif.dpiX || 96, dpiY = exif.dpiY || 96;
  const wIn  = (dims.w / dpiX).toFixed(2), hIn = (dims.h / dpiY).toFixed(2);

  const dateStr = exif.date
    ? formatExifDate(exif.date)
    : new Date(file.lastModified).toLocaleString('de-DE', {
        day:'2-digit', month:'long', year:'numeric', hour:'2-digit', minute:'2-digit'
      }) + ' (Dateidatum)';

  const physStr = `${(wIn*2.54).toFixed(1)} × ${(hIn*2.54).toFixed(1)} cm / ${wIn} × ${hIn} inch`;
  const dpiStr  = exif.dpiX ? `${Math.round(dpiX)} × ${Math.round(dpiY)} DPI` : '96 DPI (angenommen)';
  const sizeStr = formatSize(file.size) + (file.size > 5*1024*1024 ? ' (wird komprimiert)' : '');

  setById('bm-name', file.name);
  setById('bm-date', dateStr);
  setById('bm-px',   `${dims.w} × ${dims.h} px`);
  setById('bm-phys', physStr);
  setById('bm-dpi',  dpiStr);
  setById('bm-size', sizeStr);

  currentFileMeta = {
    name: file.name, date: dateStr,
    dpiX: dpiX, dpiY: dpiY, sizeBytes: file.size,
  };

  currentMjInfo = extractMjId(file.name);

  // Pre-fill Pinterest media URL from filename
  const slug = slugify(file.name.replace(/\.[^.]+$/, ''));
  setById('sm-pin-media-url', `https://dekoire.com/images/${slug}.jpg`);

  document.getElementById('analyzeBtn').disabled = false;
}

// ── MJ-ID extraction ──────────────────────────────────────────────────────────

function extractMjId(filename) {
  const m = filename.match(/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\s*(\d+)?/i);
  if (!m) return { mjId: null, uuid: null, variant: null };
  const uuid    = m[1];
  const variant = m[2] || null;
  const mjId    = variant ? `${uuid} ${variant}` : uuid;
  return { mjId, uuid, variant };
}

function slugify(str) {
  return str.trim()
    .replace(/ä/g,'ae').replace(/ö/g,'oe').replace(/ü/g,'ue')
    .replace(/Ä/g,'Ae').replace(/Ö/g,'Oe').replace(/Ü/g,'Ue')
    .replace(/ß/g,'ss')
    .replace(/\s+/g,'_')
    .replace(/[^a-zA-Z0-9_]/g,'');
}

function buildNewName(title, uuid, variant) {
  const slug    = slugify(title || '');
  const shortId = uuid ? uuid.replace(/-/g,'').substring(0, 8) : '';
  return [slug, shortId, variant].filter(Boolean).join('_');
}

function generateShortId() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  return Array.from({length: 7}, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

function regenNeueId() {
  setById('id-neu', generateShortId());
}

// ── EXIF parser ───────────────────────────────────────────────────────────────

async function readExif(file) {
  const result = { date: null, dpiX: null, dpiY: null };
  if (!file.type.includes('jpeg') && !file.name.toLowerCase().match(/\.jpe?g$/)) return result;
  try {
    const buf = await file.slice(0, 131072).arrayBuffer();
    const dv  = new DataView(buf);
    if (dv.getUint16(0) !== 0xFFD8) return result;
    let pos = 2;
    while (pos < buf.byteLength - 4) {
      const marker = dv.getUint16(pos); pos += 2;
      if (marker === 0xFFE1) {
        const segLen = dv.getUint16(pos);
        if (segLen >= 8 && dv.getUint32(pos+2) === 0x45786966 && dv.getUint16(pos+6) === 0x0000)
          parseTiff(dv, pos + 8, result);
        pos += segLen;
      } else if ((marker & 0xFF00) === 0xFF00 && marker !== 0xFFFF) {
        pos += dv.getUint16(pos);
      } else break;
    }
  } catch (_) {}
  return result;
}

function parseTiff(dv, ts, result) {
  const le  = dv.getUint16(ts) === 0x4949;
  const r16 = o => dv.getUint16(ts+o, le);
  const r32 = o => dv.getUint32(ts+o, le);
  const rat = o => { const n=r32(o), d=r32(o+4); return d?n/d:0; };
  const str = (o, len) => { let s=''; for(let i=0;i<len-1&&ts+o+i<dv.byteLength;i++) s+=String.fromCharCode(dv.getUint8(ts+o+i)); return s.trim(); };
  if (r16(2) !== 0x002A) return;
  function parseIFD(off) {
    if (off+2 > dv.byteLength-ts) return;
    const n = r16(off);
    for (let i=0; i<n; i++) {
      const e=off+2+i*12; if(e+12>dv.byteLength-ts) break;
      const tag=r16(e), cnt=r32(e+4), val=e+8;
      if (tag===0x9003)                     result.date = str(r32(val), cnt);
      else if (tag===0x0132 && !result.date) result.date = str(r32(val), cnt);
      else if (tag===0x011A)  result.dpiX = rat(r32(val));
      else if (tag===0x011B)  result.dpiY = rat(r32(val));
      else if (tag===0x8769)  parseIFD(r32(val));
    }
  }
  parseIFD(r32(4));
}

function formatExifDate(raw) {
  const m = raw.match(/^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2})/);
  if (!m) return raw;
  return new Date(+m[1],+m[2]-1,+m[3],+m[4],+m[5]).toLocaleString('de-DE',
    {day:'2-digit',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'});
}

function getImageDimensions(file) {
  return new Promise(resolve => {
    const img=new Image(), u=URL.createObjectURL(file);
    img.onload  = () => { URL.revokeObjectURL(u); resolve({w:img.naturalWidth,  h:img.naturalHeight}); };
    img.onerror = () => { URL.revokeObjectURL(u); resolve({w:0, h:0}); };
    img.src = u;
  });
}

function formatSize(bytes) {
  if (bytes < 1024)      return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/(1024*1024)).toFixed(1) + ' MB';
}

// ── Field helpers ─────────────────────────────────────────────────────────────

const ARRAY_FIELDS = ['dominante_farben', 'tags'];

function setById(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value;
}

function getFieldValue(name) { return document.getElementById('f-'+name)?.value ?? ''; }

function setFieldValue(name, value) {
  const el = document.getElementById('f-'+name);
  if (!el) return;
  if (name === 'ist_fotografie')        el.value = (value===true||value==='true') ? 'true' : 'false';
  else if (ARRAY_FIELDS.includes(name)) el.value = Array.isArray(value) ? value.join(', ') : value;
  else                                  el.value = value || '';
  if (name === 'beschreibung') updateCharCount();
}

function savePrev(name) { prevValues[name] = getFieldValue(name); }

function undoField(name) {
  if (prevValues[name] !== undefined) {
    setFieldValue(name, prevValues[name]);
    delete prevValues[name];
    document.getElementById('undo-'+name).disabled = true;
  }
}

function updateCharCount() {
  const len = document.getElementById('f-beschreibung').value.length;
  const cnt = document.getElementById('charCount');
  cnt.textContent = len + ' / 500';
  cnt.className   = 'char-count' + (len>480?' over':len>420?' warn':'');
}

// ── Analyze ───────────────────────────────────────────────────────────────────

async function analyzeImage() {
  if (!currentFile) return;
  showLoading('Analysiere Bild …');
  const fd = new FormData(); fd.append('image', currentFile);
  try {
    const res  = await fetch('/api/analyze', {method:'POST', body:fd});
    const data = await res.json();
    if (data.error) { hideLoading(); showToast(data.error, 'error'); return; }
    currentData = data;
    populateAll(data);
    document.getElementById('placeholder').style.display = 'none';
    document.getElementById('pane-produktinfo').style.display = 'flex';
    document.getElementById('actionSaveBtn').disabled = false;
    document.querySelectorAll('.sidebar-item').forEach(b => b.classList.remove('active'));
    document.getElementById('nav-produktinfo').classList.add('active');
    hideLoading();
    showToast('Analyse abgeschlossen', 'success');
    generateSocialMedia();
    prefillShopsFromProduct(data);
    // Optional hook for pages that want to act after analysis (e.g. auto legal check)
    if (typeof window._afterAnalyze === 'function') window._afterAnalyze(data, currentFile);
  } catch (err) { hideLoading(); showToast('Fehler: '+err.message, 'error'); }
}

function populateAll(d) {
  ['titel','beschreibung','dominante_farben','ist_fotografie','kunstart','epoche','tags']
    .forEach(n => { setFieldValue(n, d[n] ?? ''); document.getElementById('undo-'+n).disabled = true; delete prevValues[n]; });

  setById('m-ausrichtung', d.ausrichtung        || '—');
  setById('m-breite',      d.breite_px ? d.breite_px+' px' : '—');
  setById('m-hoehe',       d.hoehe_px  ? d.hoehe_px +' px' : '—');
  setById('m-ratio',       d.seitenverhaeltnis  || '—');

  const { mjId, uuid, variant } = currentMjInfo;
  setById('id-mj',       mjId || '—');
  setById('id-neu',      generateShortId());
  setById('id-name-neu', d.titel ? buildNewName(d.titel, uuid, variant) : '—');

  // Update Pinterest media URL with slugified title
  if (d.titel) {
    const slug = slugify(d.titel);
    setById('sm-pin-media-url', `https://dekoire.com/images/${slug}.jpg`);
  }
}

// ── Regenerate ────────────────────────────────────────────────────────────────

async function regenField(name) {
  if (!currentFile) return;
  prevValues[name] = getFieldValue(name);
  document.getElementById('undo-'+name).disabled = false;
  const btn = document.getElementById('regen-'+name);
  btn.classList.add('spinning'); btn.disabled = true;
  const fd = new FormData(); fd.append('image', currentFile); fd.append('field', name);
  try {
    const res  = await fetch('/api/regenerate', {method:'POST', body:fd});
    const data = await res.json();
    if (data.error) showToast(data.error, 'error');
    else {
      setFieldValue(name, data.value);
      currentData[name] = data.value;
      if (name === 'titel') {
        const { uuid, variant } = currentMjInfo;
        setById('id-name-neu', buildNewName(data.value, uuid, variant));
      }
    }
  } catch (err) { showToast('Fehler: '+err.message, 'error'); }
  finally { btn.classList.remove('spinning'); btn.disabled = false; }
}

// ── Social Media generation ───────────────────────────────────────────────────

async function generateSocialMedia() {
  const toArr = id => (document.getElementById(id)?.value || '').split(',').map(s=>s.trim()).filter(Boolean);
  const context = {
    titel:            document.getElementById('f-titel')?.value        || '',
    beschreibung:     document.getElementById('f-beschreibung')?.value || '',
    kunstart:         document.getElementById('f-kunstart')?.value     || '',
    epoche:           document.getElementById('f-epoche')?.value       || '',
    dominante_farben: toArr('f-dominante_farben'),
    tags:             toArr('f-tags'),
    ist_fotografie:   document.getElementById('f-ist_fotografie')?.value === 'true',
  };

  showLoading('Social Media wird generiert …');

  try {
    const res  = await fetch('/api/generate-social', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(context),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }

    const pin = data.pinterest || {};
    const ig  = data.instagram || {};

    setById('sm-pin-titel',        pin.titel        || '');
    setById('sm-pin-beschreibung', pin.beschreibung || '');
    setById('sm-pin-ziel-url',     pin.ziel_url     || (typeof PINTEREST_TARGET_URL !== 'undefined' ? PINTEREST_TARGET_URL : ''));
    setById('sm-pin-alt-text',     pin.alt_text     || '');
    // Set board select to the suggested board (closest match)
    if (pin.board) selectClosestOption('sm-pin-board', pin.board);

    setById('sm-ig-title',       ig.title       || '');
    setById('sm-ig-description', ig.description || '');
    setById('sm-ig-tags',
      Array.isArray(ig.tags) ? ig.tags.join(' ') : (ig.tags || ''));
    if (ig.location) selectClosestOption('sm-ig-location', ig.location);
    setById('sm-ig-alt-text',      ig.alt_text     || '');
    if (ig.content_type) setById('sm-ig-content-type', ig.content_type);
    setById('sm-pin-board-section', pin.board_section || '');

    showToast('Social Media Inhalte generiert', 'success');
  } catch (err) { showToast('Fehler: '+err.message, 'error'); }
  finally { hideLoading(); }
}

/* Pre-fill only basic shop fields from product data (no AI call) */
function prefillShopsFromProduct(d) {
  const desc = d.beschreibung || '';
  // Only prefill description — leave all other shop fields empty for per-card generation
  const setIfEmpty = (id, val) => {
    const el = document.getElementById(id);
    if (el && !el.value.trim() && val) el.value = val;
  };
  setIfEmpty('sm-etsy-description',    desc);
  setIfEmpty('sm-shopify-body-html',   desc);
  setIfEmpty('sm-amazon-description',  desc);
}

/* Generate texts for one shop section (only fills EMPTY fields) */
async function generateShopSection(shop) {
  const v = id => document.getElementById(id)?.value || '';
  const toArr = id => v(id).split(',').map(s=>s.trim()).filter(Boolean);
  const context = {
    titel:            v('f-titel'),
    beschreibung:     v('f-beschreibung'),
    kunstart:         v('f-kunstart'),
    epoche:           v('f-epoche'),
    dominante_farben: toArr('f-dominante_farben'),
    tags:             toArr('f-tags'),
    ist_fotografie:   v('f-ist_fotografie') === 'true',
  };

  const btn = document.getElementById('btn-gen-shop-' + shop);
  if (btn) { btn.disabled = true; btn.textContent = 'Generiert …'; }

  try {
    const res  = await fetch('/api/generate-shops', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(context),
    });
    const data = await res.json();
    if (data.error) { showToast('Shops: ' + data.error, 'error'); return; }

    const shopData = data[shop] || {};
    const setIfEmpty = (id, val) => {
      const el = document.getElementById(id);
      if (el && !el.value?.trim() && val) el.value = val;
    };

    if (shop === 'etsy') {
      setIfEmpty('sm-etsy-title',       shopData.title       || '');
      setIfEmpty('sm-etsy-description', shopData.description || '');
      setIfEmpty('sm-etsy-tags',        shopData.tags        || '');
      setIfEmpty('sm-etsy-materials',   shopData.materials   || '');
      if (shopData.who_made)  { const el = document.getElementById('sm-etsy-who-made');  if (el && !el.value) el.value = shopData.who_made; }
      if (shopData.when_made) { const el = document.getElementById('sm-etsy-when-made'); if (el && !el.value) el.value = shopData.when_made; }
    } else if (shop === 'shopify') {
      setIfEmpty('sm-shopify-title',      shopData.title      || '');
      setIfEmpty('sm-shopify-body-html',  shopData.body_html  || '');
      setIfEmpty('sm-shopify-vendor',     shopData.vendor     || '');
      setIfEmpty('sm-shopify-tags',       shopData.tags       || '');
      setIfEmpty('sm-shopify-sku',        shopData.sku        || '');
    } else if (shop === 'amazon') {
      setIfEmpty('sm-amazon-title',        shopData.title        || '');
      setIfEmpty('sm-amazon-description',  shopData.description  || '');
      setIfEmpty('sm-amazon-bullet-1',     shopData.bullet_1     || '');
      setIfEmpty('sm-amazon-bullet-2',     shopData.bullet_2     || '');
      setIfEmpty('sm-amazon-bullet-3',     shopData.bullet_3     || '');
      setIfEmpty('sm-amazon-bullet-4',     shopData.bullet_4     || '');
      setIfEmpty('sm-amazon-bullet-5',     shopData.bullet_5     || '');
      setIfEmpty('sm-amazon-search-terms', shopData.search_terms || '');
      setIfEmpty('sm-amazon-brand',        shopData.brand        || '');
    }
    showToast(shop.charAt(0).toUpperCase() + shop.slice(1) + ' Texte generiert', 'success');
  } catch (err) {
    showToast('Fehler (' + shop + '): ' + err.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" fill="none" viewBox="0 0 24 24"><path d="M23 4v6h-6M1 20v-6h6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg> Generieren'; }
  }
}

function selectClosestOption(selectId, value) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  const lower = value.toLowerCase();
  let best = null, bestScore = -1;
  for (const opt of sel.options) {
    const score = opt.value.toLowerCase() === lower ? 2
                : opt.value.toLowerCase().includes(lower) || lower.includes(opt.value.toLowerCase()) ? 1
                : 0;
    if (score > bestScore) { best = opt; bestScore = score; }
  }
  if (best) sel.value = best.value;
}

// ── Copy (shop-optimized via Claude) ─────────────────────────────────────────

async function copyField(name) {
  const value = getFieldValue(name);
  if (!value.trim()) { showToast('Feld ist leer', 'info'); return; }
  const btn = document.getElementById('copy-'+name);
  btn.classList.add('spinning'); btn.disabled = true;
  try {
    const res  = await fetch('/api/shop-copy', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({field: name, value}),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    await navigator.clipboard.writeText(data.text);
    btn.classList.remove('spinning');
    btn.classList.add('copied');
    setTimeout(() => btn.classList.remove('copied'), 2200);
    showToast('Shop-Text kopiert', 'success');
  } catch (err) { showToast('Fehler: '+err.message, 'error'); }
  finally { btn.classList.remove('spinning'); btn.disabled = false; }
}

async function copyDirect(fieldId) {
  const val = document.getElementById(fieldId)?.value || '';
  if (!val || val === '—') { showToast('Feld ist leer', 'info'); return; }
  const btn = document.querySelector(`[onclick="copyDirect('${fieldId}')"]`);
  try {
    await navigator.clipboard.writeText(val);
    if (btn) { btn.classList.add('copied'); setTimeout(() => btn.classList.remove('copied'), 2200); }
    showToast('Kopiert', 'success');
  } catch (err) { showToast('Fehler: '+err.message, 'error'); }
}

// ── Translate all ─────────────────────────────────────────────────────────────

async function translateAll(lang) {
  const toArr = id => document.getElementById(id).value.split(',').map(s=>s.trim()).filter(Boolean);
  const payload = {
    language: lang,
    titel:            document.getElementById('f-titel').value,
    beschreibung:     document.getElementById('f-beschreibung').value,
    dominante_farben: toArr('f-dominante_farben'),
    kunstart:         document.getElementById('f-kunstart').value,
    epoche:           document.getElementById('f-epoche').value,
    tags:             toArr('f-tags'),
  };
  document.querySelectorAll('.btn-lang').forEach(b => b.classList.add('loading'));
  showLoading(`Übersetze nach ${lang} …`);
  try {
    const res  = await fetch('/api/translate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    Object.entries(data).forEach(([k, v]) => setFieldValue(k, v));
    if (data.titel) {
      const { uuid, variant } = currentMjInfo;
      setById('id-name-neu', buildNewName(data.titel, uuid, variant));
    }
    showToast(`Übersetzt nach ${lang}`, 'success');
  } catch (err) { showToast('Fehler: '+err.message, 'error'); }
  finally {
    document.querySelectorAll('.btn-lang').forEach(b => b.classList.remove('loading'));
    hideLoading();
  }
}

// ── Result Banner ─────────────────────────────────────────────────────────────

let _bannerTimer;

function showResultBanner(steps, changes) {
  const banner = document.getElementById('resultBanner');
  if (!banner) return;

  const hasErr  = steps.some(s => s.type === 'err');
  const allGood = steps.every(s => s.type === 'ok' || s.type === 'info');
  const icon    = hasErr ? '⚠️' : '✅';
  let   title   = hasErr ? 'Teilweise gespeichert' : 'Gespeichert';
  if (changes && changes.length > 0)
    title += ` · ${changes.length} Felder geändert`;

  banner.querySelector('.result-banner-status').textContent = icon;
  banner.querySelector('.result-banner-title').textContent  = title;

  const icons = { ok: '✅', err: '❌', warn: '⚠️', info: '📡' };
  banner.querySelector('.result-banner-steps').innerHTML = steps.map(s =>
    `<div class="result-step ${s.type}">
       <span class="result-step-icon">${icons[s.type] || '•'}</span>
       <span class="result-step-text">${s.label}</span>
     </div>`
  ).join('');

  const changesEl = banner.querySelector('.result-banner-changes');
  if (changes && changes.length > 0) {
    changesEl.innerHTML =
      `<hr class="result-banner-sep" />
       <div class="result-changes-label">Änderungen (${changes.length})</div>` +
      changes.map(c => {
        const bef = (String(c.before || '—')).substring(0, 55);
        const aft = (String(c.after  || '—')).substring(0, 55);
        return `<div class="result-change-row">
          <div class="result-change-field">${c.field}</div>
          <div class="result-change-vals">
            <span class="result-change-before">${bef}</span>
            <span class="result-change-arrow">→</span>
            <span class="result-change-after">${aft}</span>
          </div>
        </div>`;
      }).join('');
    changesEl.style.display = 'block';
  } else {
    changesEl.innerHTML = '';
    changesEl.style.display = 'none';
  }

  banner.classList.add('visible');
  clearTimeout(_bannerTimer);
  _bannerTimer = setTimeout(closeResultBanner, 12000);
}

function closeResultBanner() {
  document.getElementById('resultBanner')?.classList.remove('visible');
  clearTimeout(_bannerTimer);
}

// ── Discord Notification ──────────────────────────────────────────────────────

async function sendDiscordNotification(type, payload, result, changes) {
  try {
    const res  = await fetch('/api/notify/discord', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ type, payload, result, changes: changes || [] }),
    });
    const data = await res.json();
    if (data.skipped) return { skipped: true };
    if (data.error)   return { ok: false, error: data.error };
    return { ok: true };
  } catch(e) {
    return { ok: false, error: e.message };
  }
}

// ── Save Product ─────────────────────────────────────────────────────────────

async function saveProduct() {
  const v   = id => document.getElementById(id)?.value || '';
  const arr = id => v(id).split(',').map(s=>s.trim()).filter(Boolean);

  const payload = {
    dekoire_id:        v('id-neu'),
    neuer_dateiname:   v('id-name-neu'),
    mj_id:             v('id-mj'),
    dateiname:         currentData.dateiname || (currentFile ? currentFile.name : ''),
    titel:             v('f-titel'),
    beschreibung:      v('f-beschreibung'),
    dominante_farben:  arr('f-dominante_farben'),
    ausrichtung:       currentData.ausrichtung       || '',
    breite_px:         currentData.breite_px         || '',
    hoehe_px:          currentData.hoehe_px          || '',
    seitenverhaeltnis: currentData.seitenverhaeltnis || '',
    ist_fotografie:    v('f-ist_fotografie') === 'true',
    kunstart:          v('f-kunstart'),
    epoche:            v('f-epoche'),
    tags:              arr('f-tags'),
    pin_titel:         v('sm-pin-titel'),
    pin_beschreibung:  v('sm-pin-beschreibung'),
    pin_ziel_url:      v('sm-pin-ziel-url'),
    pin_alt_text:      v('sm-pin-alt-text'),
    pin_board:         v('sm-pin-board'),
    pin_media_url:     v('sm-pin-media-url'),
    ig_title:          v('sm-ig-title'),
    ig_description:    v('sm-ig-description'),
    ig_tags:           v('sm-ig-tags'),
    ig_location:       v('sm-ig-location'),
    ig_alt_text:       v('sm-ig-alt-text'),
    ig_content_type:   v('sm-ig-content-type') || 'post',
    pin_board_section: v('sm-pin-board-section'),
    // Etsy
    etsy_title:            v('sm-etsy-title'),
    etsy_description:      v('sm-etsy-description'),
    etsy_tags:             v('sm-etsy-tags'),
    etsy_materials:        v('sm-etsy-materials'),
    etsy_who_made:         v('sm-etsy-who-made'),
    etsy_when_made:        v('sm-etsy-when-made'),
    etsy_occasion:         v('sm-etsy-occasion'),
    etsy_recipient:        v('sm-etsy-recipient'),
    etsy_shipping_profile: v('sm-etsy-shipping-profile'),
    etsy_price:            v('sm-etsy-price') ? parseFloat(v('sm-etsy-price')) : null,
    // Shopify
    shopify_title:         v('sm-shopify-title'),
    shopify_body_html:     v('sm-shopify-body-html'),
    shopify_vendor:        v('sm-shopify-vendor'),
    shopify_product_type:  v('sm-shopify-product-type'),
    shopify_tags:          v('sm-shopify-tags'),
    shopify_sku:           v('sm-shopify-sku'),
    shopify_price:         v('sm-shopify-price') ? parseFloat(v('sm-shopify-price')) : null,
    shopify_compare_price: v('sm-shopify-compare-price') ? parseFloat(v('sm-shopify-compare-price')) : null,
    shopify_collection:    v('sm-shopify-collection'),
    shopify_status:        v('sm-shopify-status') || 'draft',
    // Amazon
    amazon_title:          v('sm-amazon-title'),
    amazon_description:    v('sm-amazon-description'),
    amazon_bullet_1:       v('sm-amazon-bullet-1'),
    amazon_bullet_2:       v('sm-amazon-bullet-2'),
    amazon_bullet_3:       v('sm-amazon-bullet-3'),
    amazon_bullet_4:       v('sm-amazon-bullet-4'),
    amazon_bullet_5:       v('sm-amazon-bullet-5'),
    amazon_search_terms:   v('sm-amazon-search-terms'),
    amazon_brand:          v('sm-amazon-brand'),
    amazon_price:          v('sm-amazon-price') ? parseFloat(v('sm-amazon-price')) : null,
    amazon_sku:            v('sm-amazon-sku'),
    amazon_category:       v('sm-amazon-category'),
    amazon_condition:      v('sm-amazon-condition') || 'new',
    aufnahmedatum:     currentFileMeta.date || '',
    dpi_x:             currentFileMeta.dpiX || 96,
    dpi_y:             currentFileMeta.dpiY || 96,
    datei_groesse_kb:  Math.round((currentFileMeta.sizeBytes || 0) / 1024),
  };

  const btn = document.getElementById('actionSaveBtn');
  if (btn) btn.disabled = true;
  showLoading('Exportiere & speichere …');

  const steps = [];
  let exportResult = {};

  try {
    const fd = new FormData();
    if (currentFile) fd.append('image', currentFile);
    fd.append('data', JSON.stringify(payload));

    const res  = await fetch('/api/save-product', { method: 'POST', body: fd });
    const data = await res.json();
    exportResult = data;

    if (data.error) {
      steps.push({ type: 'err', label: 'Fehler beim Speichern: ' + data.error });
    } else {
      if (data.folder) {
        const folderName = data.folder.split('/').pop() || data.folder;
        steps.push({ type: 'ok', label: `Ordner angelegt: ${folderName}` });
      }
      if (data.image_url) {
        steps.push({ type: 'ok', label: 'Produkt gespeichert' });
      } else {
        steps.push({ type: 'warn', label: 'Supabase: Nicht gespeichert (nicht konfiguriert oder deaktiviert)' });
      }

      // Unlock "Produkt bearbeiten" button (use Supabase UUID as route key)
      const editId = data.supabase_id || data.dekoire_id;
      if (editId) {
        const editBtn = document.getElementById('actionEditBtn');
        if (editBtn) {
          editBtn.href = `/product/${editId}`;
          editBtn.style.display = '';
        }
      }

      // Discord notification
      const disc = await sendDiscordNotification('create', payload, data, []);
      if (disc.ok)      steps.push({ type: 'info', label: 'Discord: Benachrichtigung gesendet' });
      else if (!disc.skipped) steps.push({ type: 'warn', label: 'Discord: ' + disc.error });
    }
  } catch (err) {
    steps.push({ type: 'err', label: 'Netzwerkfehler: ' + err.message });
  } finally {
    if (btn) btn.disabled = false;
    hideLoading();
    showResultBanner(steps, null);
  }
}

// ── Auth / User menu ──────────────────────────────────────────────────────────

function toggleUserMenu() {
  document.getElementById('userDropdown').classList.toggle('open');
}
document.addEventListener('click', e => {
  if (!document.getElementById('userMenu')?.contains(e.target))
    document.getElementById('userDropdown')?.classList.remove('open');
});

async function doLogout() {
  await fetch('/auth/logout', {method:'POST'});
  window.location.href = '/login';
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showLoading(text) {
  document.getElementById('loadingText').textContent = text || 'Wird verarbeitet …';
  document.getElementById('loadingOverlay').classList.add('active');
  document.getElementById('analyzeBtn').disabled = true;
}
function hideLoading() {
  document.getElementById('loadingOverlay').classList.remove('active');
  document.getElementById('analyzeBtn').disabled = !currentFile;
}

let toastTimer;
function showToast(msg, type='info') {
  const icons = {
    success:'<svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    error:  '<svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    info:   '<svg width="16" height="16" fill="none" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/><path d="M12 8v4m0 4h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  };
  const t = document.getElementById('toast');
  t.innerHTML = (icons[type]||'') + '<span>'+msg+'</span>';
  t.className = 'show '+type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 4500);
}
