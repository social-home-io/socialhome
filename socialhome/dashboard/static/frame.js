/* Household dashboard — the photo screensaver (CSP script-src 'self').
 *
 * Loaded on a board page only while an admin turned the screensaver on
 * and picked albums, and never at night (the server leaves it out then;
 * night starting reloads the page through the view key). After
 * data-frame-minutes without a touch it fetches /fragment/frame (the
 * round: photo, day taken, album) and plays it over the board:
 *
 *   - each photo stays data-interval seconds and crossfades into the
 *     next (CSS, 2 s; a plain cut with prefers-reduced-motion);
 *   - a tap anywhere on it or any key brings the board back (the tap
 *     only wakes — it never presses what lies underneath);
 *   - at the end of a round the next one is fetched; a 404 (turned off,
 *     night, nothing left to show) closes it, the lock marker reloads
 *     into the lock screen, 401 goes to the login form;
 *   - a photo that can't load is skipped; three in a row close it.
 *
 * dashboard.js's idle return to Overview hands over when the wall went
 * idle (sessionStorage), so Overview resumes the screensaver on time
 * instead of starting the wait again.
 */
(function () {
  "use strict";

  var body = document.body;
  var frame = document.querySelector("[data-frame]");
  var minutes = Number(body && body.getAttribute("data-frame-minutes")) || 0;
  if (!frame || minutes <= 0 || body.classList.contains("dash-night")) {
    return;
  }

  var IDLE_KEY = "dash-idle-since";
  var IDLE_KEEP_MS = 3600000;
  var MAX_SKIPS = 3;
  var imgs = frame.querySelectorAll("[data-frame-img]");
  var caption = frame.querySelector("[data-frame-caption]");
  var idleTimer = null;
  var slideTimer = null;
  var loading = false;
  var showing = false;
  var slides = [];
  var index = 0;
  var front = 0;
  var skips = 0;
  var intervalMs = 20000;

  function go(path) {
    window.location.replace(path);
  }

  // When the wall went idle: now, or what dashboard.js's idle return to
  // Overview handed over (used once, only if recent).
  function idleSince() {
    var since = Date.now();
    try {
      var stored = Number(window.sessionStorage.getItem(IDLE_KEY));
      window.sessionStorage.removeItem(IDLE_KEY);
      if (stored > 0 && stored <= since && since - stored < IDLE_KEEP_MS) {
        since = stored;
      }
    } catch (e) {
      // Storage blocked: count from now.
    }
    return since;
  }

  function arm(since) {
    window.clearTimeout(idleTimer);
    var wait = minutes * 60000 - (Date.now() - since);
    idleTimer = window.setTimeout(start, Math.max(wait, 0));
  }

  function rest() {
    if (!showing) {
      arm(Date.now());
    }
  }

  ["pointerdown", "keydown", "touchstart", "wheel"].forEach(function (name) {
    document.addEventListener(name, rest, { passive: true, capture: true });
  });

  function parse(html) {
    var tpl = document.createElement("template");
    tpl.innerHTML = html;
    var round = tpl.content.querySelector("[data-frame-round]");
    if (!round) {
      return [];
    }
    var seconds = Number(round.getAttribute("data-interval"));
    if (seconds > 0) {
      intervalMs = seconds * 1000;
    }
    var out = [];
    round.querySelectorAll("li[data-photo]").forEach(function (li) {
      var text = [li.getAttribute("data-date"), li.getAttribute("data-album")]
        .filter(Boolean)
        .join(" · ");
      out.push({ src: li.getAttribute("data-photo"), caption: text });
    });
    return out;
  }

  // Fetch a round; ``then`` gets the slides, or nothing when there are none.
  function load(then) {
    if (loading) {
      return;
    }
    loading = true;
    fetch("/fragment/frame", {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "manual",
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
        return res.ok ? res.text() : null;
      })
      .then(function (html) {
        loading = false;
        then(html ? parse(html) : []);
      })
      .catch(function () {
        loading = false;
        then([]);
      });
  }

  function start() {
    if (showing || document.hidden) {
      arm(Date.now());
      return;
    }
    load(function (found) {
      if (!found.length) {
        arm(Date.now()); // off, night or nothing to show: ask again later
        return;
      }
      slides = found;
      index = 0;
      skips = 0;
      showing = true;
      frame.hidden = false;
      body.classList.add("dash-framed");
      next();
    });
  }

  function next() {
    if (!showing) {
      return;
    }
    if (index >= slides.length) {
      load(function (found) {
        if (!found.length) {
          stop();
          return;
        }
        slides = found;
        index = 0;
        next();
      });
      return;
    }
    var slide = slides[index];
    index += 1;
    var back = imgs[1 - front];
    back.onload = function () {
      if (!showing) {
        return;
      }
      skips = 0;
      back.classList.add("dash-frame-on");
      imgs[front].classList.remove("dash-frame-on");
      front = 1 - front;
      caption.textContent = slide.caption;
      caption.hidden = !slide.caption;
      slideTimer = window.setTimeout(next, intervalMs);
    };
    back.onerror = function () {
      if (!showing) {
        return;
      }
      skips += 1;
      if (skips >= MAX_SKIPS) {
        stop(); // gone meanwhile (locked, turned off, deleted): the board
        return;
      }
      next();
    };
    back.src = slide.src;
  }

  function stop() {
    showing = false;
    window.clearTimeout(slideTimer);
    frame.hidden = true;
    body.classList.remove("dash-framed");
    caption.hidden = true;
    caption.textContent = "";
    imgs.forEach(function (img) {
      img.onload = null;
      img.onerror = null;
      img.classList.remove("dash-frame-on");
      img.removeAttribute("src");
    });
    arm(Date.now());
  }

  // Any tap on the screensaver, or any key, brings the board back.
  frame.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();
    stop();
  });
  document.addEventListener("keydown", function () {
    if (showing) {
      stop();
    }
  });

  arm(idleSince());
})();
