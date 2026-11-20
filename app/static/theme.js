/* Theme switching.
 *
 * The server renders <html data-theme="<preference>"> from the "theme" cookie.
 * A preference of "auto" follows prefers-color-scheme: this script resolves it
 * to a concrete theme id before first paint (it is loaded synchronously in
 * <head>), records the preference in data-theme-pref, and writes explicit
 * choices back to the cookie. CSS does the rest — themes.css has one
 * `[data-theme="<id>"]` token block per theme.
 */
(() => {
  const COOKIE = 'theme'; // keep in sync with app/themes.py
  const DEFAULT_PREF = 'auto';
  const root = document.documentElement;
  const scheme = window.matchMedia('(prefers-color-scheme: dark)');

  const pref = () => root.dataset.themePref || root.dataset.theme || DEFAULT_PREF;
  const resolve = (p) => (p === 'auto' ? (scheme.matches ? 'dark' : 'light') : p);

  const paint = (p) => {
    root.dataset.themePref = p;
    root.dataset.theme = resolve(p);
    const chrome = getComputedStyle(root).getPropertyValue('--chrome').trim();
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta && chrome) meta.setAttribute('content', chrome);
    document.querySelectorAll('[data-theme-picker]').forEach((el) => { el.value = p; });
  };

  const remember = (p) => {
    const secure = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie =
      `${COOKIE}=${encodeURIComponent(p)}; path=/; max-age=31536000; SameSite=Lax${secure}`;
  };

  paint(pref());
  scheme.addEventListener('change', () => { if (pref() === 'auto') paint('auto'); });

  document.addEventListener('DOMContentLoaded', () => {
    paint(pref());
    document.querySelectorAll('[data-theme-picker]').forEach((el) => {
      el.addEventListener('change', () => {
        paint(el.value);
        remember(el.value);
      });
    });
  });
})();
