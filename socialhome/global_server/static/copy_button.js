/* "Copy code" buttons on the GFS public pages (landing pairing code,
 * /join invite code). Served as a file so the pages' Content-Security-Policy
 * can stay ``script-src 'self'`` (no inline script) — see
 * ``socialhome/global_server/html_page.py``.
 *
 * Wires every ``<button data-copy-target="ID">``: a click copies the text
 * of ``#ID`` to the clipboard and flips the label to ``data-copied-label``
 * for a moment. Falls back to selecting the code where the clipboard API is
 * unavailable (older Safari, insecure origins).
 */
(function () {
  "use strict";

  function selectText(target) {
    var range = document.createRange();
    range.selectNode(target);
    var sel = window.getSelection();
    if (!sel) return;
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function wire(btn) {
    var target = document.getElementById(btn.getAttribute("data-copy-target") || "");
    if (!target) return;
    btn.addEventListener("click", function () {
      var text = target.textContent || "";
      var done = function () {
        btn.textContent = btn.getAttribute("data-copied-label") || "Copied";
        btn.classList.add("copied");
        setTimeout(function () {
          btn.textContent = btn.getAttribute("data-default-label") || "Copy code";
          btn.classList.remove("copied");
        }, 1800);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          selectText(target);
        });
      } else {
        selectText(target);
      }
    });
  }

  function init() {
    var buttons = document.querySelectorAll("button[data-copy-target]");
    for (var i = 0; i < buttons.length; i++) wire(buttons[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
