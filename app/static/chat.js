(() => {
  const messagesEl = document.getElementById('messages');
  const form = document.getElementById('composer');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const titles = JSON.parse(document.getElementById('manual-titles').textContent || '{}');
  let convId = messagesEl.dataset.convId || null;
  let busy = false;

  const CITE_RE = /\[#(\d+)\s*,?\s*pp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?\]/g;
  const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function shortTitle(id) {
    const t = titles[id] || `Manual #${id}`;
    return t.length > 28 ? t.slice(0, 26) + '…' : t;
  }

  function renderMarkdown(text) {
    const withCites = (text || '').replace(CITE_RE, (_, id, a, b) => {
      const pages = b && b !== a ? `p.${a}–${b}` : `p.${a}`;
      return `<a class="cite" href="/manuals/${id}/file#page=${a}" target="_blank" rel="noopener" title="${esc(titles[id] || '')}">${esc(shortTitle(id))} ${pages}</a>`;
    });
    const html = window.marked ? marked.parse(withCites, { breaks: true }) : esc(withCites);
    return window.DOMPurify ? DOMPurify.sanitize(html, { ADD_ATTR: ['target'] }) : html;
  }

  // Citation hover/focus preview: the thumbnail is only requested the first time a chip is hovered or focused.
  let preview = null;
  function showPreview(a) {
    const m = /\/manuals\/(\d+)\/file#page=(\d+)/.exec(a.getAttribute('href') || '');
    if (!m) return;
    if (!preview) {
      preview = document.createElement('div');
      preview.className = 'cite-preview';
      preview.id = 'cite-preview';
      preview.setAttribute('role', 'tooltip');
      preview.hidden = true;
      preview.innerHTML = '<img alt="" decoding="async">';
      document.body.appendChild(preview);
    }
    const img = preview.firstChild;
    const src = `/manuals/${m[1]}/pages/${m[2]}/thumb.png`;
    if (img.getAttribute('src') !== src) img.setAttribute('src', src);
    img.alt = `Preview of page ${m[2]}`;
    a.setAttribute('aria-describedby', 'cite-preview');
    preview.hidden = false;
    const r = a.getBoundingClientRect();
    const w = preview.offsetWidth || 320;
    preview.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - w - 8))}px`;
    const h = preview.offsetHeight || 420;
    preview.style.top = `${r.top - h - 8 >= 8 ? r.top - h - 8 : r.bottom + 8}px`;
  }
  function hidePreview() {
    if (preview) preview.hidden = true;
    document.querySelectorAll('a.cite[aria-describedby]').forEach(a => a.removeAttribute('aria-describedby'));
  }
  document.addEventListener('mouseover', e => { const a = e.target.closest?.('a.cite'); if (a) showPreview(a); });
  document.addEventListener('mouseout', e => { if (e.target.closest?.('a.cite')) hidePreview(); });
  document.addEventListener('focusin', e => { const a = e.target.closest?.('a.cite'); if (a) showPreview(a); });
  document.addEventListener('focusout', e => { if (e.target.closest?.('a.cite')) hidePreview(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hidePreview(); });

  function renderStatic() {
    document.querySelectorAll('.msg[data-md]').forEach(el => {
      el.innerHTML = renderMarkdown(el.textContent);
      el.removeAttribute('data-md');
    });
  }

  const scrollDown = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };

  function addUser(text) {
    messagesEl.querySelector('.empty-state')?.remove();
    const d = document.createElement('div');
    d.className = 'msg user';
    d.textContent = text;
    messagesEl.appendChild(d);
  }

  function addAssistant() {
    const wrap = document.createElement('div');
    wrap.className = 'msg assistant';
    wrap.innerHTML = '<ul class="statuses"></ul><div class="body"><span class="thinking">Thinking</span></div>';
    messagesEl.appendChild(wrap);
    return wrap;
  }

  async function ensureConversation() {
    if (convId) return convId;
    const r = await fetch('/api/conversations', { method: 'POST' });
    if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
    convId = (await r.json()).id;
    history.replaceState(null, '', `/c/${convId}`);
    return convId;
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true; sendBtn.disabled = true;
    addUser(text);
    const el = addAssistant();
    const body = el.querySelector('.body');
    const statuses = el.querySelector('.statuses');
    scrollDown();
    let answer = '';
    try {
      const id = await ensureConversation();
      const r = await fetch(`/api/conversations/${id}/messages`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
      });
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok || !r.body) throw new Error(`Server returned ${r.status}`);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const line = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (!line.startsWith('data: ')) continue;
          const ev = JSON.parse(line.slice(6));
          if (ev.type === 'token') {
            answer += ev.text;
            body.innerHTML = renderMarkdown(answer);
          } else if (ev.type === 'reset') {
            answer = '';
            body.innerHTML = '<span class="thinking">Thinking</span>';
          } else if (ev.type === 'status') {
            const li = document.createElement('li');
            li.textContent = ev.text;
            if (ev.text.startsWith('Reading')) li.className = 'reading';
            statuses.appendChild(li);
            if (!answer) body.innerHTML = '<span class="thinking">Working</span>';
          } else if (ev.type === 'error') {
            el.classList.add('error');
            body.textContent = ev.text;
          } else if (ev.type === 'done') {
            ev.citations?.forEach(c => { if (c.title) titles[c.manual_id] = c.title; });
            body.innerHTML = renderMarkdown(answer);
            maybeAddToSidebar(text);
          }
          scrollDown();
        }
      }
    } catch (e) {
      if (e.message !== 'auth') { el.classList.add('error'); body.textContent = `Connection problem: ${e.message}`; }
    } finally {
      busy = false; sendBtn.disabled = false; input.focus();
    }
  }

  function maybeAddToSidebar(text) {
    const list = document.querySelector('.conv-list');
    if (!list || list.querySelector(`a[href="/c/${convId}"]`)) return;
    list.querySelector('li.muted')?.remove();
    list.querySelectorAll('li.active').forEach(li => li.classList.remove('active'));
    const li = document.createElement('li');
    li.className = 'active';
    li.innerHTML = `<a href="/c/${convId}">${esc(text.slice(0, 70))}</a>`;
    list.prepend(li);
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    const text = input.value;
    input.value = ''; autosize();
    send(text);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  const autosize = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 200) + 'px'; };
  input.addEventListener('input', autosize);
  document.querySelectorAll('[data-suggest]').forEach(b => b.addEventListener('click', () => send(b.textContent)));

  renderStatic();
  scrollDown();
})();
