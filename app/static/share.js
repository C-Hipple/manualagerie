/* Public share page: render the escaped markdown snapshot in the browser, sanitized.
 * Without marked/DOMPurify (or JS) the page simply shows the markdown as plain text. */
document.addEventListener('DOMContentLoaded', () => {
  const el = document.querySelector('.share-body[data-md]');
  if (!el || !window.marked || !window.DOMPurify) return;
  const html = marked.parse(el.textContent, { breaks: true });
  el.innerHTML = DOMPurify.sanitize(html, { FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select'] });
  el.removeAttribute('data-md');
});
