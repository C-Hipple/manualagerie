(() => {
  const messagesEl = document.getElementById('messages');
  const form = document.getElementById('composer');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const titles = JSON.parse(document.getElementById('manual-titles').textContent || '{}');
  const noteTitles = JSON.parse(document.getElementById('note-titles')?.textContent || '{}');
  let convId = messagesEl.dataset.convId || null;
  let busy = false;

  const CITE_RE = /\[#(\d+)\s*,?\s*pp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?\]/g;
  const NOTE_CITE_RE = /\[note\s*#?\s*(\d+)\]/gi;
  const esc = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const clip = t => t.length > 28 ? t.slice(0, 26) + '…' : t;
  const shortTitle = id => clip(titles[id] || `Manual #${id}`);

  function renderMarkdown(text) {
    const withCites = (text || '').replace(CITE_RE, (_, id, a, b) => {
      const pages = b && b !== a ? `p.${a}–${b}` : `p.${a}`;
      return `<a class="cite" href="/manuals/${id}/file#page=${a}" target="_blank" rel="noopener" title="${esc(titles[id] || '')}">${esc(shortTitle(id))} ${pages}</a>`;
    }).replace(NOTE_CITE_RE, (_, id) => {
      const t = noteTitles[id] || `Note #${id}`;
      return `<a class="cite" href="/notes#note-${id}" target="_blank" rel="noopener" title="${esc(t)}">📝 ${esc(clip(t))}</a>`;
    });
    const html = window.marked ? marked.parse(withCites, { breaks: true }) : esc(withCites);
    return window.DOMPurify ? DOMPurify.sanitize(html, { ADD_ATTR: ['target'] }) : html;
  }

  // "Share" under a finished answer: the server snapshots it and swaps in the public link.
  function addShareButton(el, msgId) {
    if (!msgId) return;
    const d = document.createElement('div');
    d.className = 'msg-actions';
    d.innerHTML = `<button type="button" class="linkish" hx-post="/api/messages/${Number(msgId)}/share"
      hx-target="this" hx-swap="outerHTML" title="Make a public, read-only link to this answer">🔗 Share</button>`;
    el.appendChild(d);
    window.htmx?.process(d);
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-copy]');
    if (!b) return;
    navigator.clipboard?.writeText(b.dataset.copy).then(() => { b.textContent = 'Copied'; });
  });

  function renderStatic() {
    document.querySelectorAll('.msg[data-md]').forEach(el => {
      el.innerHTML = renderMarkdown(el.textContent);
      el.removeAttribute('data-md');
      addShareButton(el, el.dataset.id);
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
            else if (ev.text.startsWith('Checking your notes')) li.className = 'notes';
            else if (ev.text.startsWith('Creating a share link')) li.className = 'share';
            statuses.appendChild(li);
            if (!answer) body.innerHTML = '<span class="thinking">Working</span>';
          } else if (ev.type === 'error') {
            el.classList.add('error');
            body.textContent = ev.text;
          } else if (ev.type === 'done') {
            ev.citations?.forEach(c => {
              if (!c.title) return;
              if (c.note_id) noteTitles[c.note_id] = c.title; else titles[c.manual_id] = c.title;
            });
            body.innerHTML = renderMarkdown(answer);
            addShareButton(el, ev.message_id);
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
