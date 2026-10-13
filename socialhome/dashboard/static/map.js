/* Household dashboard — Home's Map tab (CSP script-src 'self').
 *
 * The server renders everything the map shows (map_view.py →
 * _home_map.html.j2) with its geography in data-* attributes; this
 * script only decides where on screen it goes. No Leaflet, no pan, no
 * pinch: the wall map is read-only, so a child's swipe can't lose it.
 *
 *   - Framing: Web Mercator at an integer zoom (the tiles' grid). The
 *     frame fits every occupied zone (centre ± radius) and everyone
 *     outside a zone, leaving room for the bubbles above (140 px) and the
 *     zone names below (60 px); never closer than zoom 16, so one person
 *     shows a neighbourhood, not a house. Someone too far for zoom 9
 *     (regional) stays out of the frame: the household keeps it, and an
 *     edge chip points toward them (a tap zooms there).
 *   - Hysteresis: a refresh (every 30 s, dashboard.js) keeps the frame
 *     while everyone stays well inside it; it re-frames when someone
 *     leaves, or when it could zoom in on two refreshes in a row — the
 *     wall never "breathes".
 *   - Tiles: a plain <img> grid from this dashboard's own /tiles/z/x/y
 *     (the browser never talks to a tile server). The grid outlives a
 *     fragment swap, so a refresh never reloads a tile.
 *   - Markers are placed with el.style.left/top (the CSSOM — the CSP
 *     forbids style attributes, not this). Bubbles whose boxes overlap
 *     (grown by 8 px) merge into one with a caption naming the places;
 *     their zones' pins shrink. Zooming in splits them again.
 *   - Selection: a list row or a one-face bubble focuses that person
 *     (zoom 16, a ring, a callout with name, place and age); a bubble of
 *     several zooms to them and shows every face. "Show everyone", the
 *     same tap again, a minute without a touch or leaving the tab goes
 *     back. The selection is a user id kept in memory — it survives a
 *     refresh, never a reload.
 */
(function () {
  "use strict";

  var body = document.body;
  if (!body || !body.hasAttribute("data-dash")) {
    return;
  }

  var TILE = 256;
  var MAX_ZOOM = 16;
  var MIN_FIT_ZOOM = 9;
  var INSET_TOP = 140;
  var INSET_BOTTOM = 60;
  var INSET_SIDE = 140;
  var MERGE_PAD = 8;
  var EDGE_MARGIN = 10;
  var IDLE_RESET_MS = 60000;
  var FACES = 3;
  var FOCUS_DROP = 30; // the focused anchor sits a little below the centre

  var state = {
    frame: null, // {z, cx, cy}: the "everyone" frame, world px at zoom z
    size: "",
    far: "",
    zoomIn: 0,
    select: null, // {kind: "person", id} | {kind: "group", keys: [...]}
    tilesEl: null,
    tiles: {},
    tileZ: -1,
    idle: null,
  };

  function mapEl() {
    return document.querySelector("[data-map]");
  }

  // ── Geometry ──────────────────────────────────────────────────────────

  function project(lat, lon, z) {
    var n = TILE * Math.pow(2, z);
    var s = Math.sin((lat * Math.PI) / 180);
    s = Math.min(Math.max(s, -0.9999), 0.9999);
    return {
      x: ((lon + 180) / 360) * n,
      y: (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n,
    };
  }

  function metersPerPx(lat, z) {
    return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, z);
  }

  function insets(w) {
    return { top: INSET_TOP, bottom: INSET_BOTTOM, side: Math.min(INSET_SIDE, w * 0.2) };
  }

  function bbox(points, z) {
    var b = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
    points.forEach(function (p) {
      var c = project(p.lat, p.lon, z);
      var r = p.r / metersPerPx(p.lat, z);
      b.x0 = Math.min(b.x0, c.x - r);
      b.x1 = Math.max(b.x1, c.x + r);
      b.y0 = Math.min(b.y0, c.y - r);
      b.y1 = Math.max(b.y1, c.y + r);
    });
    return b;
  }

  // The closest zoom (≤ 16, ≥ minZ) at which ``points`` fit the region
  // the chrome leaves free; -1 when none does.
  function bestZoom(points, w, h, minZ) {
    var i = insets(w);
    for (var z = MAX_ZOOM; z >= minZ; z--) {
      var b = bbox(points, z);
      if (b.x1 - b.x0 <= w - 2 * i.side && b.y1 - b.y0 <= h - i.top - i.bottom) {
        return z;
      }
    }
    return -1;
  }

  // The frame at zoom z that centres ``points`` in that region.
  function frameFor(points, z, w, h) {
    var b = bbox(points, z);
    var i = insets(w);
    var mid = i.top + (h - i.top - i.bottom) / 2;
    return { z: z, cx: (b.x0 + b.x1) / 2, cy: (b.y0 + b.y1) / 2 - (mid - h / 2) };
  }

  function toBox(frame, lat, lon, w, h) {
    var c = project(lat, lon, frame.z);
    return { x: c.x - frame.cx + w / 2, y: c.y - frame.cy + h / 2 };
  }

  function escaped(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : value;
  }

  // ── What the server drew ──────────────────────────────────────────────

  function groupsOf(map) {
    return Array.prototype.map.call(map.querySelectorAll("[data-bubble]"), function (el) {
      return {
        el: el,
        key: el.getAttribute("data-bubble"),
        zone: el.getAttribute("data-in-zone"),
        home: el.hasAttribute("data-home"),
        members: (el.getAttribute("data-members") || "").split(" ").filter(Boolean),
        fit: {
          lat: parseFloat(el.getAttribute("data-lat")),
          lon: parseFloat(el.getAttribute("data-lon")),
          r: parseFloat(el.getAttribute("data-radius")) || 0,
        },
        at: null,
      };
    });
  }

  function fits(groups) {
    return groups.map(function (g) {
      return g.fit;
    });
  }

  // Everyone in frame — or, when that needs less than zoom 9, the
  // household (Home, else the fullest bubble) and whoever fits with it;
  // the rest are "far" (edge chips).
  function idealFrame(groups, w, h) {
    var z = bestZoom(fits(groups), w, h, MIN_FIT_ZOOM);
    if (z >= 0) {
      return { frame: frameFor(fits(groups), z, w, h), far: [] };
    }
    var anchor = groups.filter(function (g) {
      return g.home;
    })[0];
    if (!anchor) {
      anchor = groups.reduce(function (best, g) {
        return g.members.length > best.members.length ? g : best;
      }, groups[0]);
    }
    var a = project(anchor.fit.lat, anchor.fit.lon, 0);
    var distance = function (g) {
      var p = project(g.fit.lat, g.fit.lon, 0);
      return Math.hypot(p.x - a.x, p.y - a.y);
    };
    var others = groups
      .filter(function (g) {
        return g !== anchor;
      })
      .sort(function (p, q) {
        return distance(p) - distance(q);
      });
    var keep = [anchor];
    var far = [];
    others.forEach(function (g) {
      if (bestZoom(fits(keep.concat([g])), w, h, MIN_FIT_ZOOM) >= 0) {
        keep.push(g);
      } else {
        far.push(g);
      }
    });
    z = Math.max(bestZoom(fits(keep), w, h, MIN_FIT_ZOOM), MIN_FIT_ZOOM);
    return { frame: frameFor(fits(keep), z, w, h), far: far };
  }

  // Everyone still well inside the current frame's free region?
  function inside(frame, groups, w, h) {
    var i = insets(w);
    return groups.every(function (g) {
      var p = toBox(frame, g.fit.lat, g.fit.lon, w, h);
      var r = g.fit.r / metersPerPx(g.fit.lat, frame.z);
      return (
        p.x - r >= i.side * 0.8 &&
        p.x + r <= w - i.side * 0.8 &&
        p.y - r >= i.top * 0.8 &&
        p.y + r <= h - i.bottom * 0.8
      );
    });
  }

  // Keep the frame unless someone left it (or the box, or who is "far",
  // changed), or it could zoom in on two refreshes in a row.
  function chooseFrame(groups, w, h, force) {
    var ideal = idealFrame(groups, w, h);
    var size = w + "x" + h;
    var far = ideal.far
      .map(function (g) {
        return g.key;
      })
      .join(" ");
    var near = groups.filter(function (g) {
      return ideal.far.indexOf(g) < 0;
    });
    var cur = state.frame;
    var adopt =
      force || !cur || state.size !== size || state.far !== far || !inside(cur, near, w, h);
    if (!adopt && ideal.frame.z > cur.z) {
      state.zoomIn += 1;
      adopt = state.zoomIn >= 2;
    } else if (!adopt) {
      state.zoomIn = 0;
    }
    if (adopt) {
      state.frame = ideal.frame;
      state.size = size;
      state.far = far;
      state.zoomIn = 0;
    }
    return ideal.far;
  }

  // ── Tiles ─────────────────────────────────────────────────────────────

  // A tile that can't load stays the page colour (the markers and the
  // list still work; the offline banner covers a dead server).
  function hideSelf(event) {
    event.target.hidden = true;
  }

  function drawTiles(map, frame, w, h) {
    var holder = map.querySelector("[data-map-tiles]");
    if (!holder) {
      return;
    }
    if (state.tilesEl && holder !== state.tilesEl) {
      holder.replaceWith(state.tilesEl); // a refresh keeps the loaded grid
      holder = state.tilesEl;
    }
    state.tilesEl = holder;
    if (state.tileZ !== frame.z) {
      holder.replaceChildren();
      state.tiles = {};
      state.tileZ = frame.z;
    }
    var n = Math.pow(2, frame.z);
    var x0 = Math.round(frame.cx - w / 2);
    var y0 = Math.round(frame.cy - h / 2);
    var want = {};
    for (var ty = Math.floor(y0 / TILE); ty <= Math.floor((y0 + h) / TILE); ty++) {
      if (ty < 0 || ty >= n) {
        continue;
      }
      for (var tx = Math.floor(x0 / TILE); tx <= Math.floor((x0 + w) / TILE); tx++) {
        var key = frame.z + "/" + tx + "/" + ty;
        want[key] = true;
        var img = state.tiles[key];
        if (!img) {
          img = document.createElement("img");
          img.className = "dash-mp-tile";
          img.alt = "";
          img.decoding = "async";
          img.draggable = false;
          img.addEventListener("error", hideSelf);
          img.src = "/tiles/" + frame.z + "/" + (((tx % n) + n) % n) + "/" + ty;
          state.tiles[key] = img;
          holder.appendChild(img);
        }
        img.style.left = tx * TILE - x0 + "px";
        img.style.top = ty * TILE - y0 + "px";
      }
    }
    Object.keys(state.tiles).forEach(function (key) {
      if (!want[key]) {
        state.tiles[key].remove();
        delete state.tiles[key];
      }
    });
  }

  // ── Markers ───────────────────────────────────────────────────────────

  function place(el, p) {
    el.style.left = p.x + "px";
    el.style.top = p.y + "px";
  }

  function rectIn(el, box) {
    var r = el.getBoundingClientRect();
    return {
      x0: r.left - box.left,
      y0: r.top - box.top,
      x1: r.right - box.left,
      y1: r.bottom - box.top,
    };
  }

  function overlap(a, b) {
    return (
      a.x0 - MERGE_PAD < b.x1 + MERGE_PAD &&
      b.x0 - MERGE_PAD < a.x1 + MERGE_PAD &&
      a.y0 - MERGE_PAD < b.y1 + MERGE_PAD &&
      b.y0 - MERGE_PAD < a.y1 + MERGE_PAD
    );
  }

  // Nudge a bubble sideways back into the box (its tail then points a
  // little off its anchor — better than half a face off the edge).
  function keepIn(el, map, w) {
    var r = rectIn(el, map.getBoundingClientRect());
    var shift = r.x0 < 4 ? 4 - r.x0 : r.x1 > w - 4 ? w - 4 - r.x1 : 0;
    if (shift) {
      el.style.left = parseFloat(el.style.left) + shift + "px";
    }
  }

  function hideGroup(map, g) {
    g.el.classList.add("dash-mp-off");
    var dot = map.querySelector('[data-dot="' + escaped(g.key) + '"]');
    if (dot) {
      dot.classList.add("dash-mp-off");
    }
  }

  function faceCopies(parts) {
    var faces = [];
    var seen = {};
    parts.forEach(function (g) {
      g.el.querySelectorAll("[data-face]").forEach(function (face) {
        var id = face.getAttribute("data-face");
        if (!seen[id]) {
          seen[id] = true;
          var copy = face.cloneNode(true);
          copy.classList.remove("dash-mp-extra");
          faces.push(copy);
        }
      });
    });
    return faces;
  }

  function fillFaces(holder, faces) {
    faces.forEach(function (face, i) {
      if (i >= FACES) {
        face.classList.add("dash-mp-extra");
      }
      holder.appendChild(face);
    });
    if (faces.length > FACES) {
      var more = document.createElement("span");
      more.className = "dash-mp-more";
      more.textContent = "+" + (faces.length - FACES);
      holder.appendChild(more);
    }
  }

  // "🏠 Home · 🏫 School", after two places "… · +1 place"; anyone
  // away adds "🚗 Away" at the end.
  function caption(map, parts) {
    var places = [];
    var away = null;
    parts.forEach(function (g) {
      var text = g.el.getAttribute("data-caption");
      if (g.el.hasAttribute("data-away")) {
        away = text;
      } else if (places.indexOf(text) < 0) {
        places.push(text);
      }
    });
    var out = places.slice(0, 2);
    var rest = places.length - out.length;
    if (rest > 0) {
      out.push(
        rest === 1
          ? map.getAttribute("data-more-one")
          : (map.getAttribute("data-more-other") || "").replace("{n}", String(rest))
      );
    }
    if (away) {
      out.push(away);
    }
    return out.join(" · ");
  }

  // One bubble for several: at their mean x, above the top-most anchor.
  function mergedBubble(map, parts) {
    var el = document.createElement("span");
    var zoned = parts.some(function (g) {
      return g.zone;
    });
    el.className =
      "dash-mp-bubble dash-mp-bubble-merged " +
      (zoned ? "dash-mp-bubble-zone" : "dash-mp-bubble-free");
    el.setAttribute("data-made", "");
    el.setAttribute(
      "data-merge",
      parts
        .map(function (g) {
          return g.key;
        })
        .join(" ")
    );
    var faces = document.createElement("span");
    faces.className = "dash-mp-faces";
    fillFaces(faces, faceCopies(parts));
    el.appendChild(faces);
    var cap = document.createElement("span");
    cap.className = "dash-mp-caption";
    cap.textContent = caption(map, parts);
    el.appendChild(cap);
    var x = 0;
    var y = Infinity;
    parts.forEach(function (g) {
      x += g.at.x;
      y = Math.min(y, g.at.y);
    });
    place(el, { x: x / parts.length, y: y });
    map.appendChild(el);
    return el;
  }

  // Bubbles that overlap on screen merge; a merged bubble that then
  // overlaps another merges again (a few rounds at most).
  function mergeOverlaps(map, groups, w) {
    var units = groups
      .filter(function (g) {
        return !g.el.classList.contains("dash-mp-off");
      })
      .map(function (g) {
        return { parts: [g], el: g.el };
      });
    for (var round = 0; round < 4; round++) {
      var box = map.getBoundingClientRect();
      var rects = units.map(function (u) {
        return rectIn(u.el, box);
      });
      var root = units.map(function (_, i) {
        return i;
      });
      var find = function (i) {
        while (root[i] !== i) {
          i = root[i];
        }
        return i;
      };
      var joined = false;
      for (var i = 0; i < units.length; i++) {
        for (var j = i + 1; j < units.length; j++) {
          if (find(i) !== find(j) && overlap(rects[i], rects[j])) {
            root[find(j)] = find(i);
            joined = true;
          }
        }
      }
      if (!joined) {
        return;
      }
      var clusters = {};
      var order = [];
      units.forEach(function (u, k) {
        var r = find(k);
        if (!clusters[r]) {
          clusters[r] = [];
          order.push(r);
        }
        clusters[r].push(u);
      });
      units = order.map(function (r) {
        var cluster = clusters[r];
        if (cluster.length === 1) {
          return cluster[0];
        }
        var parts = [];
        cluster.forEach(function (u) {
          if (u.el.hasAttribute("data-made")) {
            u.el.remove();
          }
          parts = parts.concat(u.parts);
        });
        parts.sort(function (p, q) {
          return groups.indexOf(p) - groups.indexOf(q);
        });
        parts.forEach(function (g) {
          g.el.classList.add("dash-mp-off");
          if (g.zone) {
            var pin = map.querySelector('.dash-mp-zone[data-zone="' + escaped(g.zone) + '"]');
            if (pin) {
              pin.classList.add("dash-mp-zone-mini");
            }
          }
        });
        var el = mergedBubble(map, parts);
        // Selected (or holding the selected person behind "+N"): every
        // face shows — a tap on a merged bubble always reveals who.
        var keys = selectedKeys(groups);
        var hidden = el.querySelector(".dash-mp-extra.dash-mp-selected");
        var all = parts.every(function (g) {
          return keys.indexOf(g.key) >= 0;
        });
        el.classList.toggle("dash-mp-expanded", Boolean(hidden) || (all && keys.length > 0));
        keepIn(el, map, w);
        return { parts: parts, el: el };
      });
    }
  }

  // Someone beyond the frame: a chip on the box edge, toward them.
  function edgeChip(map, g, frame, w, h) {
    var chip = document.createElement("span");
    chip.className = "dash-mp-edge";
    chip.setAttribute("data-made", "");
    chip.setAttribute("data-edge", g.key);
    var arrow = document.createElement("span");
    arrow.className = "dash-mp-edge-arrow";
    arrow.textContent = "➜";
    chip.appendChild(arrow);
    var face = g.el.querySelector("[data-face]");
    if (face) {
      var copy = face.cloneNode(true);
      copy.classList.remove("dash-mp-extra");
      chip.appendChild(copy);
    }
    var label = document.createElement("span");
    label.className = "dash-mp-edge-label";
    label.textContent = g.el.getAttribute("data-label") || "";
    chip.appendChild(label);
    map.appendChild(chip);
    var p = toBox(frame, g.fit.lat, g.fit.lon, w, h);
    var dx = p.x - w / 2;
    var dy = p.y - h / 2;
    var size = chip.getBoundingClientRect();
    var hx = Math.max(1, w / 2 - size.width / 2 - EDGE_MARGIN);
    var hy = Math.max(1, h / 2 - size.height / 2 - EDGE_MARGIN);
    var tx = hx / Math.max(Math.abs(dx), 1e-9);
    var ty = hy / Math.max(Math.abs(dy), 1e-9);
    var t = Math.min(tx, ty);
    var at = { x: w / 2 + dx * t, y: h / 2 + dy * t };
    arrow.style.transform = "rotate(" + (Math.atan2(dy, dx) * 180) / Math.PI + "deg)";
    // Slide along that edge to the nearest spot that covers no marker.
    var alongY = tx < ty; // on the left / right edge: move up and down
    var box = map.getBoundingClientRect();
    var blockers = Array.prototype.filter.call(
      map.querySelectorAll(".dash-mp-bubble, [data-edge], .dash-mp-zone"),
      function (el) {
        return el !== chip && !el.classList.contains("dash-mp-off");
      }
    );
    var free = function () {
      var r = rectIn(chip, box);
      return blockers.every(function (el) {
        return !overlap(r, rectIn(el, box));
      });
    };
    for (var k = 0; k <= 60; k++) {
      var step = (k % 2 ? 1 : -1) * Math.ceil(k / 2) * 12;
      var x = alongY ? at.x : Math.min(Math.max(at.x + step, size.width / 2 + EDGE_MARGIN), w - size.width / 2 - EDGE_MARGIN);
      var y = alongY ? Math.min(Math.max(at.y + step, size.height / 2 + EDGE_MARGIN), h - size.height / 2 - EDGE_MARGIN) : at.y;
      place(chip, { x: x, y: y });
      if (free()) {
        return;
      }
    }
    place(chip, at);
  }

  // An empty zone is context only: it gives way when its pin would land
  // on the people (a wide zoom) or be cut by the box edge.
  function clearEmptyZones(map, w, h) {
    var box = map.getBoundingClientRect();
    var busy = Array.prototype.filter
      .call(map.querySelectorAll(".dash-mp-bubble, .dash-mp-zone:not(.dash-mp-zone-empty), .dash-mp-dot"), function (el) {
        return !el.classList.contains("dash-mp-off");
      })
      .map(function (el) {
        return rectIn(el, box);
      });
    map.querySelectorAll(".dash-mp-zone-empty").forEach(function (zone) {
      var r = rectIn(zone.querySelector(".dash-mp-pin") || zone, box);
      var cut = r.x0 < 0 || r.y0 < 0 || r.x1 > w || r.y1 > h;
      if (
        cut ||
        busy.some(function (b) {
          return overlap(r, b);
        })
      ) {
        zone.classList.add("dash-mp-off");
        var area = map.querySelector('[data-area="' + escaped(zone.getAttribute("data-zone")) + '"]');
        if (area) {
          area.classList.add("dash-mp-off");
        }
      }
    });
  }

  // ── Selection ─────────────────────────────────────────────────────────

  function selectedKeys(groups) {
    var sel = state.select;
    if (!sel) {
      return [];
    }
    if (sel.kind === "group") {
      return groups
        .filter(function (g) {
          return sel.keys.indexOf(g.key) >= 0;
        })
        .map(function (g) {
          return g.key;
        });
    }
    return groups
      .filter(function (g) {
        return g.members.indexOf(sel.id) >= 0;
      })
      .map(function (g) {
        return g.key;
      });
  }

  function focusFrame(groups, w, h) {
    var keys = selectedKeys(groups);
    var chosen = groups.filter(function (g) {
      return keys.indexOf(g.key) >= 0;
    });
    if (state.select.kind === "person") {
      var c = project(chosen[0].fit.lat, chosen[0].fit.lon, MAX_ZOOM);
      return { z: MAX_ZOOM, cx: c.x, cy: c.y - FOCUS_DROP };
    }
    var z = Math.max(bestZoom(fits(chosen), w, h, 0), 0);
    return frameFor(fits(chosen), z, w, h);
  }

  function selectedPeople(groups) {
    var sel = state.select;
    if (!sel) {
      return [];
    }
    if (sel.kind === "person") {
      return [sel.id];
    }
    var keys = selectedKeys(groups);
    var out = [];
    groups.forEach(function (g) {
      if (keys.indexOf(g.key) >= 0) {
        out = out.concat(g.members);
      }
    });
    return out;
  }

  function showSelection(map, groups, frame, w, h) {
    var sel = state.select;
    var ids = selectedPeople(groups);
    document.querySelectorAll("[data-select]").forEach(function (row) {
      var on = ids.indexOf(row.getAttribute("data-select")) >= 0;
      row.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var reset = document.querySelector("[data-map-reset]");
    if (reset) {
      reset.hidden = !sel;
    }
    var keys = selectedKeys(groups);
    var person = sel && sel.kind === "person" ? sel.id : null;
    groups.forEach(function (g) {
      var on = keys.indexOf(g.key) >= 0;
      var mine = person ? g.el.querySelector('[data-face="' + escaped(person) + '"]') : null;
      // A group shows every face; a person tucked behind "+N" too.
      var open = on && (!person || (mine && mine.classList.contains("dash-mp-extra")));
      g.el.classList.toggle("dash-mp-expanded", Boolean(open));
      g.el.querySelectorAll("[data-face]").forEach(function (face) {
        face.classList.toggle("dash-mp-selected", face === mine);
      });
    });
    map.querySelectorAll("[data-callout]").forEach(function (callout) {
      var show = person !== null && callout.getAttribute("data-callout") === person;
      callout.hidden = !show;
      if (show) {
        var g = groups.filter(function (x) {
          return x.members.indexOf(person) >= 0;
        })[0];
        place(callout, toBox(frame, g.fit.lat, g.fit.lon, w, h));
      }
    });
  }

  function armIdle() {
    window.clearTimeout(state.idle);
    if (state.select) {
      state.idle = window.setTimeout(function () {
        state.select = null;
        layout(false);
      }, IDLE_RESET_MS);
    }
  }

  // Tapping what is selected again goes back to everyone.
  function select(next) {
    var cur = state.select;
    var same =
      cur !== null &&
      next !== null &&
      cur.kind === next.kind &&
      (next.kind === "person" ? cur.id === next.id : cur.keys.join(" ") === next.keys.join(" "));
    state.select = same ? null : next;
    armIdle();
    layout(false);
  }

  // ── Layout ────────────────────────────────────────────────────────────

  function layout(force) {
    var map = mapEl();
    if (!map) {
      return;
    }
    var w = map.clientWidth;
    var h = map.clientHeight;
    if (!w || !h) {
      return;
    }
    map.querySelectorAll("[data-made]").forEach(function (el) {
      el.remove();
    });
    map.querySelectorAll(".dash-mp-off, .dash-mp-zone-mini, .dash-mp-cut").forEach(function (el) {
      el.classList.remove("dash-mp-off", "dash-mp-zone-mini", "dash-mp-cut");
    });
    var groups = groupsOf(map);
    if (!groups.length) {
      return;
    }
    if (state.select && !selectedKeys(groups).length) {
      state.select = null; // they left the map meanwhile
    }
    var far = chooseFrame(groups, w, h, force);
    var focus = state.select ? focusFrame(groups, w, h) : null;
    var frame = focus || state.frame;
    drawTiles(map, frame, w, h);
    map.querySelectorAll(".dash-mp-area").forEach(function (el) {
      var lat = parseFloat(el.getAttribute("data-lat"));
      place(el, toBox(frame, lat, parseFloat(el.getAttribute("data-lon")), w, h));
      var px = (2 * parseFloat(el.getAttribute("data-radius"))) / metersPerPx(lat, frame.z);
      el.style.width = px + "px";
    });
    map.querySelectorAll(".dash-mp-zone, .dash-mp-dot").forEach(function (el) {
      var lat = parseFloat(el.getAttribute("data-lat"));
      place(el, toBox(frame, lat, parseFloat(el.getAttribute("data-lon")), w, h));
    });
    groups.forEach(function (g) {
      g.at = toBox(frame, g.fit.lat, g.fit.lon, w, h);
      place(g.el, g.at);
    });
    showSelection(map, groups, frame, w, h);
    var box = map.getBoundingClientRect();
    if (focus) {
      // Zoomed in: what doesn't fit is not drawn — never half a bubble.
      groups.forEach(function (g) {
        var r = rectIn(g.el, box);
        if (r.x0 < 0 || r.y0 < 0 || r.x1 > w || r.y1 > h) {
          hideGroup(map, g);
        }
      });
    } else {
      far.forEach(function (g) {
        hideGroup(map, g);
      });
      groups.forEach(function (g) {
        if (!g.el.classList.contains("dash-mp-off")) {
          keepIn(g.el, map, w);
        }
      });
    }
    mergeOverlaps(map, groups, w);
    if (!focus) {
      far.forEach(function (g) {
        edgeChip(map, g, frame, w, h);
      });
    }
    clearEmptyZones(map, w, h);
    map.querySelectorAll(".dash-mp-zone-name").forEach(function (name) {
      var r = rectIn(name, box);
      if (r.x0 < 4 || r.y0 < 4 || r.x1 > w - 4 || r.y1 > h - 4) {
        name.classList.add("dash-mp-cut");
      }
    });
  }

  // ── Wiring ────────────────────────────────────────────────────────────

  document.addEventListener("click", function (event) {
    var t = event.target;
    if (!t || !t.closest) {
      return;
    }
    var row = t.closest("[data-select]");
    if (row) {
      select({ kind: "person", id: row.getAttribute("data-select") });
      return;
    }
    if (t.closest("[data-map-reset]")) {
      state.select = null;
      armIdle();
      layout(false);
      return;
    }
    var edge = t.closest("[data-edge]");
    var bubble = edge
      ? document.querySelector('[data-bubble="' + escaped(edge.getAttribute("data-edge")) + '"]')
      : t.closest("[data-bubble], [data-merge]");
    if (!bubble) {
      return;
    }
    if (bubble.hasAttribute("data-merge")) {
      select({ kind: "group", keys: bubble.getAttribute("data-merge").split(" ") });
      return;
    }
    var ids = (bubble.getAttribute("data-members") || "").split(" ").filter(Boolean);
    if (ids.length === 1) {
      select({ kind: "person", id: ids[0] });
    } else {
      select({ kind: "group", keys: [bubble.getAttribute("data-bubble")] });
    }
  });

  // Any touch keeps a selection for another minute.
  ["pointerdown", "keydown"].forEach(function (name) {
    document.addEventListener(
      name,
      function () {
        if (state.select) {
          armIdle();
        }
      },
      { passive: true }
    );
  });

  // dashboard.js swapped in a fresh fragment: the same frame unless
  // someone left it, the same selection, the same tiles.
  document.addEventListener("dash:swap", function () {
    layout(false);
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () {
      layout(true);
    }, 150);
  });

  layout(true);
})();
