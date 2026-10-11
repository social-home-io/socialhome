/* Household dashboard — shared page script (CSP script-src 'self').
 *
 * Back/forward cache: a page restored from it was rendered for an older
 * state (say, unlocked before someone tapped the lock). Reload so the
 * server decides again what this tablet may see.
 */
(function () {
  "use strict";

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });
})();
