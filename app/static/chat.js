(() => {
  const messagesEl = document.getElementById('messages');
  const form = document.getElementById('composer');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  const titles = JSON.parse(document.getElementById('manual-titles').textContent || '{}');
  const noteTitles = JSON.parse(document.getElementById('note-titles')?.textContent || '{}');
  let convId = messagesEl.dataset.convId || null;
  let busy = false;

  // Attached images waiting to be sent: [{ file, url }]. Thumbnails use object URLs; the server validates.
  const maxImages = Number(form.dataset.images || 0);
  const tray = document.getElementById('attach-tray');
  const attachInput = document.getElementById('attach-input');
  let pending = [];
  const IMAGE_NAME_RE = /\.(png|jpe?g|webp|gif|heic|heif)$/i;
  const isImage = f => f && (f.type.startsWith('image/') || IMAGE_NAME_RE.test(f.name || ''));

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
      addShareButton(el, el.dataset.id);
    });
  }

  const scrollDown = () => { messagesEl.scrollTop = messagesEl.scrollHeight; };

  function thumb(url, alt) {
    const img = document.createElement('img');
    img.src = url; img.alt = alt; img.decoding = 'async';
    // Browsers other than Safari can't preview HEIC; show the label instead of a broken image.
    img.addEventListener('error', () => img.replaceWith(Object.assign(document.createElement('span'), {
      className: 'img-fallback', textContent: alt })), { once: true });
    return img;
  }

  function addUser(text, imageUrls = []) {
    messagesEl.querySelector('.empty-state')?.remove();
    const d = document.createElement('div');
    d.className = 'msg user';
    if (imageUrls.length) {
      const row = document.createElement('div');
      row.className = 'msg-images';
      imageUrls.forEach((u, i) => row.appendChild(thumb(u, `Attached image ${i + 1}`)));
      d.appendChild(row);
    }
    d.appendChild(document.createTextNode(text));
    messagesEl.appendChild(d);
  }

  function attachNotice(text) {
    if (!tray) return;
    tray.querySelector('.attach-note')?.remove();
    if (!text) return;
    const n = document.createElement('span');
    n.className = 'attach-note muted small';
    n.textContent = text;
    tray.appendChild(n);
    tray.hidden = false;
  }

  function renderTray() {
    if (!tray) return;
    tray.replaceChildren();
    pending.forEach((p, i) => {
      const t = document.createElement('div');
      t.className = 'attach-thumb';
      t.appendChild(thumb(p.url, p.file.name || `Image ${i + 1}`));
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'attach-remove'; x.textContent = '×';
      x.title = 'Remove'; x.setAttribute('aria-label', `Remove ${p.file.name || `image ${i + 1}`}`);
      x.addEventListener('click', () => { URL.revokeObjectURL(p.url); pending.splice(i, 1); renderTray(); input.focus(); });
      t.appendChild(x);
      tray.appendChild(t);
    });
    tray.hidden = pending.length === 0;
  }

  function addFiles(files) {
    if (!maxImages) return;
    const imgs = [...files].filter(isImage);
    let skipped = files.length - imgs.length;
    for (const f of imgs) {
      if (pending.length >= maxImages) { skipped++; continue; }
      pending.push({ file: f, url: URL.createObjectURL(f) });
    }
    renderTray();
    if (pending.length >= maxImages && skipped) attachNotice(`Up to ${maxImages} images per question.`);
    else if (skipped) attachNotice('Only images can be attached.');
  }

  if (maxImages && tray && attachInput) {
    document.getElementById('attach').addEventListener('click', () => attachInput.click());
    attachInput.addEventListener('change', () => { addFiles(attachInput.files); attachInput.value = ''; });
    input.addEventListener('paste', e => {
      const files = [...(e.clipboardData?.files || [])].filter(isImage);
      if (!files.length) return;
      e.preventDefault();  // keep the image's file name out of the textarea
      addFiles(files);
    });
    const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
    ['dragenter', 'dragover'].forEach(t => form.addEventListener(t, e => {
      if (!hasFiles(e)) return;
      e.preventDefault(); form.classList.add('over');
    }));
    form.addEventListener('dragleave', e => { if (!form.contains(e.relatedTarget)) form.classList.remove('over'); });
    form.addEventListener('drop', e => {
      if (!hasFiles(e)) return;
      e.preventDefault(); form.classList.remove('over');
      addFiles(e.dataTransfer.files);
    });
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

  async function send(text, images = []) {
    if (busy || (!text.trim() && !images.length)) return false;
    busy = true; sendBtn.disabled = true;
    addUser(text, images.map(p => p.url));
    const el = addAssistant();
    const body = el.querySelector('.body');
    const statuses = el.querySelector('.statuses');
    scrollDown();
    let answer = '';
    try {
      const id = await ensureConversation();
      let req = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) };
      if (images.length) {
        const fd = new FormData();
        fd.append('text', text);
        images.forEach(p => fd.append('images', p.file, p.file.name || 'image'));
        req = { method: 'POST', body: fd };
      }
      const r = await fetch(`/api/conversations/${id}/messages`, req);
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) {
        const detail = await r.json().then(j => j.detail).catch(() => null);
        el.classList.add('error');
        body.textContent = typeof detail === 'string' ? detail : `Server returned ${r.status}`;
        return;
      }
      if (!r.body) throw new Error(`Server returned ${r.status}`);
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
            maybeAddToSidebar(text.trim() || 'Photo question');
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
    if (busy) return;
    const text = input.value;
    const images = pending;
    if (!text.trim() && !images.length) return;
    pending = []; renderTray();  // the object URLs live on in the sent bubble
    input.value = ''; autosize();
    send(text, images);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  const autosize = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 200) + 'px'; };
  input.addEventListener('input', autosize);
  document.querySelectorAll('[data-suggest]').forEach(b => b.addEventListener('click', () => send(b.textContent)));

  renderStatic();
  scrollDown();

  // The page was loaded while an answer was still being written (e.g. after a reload mid-turn): the server
  // keeps going, so reload once it's done to show the saved answer.
  if (document.getElementById('pending-answer')) {
    busy = true; sendBtn.disabled = true;
    setTimeout(() => location.reload(), 3000);
  }
})();
