/* Household dashboard — shared page script (CSP script-src 'self').
 *
 * On every page: a page restored from the back/forward cache was
 * rendered for an older state (say, unlocked before someone tapped the
 * lock) — reload so the server decides again what this tablet may see.
 *
 * On a board page (<body data-dash>, Home or a person):
 *   - the clock ticks every second from the server's time (data-now), not
 *     the tablet's own clock (the picture view's analog hands too); the
 *     household day changing reloads the page;
 *   - every 30 s the matching /fragment/… is fetched and swapped into
 *     <main>. A locked marker reloads (the server shows the lock screen),
 *     401 goes to the login form, 404 (person hidden) or a redirect (the
 *     Map tab with nobody on it, or at night) goes Home, a new view key
 *     (colours, night, rail changed) reloads the whole page; after a swap
 *     a "dash:swap" event tells a tab's own script (map.js);
 *   - two failed refreshes in a row show the offline banner, hide
 *     relative times and grey "now" until a refresh works again;
 *   - a person page, or Home's Messages / Notes tab, returns to Home's
 *     Overview after data-idle-minutes without a touch;
 *   - the Wake Lock keeps the screen on (data-keep-awake);
 *   - every 10 min the content drifts 1–2 px against burn-in (not with
 *     prefers-reduced-motion);
 *   - night mode: a tap shows the normal page for 60 s;
 *   - household messages ([data-say]): a tap reads one aloud with the
 *     browser's speech synthesis in the page language — never on its
 *     own, one at a time, a second tap stops it (a toggle row keeps
 *     aria-pressed and a polite live region in step); only an on-device voice
 *     for the language is used (the text never leaves the tablet) —
 *     without one there is no 🔊 and a tap does nothing.
 *   - mentions ([data-mention-state] in <main>): after every load and
 *     swap the rail's badges ([data-badge]) follow the server, and a
 *     mention this tablet hasn't seen yet plays one short, soft two-note
 *     tone made on the device (WebAudio) — once per mention (remembered
 *     in sessionStorage), never at night, never when the admin turned it
 *     off, and silent where the browser blocks audio (the first tap
 *     anywhere unlocks it);
 *   - "Got it" ([data-ack] forms) posts in the background, takes the
 *     card away and refreshes; without JS the form posts and comes back.
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
        if (res.ok || res.status === 401) {
          go(path);
        } else if (res.status === 404) {
          // The page's person is gone (hidden meanwhile): Home, like
          // refresh() — never a static not-found page with no script.
          go("/");
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
    root.querySelectorAll(".dash-face img, .dash-mp-av img").forEach(function (img) {
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
      if (el && el.tagName === "IMG" && el.closest(".dash-face, .dash-mp-av")) {
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
    // The picture view's analog clock: turn the hands (an SVG transform
    // attribute — not a style, so the CSP has nothing to say) with the
    // server's formula (picture.py hand_angles).
    var minute = Number(parts.minute);
    var angles = {
      hour: (Number(parts.hour) % 12) * 30 + minute * 0.5,
      minute: minute * 6,
    };
    document.querySelectorAll("[data-hand]").forEach(function (el) {
      var deg = angles[el.getAttribute("data-hand")];
      var value = "rotate(" + deg + " 100 100)";
      if (deg !== undefined && el.getAttribute("transform") !== value) {
        el.setAttribute("transform", value);
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
    sayAfterSwap();
    syncMentions();
    document.dispatchEvent(new window.CustomEvent("dash:swap"));
  }

  // A refresh asked for while one is in flight runs right after it (a
  // "Got it" must not be undone by an older fragment landing).
  var again = false;

  function refresh() {
    if (busy) {
      again = true;
      return;
    }
    if (!main || !fragmentUrl || document.hidden) {
      return;
    }
    busy = true;
    fetch(fragmentUrl, {
      credentials: "same-origin",
      cache: "no-store",
      // A redirect is never followed into the swap: it means "this tab
      // is gone" (the Map with nobody on it, or night) — go Home.
      redirect: "manual",
      headers: { Accept: "text/html" },
    })
      .then(function (res) {
        if (res.type === "opaqueredirect") {
          go("/");
          return null;
        }
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
        if (again) {
          again = false;
          refresh();
        }
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

  // ── Household messages: tap to hear one ──────────────────────────────
  // Only on a tap, never on its own; a second tap on the same message
  // stops it; one at a time. Only where the browser has a voice for the
  // page language — otherwise there is no 🔊 and no "Read aloud", and a
  // tap does nothing (the message is still shown).
  var synth = window.speechSynthesis || null;
  var lang = (document.documentElement.getAttribute("lang") || "en").toLowerCase();
  var voice = null;
  var speakingId = null;
  var current = null; // the utterance being spoken

  function findVoice() {
    if (!synth || typeof window.SpeechSynthesisUtterance !== "function") {
      return null;
    }
    var primary = lang.split("-")[0];
    var voices = synth.getVoices() || [];
    var exact = null;
    var near = null;
    voices.forEach(function (v) {
      // On-device voices only: a remote one (Chrome's "Google …") would
      // send the message text off the tablet.
      if (!v || v.localService !== true) {
        return;
      }
      var code = String(v.lang || "").toLowerCase().replace("_", "-");
      if (!exact && code === lang) {
        exact = v;
      } else if (!near && code.split("-")[0] === primary) {
        near = v;
      }
    });
    return exact || near;
  }

  // The polite live region (outside <main>, so a refresh keeps it):
  // written only when its text changes, so a swap never re-announces.
  function sayStatus(text) {
    var el = document.querySelector("[data-say-status]");
    if (el && el.textContent !== text) {
      el.textContent = text;
    }
  }

  function markSay() {
    var can = voice !== null;
    var playing = "";
    document.documentElement.classList.toggle("dash-can-speak", can);
    document.querySelectorAll("[data-say]").forEach(function (el) {
      if (can) {
        el.setAttribute("aria-describedby", "dash-say-hint");
      } else {
        el.removeAttribute("aria-describedby");
      }
      var on = speakingId !== null && el.getAttribute("data-msg") === speakingId;
      el.classList.toggle("dash-speaking", on);
      // The Messages tab's rows are toggle buttons.
      if (el.hasAttribute("aria-pressed")) {
        el.setAttribute("aria-pressed", on ? "true" : "false");
      }
      if (on) {
        playing = el.getAttribute("data-say-playing") || "";
      }
    });
    sayStatus(playing);
  }

  function stopSpeaking() {
    speakingId = null;
    current = null;
    if (synth) {
      synth.cancel();
    }
    markSay();
  }

  function sayAfterSwap() {
    // The message being read was deleted meanwhile: stop reading it.
    if (
      speakingId !== null &&
      !document.querySelector('[data-say][data-msg="' + CSS.escape(speakingId) + '"]')
    ) {
      stopSpeaking();
    } else {
      markSay();
    }
  }

  function updateVoice() {
    voice = findVoice();
    markSay();
  }

  if (synth) {
    updateVoice();
    // Voices often load after the page does (older engines only know the
    // handler property); look once more after a second either way.
    if (synth.addEventListener) {
      synth.addEventListener("voiceschanged", updateVoice);
    } else {
      synth.onvoiceschanged = updateVoice;
    }
    window.setTimeout(updateVoice, 1000);
    // Leaving the page stops it — and forgets it, so a page restored
    // from the back/forward cache shows no stale 🔊.
    window.addEventListener("pagehide", function () {
      stopSpeaking();
    });
  }

  document.addEventListener("click", function (event) {
    var el = event.target.closest && event.target.closest("[data-say]");
    if (!el || voice === null) {
      return;
    }
    var id = el.getAttribute("data-msg");
    if (speakingId === id) {
      stopSpeaking(); // a second tap stops it
      return;
    }
    synth.cancel(); // one at a time
    var utterance = new window.SpeechSynthesisUtterance(el.getAttribute("data-say"));
    utterance.lang = voice.lang || lang;
    try {
      utterance.voice = voice;
    } catch (e) {
      // Can't pin the on-device voice: the engine might pick a remote
      // default — don't speak at all.
      stopSpeaking();
      return;
    }
    if (utterance.voice !== voice) {
      stopSpeaking();
      return;
    }
    // A cancelled utterance reports its end late: only the current one
    // may clear the 🔊.
    var done = function () {
      if (current === utterance) {
        current = null;
        speakingId = null;
        markSay();
      }
    };
    utterance.onend = done;
    utterance.onerror = done;
    current = utterance;
    speakingId = id;
    markSay();
    synth.speak(utterance);
  });

  // ── Mentions: rail badges, a gentle tone for new ones, "Got it" ──────
  var SEEN_KEY = "dash-mentions-seen";
  var SEEN_MAX = 200;
  // Wait no longer than this for a suspended audio context: a resume that
  // only lands on a later tap must not play a stale tone.
  var TONE_LATE_MS = 2000;
  var AudioCtx = window.AudioContext || window.webkitAudioContext || null;
  var audio = null; // one context, unlocked by the first tap, reused

  function loadSeen() {
    try {
      var list = JSON.parse(window.sessionStorage.getItem(SEEN_KEY) || "[]");
      return Array.isArray(list)
        ? list.filter(function (k) {
            return typeof k === "string";
          })
        : [];
    } catch (e) {
      return []; // storage blocked (private mode, a preview): memory only
    }
  }

  var seen = loadSeen();

  function saveSeen() {
    try {
      window.sessionStorage.setItem(SEEN_KEY, JSON.stringify(seen));
    } catch (e) {
      // Remembered in memory for this page only.
    }
  }

  function audioContext() {
    if (!AudioCtx) {
      return null;
    }
    if (!audio) {
      try {
        audio = new AudioCtx();
      } catch (e) {
        audio = null;
      }
    }
    return audio;
  }

  // Two soft notes, once (the app's ringtone shape: sine, a short gain
  // ramp peaking at 0.1, below its 0.12) — synthesised here, nothing fetched.
  function tone(ctx) {
    var now = ctx.currentTime;
    var osc = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.type = "sine";
    osc.connect(gain);
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0, now);
    osc.frequency.setValueAtTime(440, now);
    gain.gain.linearRampToValueAtTime(0.1, now + 0.05);
    gain.gain.linearRampToValueAtTime(0, now + 0.4);
    osc.frequency.setValueAtTime(554, now + 0.45);
    gain.gain.setValueAtTime(0, now + 0.45);
    gain.gain.linearRampToValueAtTime(0.1, now + 0.5);
    gain.gain.linearRampToValueAtTime(0, now + 0.95);
    osc.onended = function () {
      osc.disconnect();
      gain.disconnect();
    };
    osc.start(now);
    osc.stop(now + 1);
  }

  function chime() {
    var ctx = audioContext();
    if (!ctx) {
      return;
    }
    try {
      if (ctx.state === "running") {
        tone(ctx);
        return;
      }
      var asked = Date.now();
      ctx.resume().then(
        function () {
          if (ctx.state === "running" && Date.now() - asked < TONE_LATE_MS) {
            tone(ctx);
          }
        },
        function () {
          // Autoplay blocked: stay silent — the banner is enough.
        }
      );
    } catch (e) {
      // No audio on this device.
    }
  }

  // The first tap anywhere unlocks audio (browsers only start sound
  // after a user gesture).
  document.addEventListener(
    "pointerdown",
    function unlock() {
      document.removeEventListener("pointerdown", unlock, true);
      var ctx = audioContext();
      if (ctx && ctx.state === "suspended") {
        ctx.resume().catch(function () {});
      }
    },
    true
  );
  window.addEventListener("pagehide", function () {
    if (audio) {
      try {
        audio.close();
      } catch (e) {
        // Already closed.
      }
      audio = null;
    }
  });

  function syncMentions() {
    var state = main && main.querySelector("[data-mention-state]");
    if (!state) {
      return;
    }
    var badges = (state.getAttribute("data-mention-badges") || "")
      .split(" ")
      .filter(Boolean);
    document.querySelectorAll("[data-badge]").forEach(function (el) {
      el.hidden = badges.indexOf(el.getAttribute("data-badge")) < 0;
    });
    var fresh = (state.getAttribute("data-mention-keys") || "")
      .split(" ")
      .filter(function (k) {
        return k && seen.indexOf(k) < 0;
      });
    if (!fresh.length) {
      return;
    }
    seen = seen.concat(fresh).slice(-SEEN_MAX);
    saveSeen();
    if (
      state.getAttribute("data-mention-sound") === "1" &&
      !body.classList.contains("dash-night")
    ) {
      chime();
    }
  }

  syncMentions();

  // "Got it": post in the background, take the card away, refresh. A
  // 404 means it is gone anyway (seen on another tablet, deleted, too
  // old); a 429 keeps the card for a later tap.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (
      !form ||
      !form.hasAttribute ||
      !form.hasAttribute("data-ack") ||
      typeof window.URLSearchParams !== "function"
    ) {
      return;
    }
    event.preventDefault();
    var button = form.querySelector("button");
    if (button) {
      button.disabled = true;
    }
    var data = new window.URLSearchParams();
    form.querySelectorAll("input[name]").forEach(function (input) {
      data.append(input.name, input.value);
    });
    fetch(form.getAttribute("action"), {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "manual",
      body: data,
    })
      .then(function (res) {
        if (res.status === 401) {
          go("/login?next=" + encodeURIComponent(window.location.pathname));
        } else if (res.status === 423) {
          go("/");
        } else if (res.type === "opaqueredirect" || res.ok || res.status === 404) {
          var item = form.closest("li");
          if (item) {
            item.remove();
          }
        }
      })
      .catch(function () {
        // Offline: the card stays; the refresh shows the banner.
      })
      .then(function () {
        if (button) {
          button.disabled = false;
        }
        refresh();
      });
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

  // ── Idle return to Overview (person pages, Home's other tabs) ────────
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
