/* Household dashboard — shared page script (CSP script-src 'self').
 *
 * On every page: a page restored from the back/forward cache was
 * rendered for an older state (say, unlocked before someone tapped the
 * lock) — reload so the server decides again what this tablet may see.
 *
 * On a board page (<body data-dash>, Home or a person):
 *   - the clock ticks every second from the server's time (data-now), not
 *     the tablet's own clock; the household day changing reloads the page;
 *   - every 30 s the matching /fragment/… is fetched and swapped into
 *     <main>. A locked marker reloads (the server shows the lock screen),
 *     401 goes to the login form, 404 (person hidden) goes Home, a new
 *     view key (colours, night, rail changed) reloads the whole page;
 *   - two failed refreshes in a row show the offline banner, hide
 *     relative times and grey "now" until a refresh works again;
 *   - a person page returns Home after data-idle-minutes without a touch;
 *   - the Wake Lock keeps the screen on (data-keep-awake);
 *   - every 10 min the content drifts 1–2 px against burn-in (not with
 *     prefers-reduced-motion);
 *   - night mode: a tap shows the normal page for 60 s.
 */
(function () {
  "use strict";

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });

  var body = document.body;
  if (!body || !body.hasAttribute("data-dash")) {
    return;
  }

  var REFRESH_MS = 30000;
  var HOME_PROBE = "/fragment/home";
  var STALE_AFTER = 2;
  var NIGHT_WAKE_MS = 60000;
  var SHIFT_MS = 600000;
  var SHIFTS = ["dash-shift-1", "dash-shift-2", "dash-shift-3", "dash-shift-4"];

  var main = document.querySelector("[data-main]");
  var fragmentUrl = body.getAttribute("data-fragment");
  var viewKey = body.getAttribute("data-view-key");
  var tz = body.getAttribute("data-tz") || "UTC";
  var day = body.getAttribute("data-day");
  var skew = Number(body.getAttribute("data-now")) * 1000 - Date.now();
  var idleMinutes = Number(body.getAttribute("data-idle-minutes")) || 0;
  var keepAwake = body.getAttribute("data-keep-awake") === "1";
  var reducedMotion =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Marks a running script for the CSS (e.g. the night "tap" hint); a
  // class on <html> survives every fragment swap.
  document.documentElement.classList.add("dash-js");

  function go(path) {
    window.location.replace(path);
  }

  // ── Offline state (shared by refresh, midnight and idle return) ──────
  var failures = 0;

  function setStale(stale) {
    body.classList.toggle("dash-stale", stale);
    document.querySelectorAll("[data-offline]").forEach(function (el) {
      el.hidden = !stale;
    });
  }

  function failed() {
    failures += 1;
    if (failures >= STALE_AFTER) {
      setStale(true);
    }
  }

  // Leave this page only when the server answers: offline, a navigation
  // would trade the last good page (and the ticking clock) for the
  // browser's error page. Unreachable → counted as a failed refresh and
  // tried again on a later call (at most every REFRESH_MS).
  var probing = false;
  var lastProbe = 0;
  function goWhenReachable(path, probe) {
    var now = Date.now();
    if (probing || now - lastProbe < REFRESH_MS) {
      return;
    }
    probing = true;
    lastProbe = now;
    fetch(probe, {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    })
      .then(function (res) {
        if (res.ok || res.status === 401 || res.status === 404) {
          go(path);
        } else {
          failed();
        }
      })
      .catch(failed)
      .then(function () {
        probing = false;
      });
  }

  // A picture that can't load (gone meanwhile): show the initials
  // underneath instead of a broken-image icon — for pictures that failed
  // before this deferred script ran, and for later ones.
  function dropBroken(root) {
    root.querySelectorAll(".dash-face img").forEach(function (img) {
      if (img.complete && img.naturalWidth === 0) {
        img.remove();
      }
    });
  }
  dropBroken(document);
  document.addEventListener(
    "error",
    function (event) {
      var el = event.target;
      if (el && el.tagName === "IMG" && el.closest(".dash-face")) {
        el.remove();
      }
    },
    true
  );

  // In the face strip (portrait / phone) bring the current face into view.
  var current = document.querySelector(".dash-rail [aria-current]");
  if (current && current.scrollIntoView) {
    current.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  // ── Clock ────────────────────────────────────────────────────────────
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
    fmt = null; // Unknown zone in this browser — keep the server's time.
  }

  function tick() {
    if (!fmt || !isFinite(skew)) {
      return;
    }
    var parts = {};
    fmt.formatToParts(new Date(Date.now() + skew)).forEach(function (p) {
      parts[p.type] = p.value;
    });
    if (day && parts.year + "-" + parts.month + "-" + parts.day !== day) {
      // Midnight: the server spells the new day (and resets the lists) —
      // once it can be reached; the clock keeps ticking meanwhile.
      goWhenReachable(window.location.pathname, fragmentUrl);
    }
    var text = parts.hour + ":" + parts.minute;
    document.querySelectorAll("[data-clock]").forEach(function (el) {
      if (el.textContent !== text) {
        el.textContent = text;
      }
    });
  }

  tick();
  window.setInterval(tick, 1000);

  // ── Live refresh + offline banner ────────────────────────────────────
  var busy = false;

  function swap(html) {
    var tpl = document.createElement("template");
    tpl.innerHTML = html;
    var view = tpl.content.querySelector("[data-view-key]");
    if (!view) {
      failed();
      return;
    }
    if (view.getAttribute("data-view-key") !== viewKey) {
      window.location.reload();
      return;
    }
    main.replaceChildren(tpl.content);
    failures = 0;
    setStale(false);
    tick();
  }

  function refresh() {
    if (busy || !main || !fragmentUrl || document.hidden) {
      return;
    }
    busy = true;
    fetch(fragmentUrl, {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: { Accept: "text/html" },
    })
      .then(function (res) {
        if (res.headers.get("X-Dashboard-Locked")) {
          go("/");
          return null;
        }
        if (res.status === 401) {
          go("/login?next=" + encodeURIComponent(window.location.pathname));
          return null;
        }
        if (res.status === 404) {
          go("/");
          return null;
        }
        if (!res.ok) {
          failed();
          return null;
        }
        return res.text();
      })
      .then(function (html) {
        if (html !== null && html !== undefined) {
          swap(html);
        }
      })
      .catch(failed)
      .then(function () {
        busy = false;
      });
  }

  window.setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      refresh();
    }
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest("[data-reload]")) {
      window.location.reload();
    }
  });

  // ── Night mode: a tap shows the normal page for a minute ─────────────
  var wakeTimer = null;
  if (body.classList.contains("dash-night")) {
    document.addEventListener(
      "pointerdown",
      function () {
        body.classList.add("dash-woken");
        window.clearTimeout(wakeTimer);
        wakeTimer = window.setTimeout(function () {
          body.classList.remove("dash-woken");
        }, NIGHT_WAKE_MS);
      },
      true
    );
  }

  // ── Idle return to Home (person pages) ───────────────────────────────
  if (idleMinutes > 0) {
    var idleTimer = null;
    var idleDue = function () {
      goWhenReachable("/", HOME_PROBE);
      // Still here (offline)? Try again shortly.
      idleTimer = window.setTimeout(idleDue, REFRESH_MS);
    };
    var armIdle = function () {
      window.clearTimeout(idleTimer);
      idleTimer = window.setTimeout(idleDue, idleMinutes * 60000);
    };
    ["pointerdown", "keydown", "touchstart", "wheel"].forEach(function (name) {
      document.addEventListener(name, armIdle, { passive: true });
    });
    armIdle();
  }

  // ── Wake Lock (keep the screen on) ───────────────────────────────────
  if (keepAwake && navigator.wakeLock && navigator.wakeLock.request) {
    var lock = null;
    var acquire = function () {
      if (lock || document.hidden) {
        return;
      }
      navigator.wakeLock
        .request("screen")
        .then(function (sentinel) {
          lock = sentinel;
          sentinel.addEventListener("release", function () {
            lock = null;
          });
        })
        .catch(function () {
          lock = null; // Not allowed (battery saver, no gesture yet).
        });
    };
    acquire();
    document.addEventListener("visibilitychange", acquire);
    document.addEventListener("pointerdown", acquire, { passive: true });
  }

  // ── Burn-in: drift 1–2 px every 10 minutes ───────────────────────────
  if (!reducedMotion) {
    var shift = 0;
    window.setInterval(function () {
      document.documentElement.classList.remove(SHIFTS[shift]);
      shift = (shift + 1) % SHIFTS.length;
      document.documentElement.classList.add(SHIFTS[shift]);
    }, SHIFT_MS);
  }
})();
