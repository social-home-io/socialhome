/* Pre-paint theme bootstrap — loaded synchronously from index.html before
 * any module bundle, so the <html> class matches the user's stored or
 * system preference on the very first paint. The matching effect in
 * src/store/theme.ts takes over once the SPA mounts. Kept as a file (not
 * an inline <script>) so the SPA's CSP can stay `script-src 'self'`. */
(function () {
  try {
    var t = localStorage.getItem('sh_theme') || 'auto';
    var dark = t === 'dark' ||
      (t === 'auto' &&
        window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.classList.add(
      dark ? 'sh-theme-dark' : 'sh-theme-light'
    );
  } catch (e) { /* sandboxed / no-storage — silently fall back */ }
})();
