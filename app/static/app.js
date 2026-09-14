// Maple Court - Tenant Chatbot Assistant chat UI. Talks to /ask, /ingest, /ingest/status and /health.

const $ = (id) => document.getElementById(id);
const form = $('composer'), textarea = $('question'), askBtn = $('askBtn'), category = $('category');
const thread = $('thread'), welcome = $('welcome'), newChat = $('newChat');
const ingestBtn = $('ingestBtn'), ingestStatus = $('ingestStatus'), ingestDetail = $('ingestDetail');

let busy = false;

const esc = (s) => (s ?? '').toString()
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const URGENT = new Set(['immediate_danger', 'maintenance_emergency']);

const ICON = {
  spark: '<svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg>',
  warn: '<svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
  phone: '<svg viewBox="0 0 24 24"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z"/></svg>',
};

const fmtDate = (iso) => {
  if (!iso) return '';
  const d = new Date(iso + 'T00:00:00');
  return isNaN(d) ? iso : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
};
const clock = () => new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

// Phone numbers in quoted contact text become tap-to-call links.
const PHONE = /\((\d{3})\)\s?(\d{3})-(\d{4})/;
const linkify = (html) => html
  .replace(new RegExp(PHONE.source, 'g'), '<a href="tel:+1$1$2$3">$&</a>')
  .replace(/\b911\b/g, '<a href="tel:911">911</a>');

// Light formatting for model output: paragraphs, "- " bullets, **bold**, and the
// trailing "Source: ..." line the prompt asks for, which is shown quietly.
function formatAnswer(text) {
  return esc(text).trim().split(/\n{2,}/).map((block) => {
    const lines = block.split('\n');
    if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
      return '<ul>' + lines.map((l) => `<li>${l.replace(/^\s*[-*•]\s+/, '')}</li>`).join('') + '</ul>';
    }
    const cls = /^sources?:/i.test(block) ? ' class="cite-line"' : '';
    return `<p${cls}>${lines.join('<br>')}</p>`;
  }).join('').replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
}
const formatBot = (text) => linkify(formatAnswer(text));

// "Payment & Billing Policy — Maple Court (v3, effective 1 April 2025)" -> parts.
function splitCitation(citation) {
  const m = /^(.*?)(?:\s+—\s+Maple Court)?\s*\((v\d+),\s*effective ([^)]+)\)\s*(.*)$/.exec(citation || '');
  return m ? { title: m[1], version: m[2], effective: m[3], rest: m[4] } : { title: citation || 'Document' };
}

function renderSources(sources) {
  if (!sources?.length) return '';
  const chips = sources.map((s, i) => {
    const c = splitCitation(s.citation);
    const section = s.section || c.rest;
    const inForce = s.effective_from
      ? (s.effective_to ? `In force ${fmtDate(s.effective_from)} – ${fmtDate(s.effective_to)}` : `In force since ${fmtDate(s.effective_from)}`)
      : c.effective ? `Effective ${c.effective}` : '';
    const detail = `<strong>${esc(c.title)}${s.is_template ? '<span class="tag-template">TEMPLATE</span>' : ''}</strong>
      <span>${[c.version, section, inForce].filter(Boolean).map(esc).join(' · ')}</span>`;
    return `<button type="button" class="chip" aria-expanded="false" data-detail="${esc(detail)}">
      <span class="chip-n">${i + 1}</span><span class="chip-t">${esc(c.title)}</span>${c.version ? `<span class="chip-v">${esc(c.version)}</span>` : ''}
    </button>`;
  }).join('');
  return `<div class="chips">${chips}</div><div class="src-detail" hidden></div>`;
}

function renderAlert(e) {
  const urgent = URGENT.has(e.reason);
  // The API phrases these as a question; there is no escalate action behind it yet,
  // so the contact block itself is the call to action.
  const message = esc(e.message).replace(/\s*Do you want to escalate[^?]*\?\s*$/, '');
  const phone = urgent && e.contact && PHONE.exec(e.contact.text);
  return `<div class="alert ${urgent ? 'alert-urgent' : ''}" role="${urgent ? 'alert' : 'note'}">
    <div class="alert-head">${urgent ? ICON.warn : ICON.info}<span>${message}</span></div>
    ${phone ? `<a class="call" href="tel:+1${phone[1]}${phone[2]}${phone[3]}">${ICON.phone} Call ${esc(phone[0])}</a>` : ''}
    ${e.contact ? `<div class="alert-body">${linkify(esc(e.contact.text))}</div>
      <div class="alert-src">Quoted from ${esc(e.contact.source)}</div>` : ''}
    ${e.flagged_sources?.length ? `<div class="alert-src">Concerns: ${esc(e.flagged_sources.join('; '))}</div>` : ''}
  </div>`;
}

function renderAnswer(data) {
  const e = data.escalation;
  // Urgent routes answer with the contact block itself; don't show it twice.
  const duplicate = e?.contact && e.contact.text.trim() === (data.answer || '').trim();
  return (e ? renderAlert(e) : '')
    + (duplicate ? '' : formatBot(data.answer) + renderSources(data.sources));
}

// ---------- Messages ----------

function addUser(text) {
  const el = document.createElement('div');
  el.className = 'msg msg-user';
  el.innerHTML = `<div class="msg-col">
    <div class="bubble">${esc(text)}</div>
    <div class="msg-meta">${clock()}</div></div>`;
  thread.appendChild(el);
}

function addBot() {
  const el = document.createElement('div');
  el.className = 'msg msg-bot';
  el.innerHTML = `<div class="avatar">${ICON.spark}</div>
    <div class="msg-col">
      <div class="bubble"><span class="typing" aria-label="Assistant is typing"><i></i><i></i><i></i></span></div>
      <div class="msg-meta"></div>
    </div>`;
  thread.appendChild(el);
  return el;
}

const scrollDown = () => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

function setBusy(on) {
  busy = on;
  askBtn.disabled = on;
  document.querySelectorAll('[data-q]').forEach((b) => { b.disabled = on; });
}

async function ask(question) {
  const q = question.trim();
  if (!q || busy) return;
  setBusy(true);
  welcome.hidden = true;
  newChat.hidden = false;
  textarea.value = '';
  autosize();

  addUser(q);
  const bot = addBot();
  const bubble = bot.querySelector('.bubble'), meta = bot.querySelector('.msg-meta');
  scrollDown();

  // Empty means all documents; send null rather than "" so the API applies no filter.
  const cat = category.value || null;
  const catLabel = category.selectedOptions[0].textContent;

  try {
    const res = await fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, category: cat }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
    bubble.innerHTML = renderAnswer(data);
    meta.innerHTML = `<span>${clock()}</span>`
      + (data.elapsed_s != null ? `<span>${data.elapsed_s.toFixed(1)}s</span>` : '')
      + (cat ? `<span>${esc(catLabel)} only</span>` : '')
      + `<button type="button" class="tool" data-copy>${ICON.copy}<span>Copy</span></button>`;
    meta.querySelector('[data-copy]').dataset.text = data.answer || '';
  } catch (err) {
    bubble.classList.add('error');
    bubble.textContent = `Sorry, something went wrong: ${err.message || 'request failed'}`;
  } finally {
    setBusy(false);
    scrollDown();
    textarea.focus();
  }
}

// Source chips toggle a detail card; copy buttons copy the raw answer.
thread.addEventListener('click', async (e) => {
  const chip = e.target.closest('.chip');
  if (chip) {
    const bubble = chip.closest('.bubble'), panel = bubble.querySelector('.src-detail');
    const open = chip.getAttribute('aria-expanded') === 'true';
    bubble.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-expanded', 'false'));
    panel.hidden = open;
    if (!open) {
      chip.setAttribute('aria-expanded', 'true');
      panel.innerHTML = chip.dataset.detail;
    }
    return;
  }
  const copy = e.target.closest('[data-copy]');
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.text);
      copy.innerHTML = `${ICON.check}<span>Copied</span>`;
      setTimeout(() => { copy.innerHTML = `${ICON.copy}<span>Copy</span>`; }, 1500);
    } catch { /* clipboard unavailable */ }
  }
});

// ---------- Composer ----------

function autosize() {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 180) + 'px';
  form.classList.toggle('empty', !textarea.value.trim());
}

form.addEventListener('submit', (e) => { e.preventDefault(); ask(textarea.value); });
textarea.addEventListener('input', autosize);
textarea.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    ask(textarea.value);
  }
});

document.querySelectorAll('[data-q]').forEach((btn) =>
  btn.addEventListener('click', () => ask(btn.dataset.q)));

newChat.addEventListener('click', () => {
  thread.innerHTML = '';
  welcome.hidden = false;
  newChat.hidden = true;
  window.scrollTo({ top: 0 });
  textarea.focus();
});

// ---------- Document index ----------

function setStatus(state, detail) {
  const labels = { idle: 'Idle', running: 'Indexing…', succeeded: 'Indexed', failed: 'Failed' };
  ingestStatus.dataset.state = state;
  ingestStatus.querySelector('span').textContent = labels[state] || state;
  if (detail) ingestDetail.textContent = detail;
}

async function refreshStatus() {
  try {
    const d = await (await fetch('/ingest/status')).json();
    const st = d.status || 'idle';
    const detail = st === 'failed' ? d.error
      : st === 'succeeded' && d.stats ? `${d.stats.documents} documents, ${d.stats.chunks} chunks indexed.`
      : null;
    setStatus(st, detail);
    return st;
  } catch {
    setStatus('failed', 'Could not reach the server.');
    return 'failed';
  }
}

ingestBtn.addEventListener('click', async () => {
  ingestBtn.disabled = true;
  setStatus('running', 'Reading documents and rebuilding the index…');
  try {
    await fetch('/ingest', { method: 'POST' }); // 409 just means a run is already going
    const poll = setInterval(async () => {
      if (await refreshStatus() !== 'running') {
        clearInterval(poll);
        ingestBtn.disabled = false;
      }
    }, 2000);
  } catch (err) {
    setStatus('failed', err.message);
    ingestBtn.disabled = false;
  }
});

async function loadPropertyDate() {
  try {
    const d = await (await fetch('/health')).json();
    if (d.property_date) $('asOf').textContent = `Online · documents in force ${fmtDate(d.property_date)}`;
  } catch { /* "Online" stays */ }
}

autosize();
refreshStatus();
loadPropertyDate();
