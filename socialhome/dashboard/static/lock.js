/* Household dashboard — lock screen enhancements.
 *
 * Progressive enhancement only: the server-rendered page already shows
 * the household time and a plain PIN form that works without this file.
 * Here we (1) keep the clock ticking in the household's time zone,
 * (2) reload after a quiet minute so the screen stays current and
 * (3) replace the PIN field with a big on-screen keypad. Served from
 * /static/ (CSP script-src 'self'); no inline handlers anywhere.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-lock]");
  if (!root) {
    return;
  }

  // "?pin=wrong" / "?pin=wait" report the last unlock try once; drop the
  // flag from the address so a later reload shows the plain lock screen.
  if (window.location.search && window.history.replaceState) {
    window.history.replaceState(null, "", window.location.pathname);
  }

  // ── Clock ────────────────────────────────────────────────────────────
  // Ticks from the server's reading (data-now) rather than the tablet's
  // own clock, so a tablet set to the wrong time or zone still shows the
  // household time. When the household day changes, reload: the server
  // spells the new date in the dashboard language.
  var clock = root.querySelector("[data-clock]");
  if (clock) {
    var tz = clock.getAttribute("data-tz") || "UTC";
    var day = clock.getAttribute("data-day");
    var skew = Number(clock.getAttribute("data-now")) * 1000 - Date.now();
    var fmt = null;
    try {
      fmt = new Intl.DateTimeFormat("en-GB", {
        timeZone: tz,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
    } catch (e) {
      fmt = null; // Unknown zone in this browser — keep the server time.
    }
    if (fmt && isFinite(skew)) {
      var tick = function () {
        var parts = {};
        fmt.formatToParts(new Date(Date.now() + skew)).forEach(function (p) {
          parts[p.type] = p.value;
        });
        if (parts.year + "-" + parts.month + "-" + parts.day !== day) {
          window.location.replace(window.location.pathname);
          return;
        }
        clock.textContent = parts.hour + ":" + parts.minute;
      };
      window.setInterval(tick, 5000);
    }
  }

  // ── Staying current ──────────────────────────────────────────────────
  // A minute without a touch reloads the lock screen (the no-JS page
  // refreshes the same way): it picks up an admin removing the PIN (the
  // tablet unlocks), the night window's colours, a sign-out — and puts
  // away a PIN pad left open with a half-typed PIN. Every touch or key
  // starts the minute again; a PIN on its way to the server stops it.
  var IDLE_MS = 60000;
  var idleTimer = 0;
  var reloading = true;
  function stayCurrent() {
    window.clearTimeout(idleTimer);
    if (reloading) {
      idleTimer = window.setTimeout(function () {
        window.location.replace(window.location.pathname);
      }, IDLE_MS);
    }
  }
  function stopReloading() {
    reloading = false;
    window.clearTimeout(idleTimer);
  }
  document.addEventListener("pointerdown", stayCurrent, true);
  document.addEventListener("keydown", stayCurrent, true);
  stayCurrent();

  // ── PIN keypad ───────────────────────────────────────────────────────
  var form = root.querySelector("[data-pin-form]");
  var input = form && form.querySelector("input[name=pin]");
  if (!form || !input) {
    return;
  }
  var details = form.closest("details");
  var maxLen = Number(input.getAttribute("maxlength")) || 6;
  var minLen = Number(input.getAttribute("minlength")) || 4;
  var sending = false;

  var dots = document.createElement("div");
  dots.className = "dash-pin-dots";
  dots.setAttribute("aria-hidden", "true");

  var pad = document.createElement("div");
  pad.className = "dash-keypad";

  function key(label, value, aria) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "dash-key";
    b.textContent = label;
    b.setAttribute("data-key", value);
    if (aria) {
      b.setAttribute("aria-label", aria);
    }
    pad.appendChild(b);
    return b;
  }

  ["1", "2", "3", "4", "5", "6", "7", "8", "9"].forEach(function (d) {
    key(d, d);
  });
  var del = key("⌫", "del", form.getAttribute("data-label-delete"));
  del.classList.add("dash-key-quiet");
  key("0", "0");
  var ok = key("✓", "ok", form.getAttribute("data-label-submit"));
  ok.classList.add("dash-key-ok");

  function render() {
    var n = input.value.length;
    dots.textContent = "";
    for (var i = 0; i < n; i += 1) {
      var dot = document.createElement("span");
      dot.className = "dash-pin-dot";
      dots.appendChild(dot);
    }
    ok.disabled = sending || n < minLen;
    del.disabled = sending || n === 0;
  }

  function press(value) {
    if (sending) {
      return;
    }
    if (value === "del") {
      input.value = input.value.slice(0, -1);
    } else if (value === "ok") {
      if (input.value.length >= minLen) {
        sending = true;
        stopReloading();
        render();
        form.submit();
        return;
      }
    } else if (input.value.length < maxLen) {
      input.value += value;
    }
    form.classList.remove("dash-lock-error");
    render();
  }

  pad.addEventListener("click", function (event) {
    var b = event.target.closest("[data-key]");
    if (b && !b.disabled) {
      press(b.getAttribute("data-key"));
    }
  });

  // A hardware keyboard (or a paired one) works too while the pad shows.
  document.addEventListener("keydown", function (event) {
    if (details && !details.open) {
      return;
    }
    if (event.key >= "0" && event.key <= "9" && event.key.length === 1) {
      press(event.key);
    } else if (event.key === "Backspace") {
      press("del");
    } else if (event.key === "Enter") {
      press("ok");
    } else {
      return;
    }
    event.preventDefault();
  });

  // The native field stays the form's value carrier; it just isn't typed
  // into any more (read-only, so no OS keyboard covers the keypad).
  input.value = "";
  input.readOnly = true;
  input.tabIndex = -1;
  form.classList.add("dash-has-keypad");
  form.appendChild(dots);
  form.appendChild(pad);
  render();

  function focusPad() {
    var first = pad.querySelector(".dash-key");
    if (first) {
      first.focus();
    }
  }

  if (details) {
    details.addEventListener("toggle", function () {
      if (details.open) {
        focusPad();
      }
    });
    if (details.open) {
      focusPad(); // after a wrong PIN the pad is already showing
    }
  }
})();
