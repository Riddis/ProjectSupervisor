/* Control Center image review grid: keyboard-driven verdicts, zoom, compare and bulk select.
   No build step, no framework - plain DOM + fetch against /review/{item}/verdict. */
(function () {
  "use strict";
  var grid = document.getElementById("review-grid");
  if (!grid) return;

  var itemId = grid.dataset.itemId;
  var csrfToken = grid.dataset.csrf;
  var hasTags = grid.dataset.hasTags === "1";
  var tiles = Array.prototype.slice.call(grid.querySelectorAll(".review-tile"));
  var VERDICT_KEYS = { a: "ACCEPT", r: "REJECT", x: "MODIFY", g: "REDO" };

  var progressEl = document.getElementById("review-progress");
  var zoomOverlay = document.getElementById("zoom-overlay");
  var zoomImage = document.getElementById("zoom-image");
  var compareOverlay = document.getElementById("compare-overlay");
  var compareImages = document.getElementById("compare-images");
  var modifyPrompt = document.getElementById("modify-prompt");
  var modifyInput = document.getElementById("modify-note-input");
  var pendingModifyTiles = [];
  // The tile a keyboard verdict should target when nothing is selected: updated on every click/focus inside the
  // grid, so "press A after clicking Compare" hits the tile under Compare, not whichever tile happened to load
  // first. document.activeElement is tried first because it reflects real keyboard/tab focus most precisely;
  // this is the fallback for the common case where a click landed on a child (a button, an image) that itself
  // does not carry focus.
  var lastActiveTile = null;

  function isVisible(tile) {
    return tile.style.display !== "none";
  }

  function selectedTiles() {
    var checked = tiles.filter(function (t) {
      return isVisible(t) && t.querySelector(".tile-select").checked;
    });
    return checked;
  }

  function focusedTile() {
    var active = document.activeElement;
    var fromFocus = active && active.closest ? active.closest(".review-tile") : null;
    if (fromFocus) return fromFocus;
    if (lastActiveTile) return lastActiveTile;
    return tiles.filter(isVisible)[0] || null;
  }

  function targetTiles() {
    var sel = selectedTiles();
    if (sel.length) return sel;
    var f = focusedTile();
    return f ? [f] : [];
  }

  function updateProgress(answered, total) {
    if (progressEl && typeof answered === "number") {
      progressEl.textContent = answered + " of " + total;
    }
  }

  function tileNote(tile) {
    var noteEl = tile.querySelector('[data-role="note"]');
    return noteEl ? noteEl.textContent : "";
  }

  function tileTags(tile) {
    var boxes = Array.prototype.slice.call(tile.querySelectorAll(".tile-tag-input"));
    return boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; }).join(",");
  }

  function applyTileResult(tile, verdict, note) {
    tile.dataset.verdict = verdict;
    tile.classList.remove("verdict-pending");
    tile.classList.add("has-verdict");
    var badge = tile.querySelector('[data-role="verdict-badge"]');
    if (badge) badge.textContent = verdict;
    var noteEl = tile.querySelector('[data-role="note"]');
    if (noteEl) noteEl.textContent = note || "";
  }

  // A verdict the running supervisor only *queued* is not yet on disk - a reload before it is applied would show
  // the image as unanswered again, so the tile must not look answered.
  function applyQueuedResult(tile, verdict) {
    tile.classList.add("verdict-pending");
    var badge = tile.querySelector('[data-role="verdict-badge"]');
    if (badge) badge.textContent = verdict + " (queued)";
  }

  function postVerdict(tile, verdict, note) {
    var params = { image_id: tile.dataset.id, verdict: verdict, note: note || "", csrf_token: csrfToken };
    if (hasTags) { params.reason_tags = tileTags(tile); params.reason_tags_set = "1"; }
    var body = new URLSearchParams(params);
    return fetch("/review/" + encodeURIComponent(itemId) + "/verdict", {
      method: "POST", body: body, headers: { "X-Requested-With": "fetch" },
    }).then(function (resp) {
      if (!resp.ok) throw new Error("verdict failed: " + resp.status);
      return resp.json();
    }).then(function (data) {
      if (data.queued) {
        applyQueuedResult(tile, verdict);
      } else {
        applyTileResult(tile, verdict, note);
        updateProgress(data.answered, data.total);
      }
      return data;
    });
  }

  function applyVerdict(verdict, tilesToUpdate, note) {
    tilesToUpdate.forEach(function (tile) {
      postVerdict(tile, verdict, note).then(function () {
        delete tile.dataset.error;
      }).catch(function (err) {
        console.error(err);
        // A dropped verdict must not look saved - console.error alone is invisible outside devtools, so mark
        // the tile itself.
        tile.dataset.error = "verdict failed";
      });
    });
  }

  function openModifyPrompt(tilesToUpdate) {
    pendingModifyTiles = tilesToUpdate;
    modifyInput.value = "";
    modifyPrompt.hidden = false;
    modifyInput.focus();
  }

  document.getElementById("modify-note-confirm").addEventListener("click", function () {
    applyVerdict("MODIFY", pendingModifyTiles, modifyInput.value);
    modifyPrompt.hidden = true;
  });
  document.getElementById("modify-note-cancel").addEventListener("click", function () {
    modifyPrompt.hidden = true;
  });

  function closeOverlays() {
    zoomOverlay.hidden = true;
    compareOverlay.hidden = true;
    modifyPrompt.hidden = true;
  }

  function openZoom(tile) {
    var img = tile.querySelector(".review-thumb");
    if (!img || !img.dataset.zoom) return;
    zoomImage.src = "/thumb/" + img.dataset.zoom;
    zoomOverlay.hidden = false;
  }

  function openCompare(tile) {
    var btn = tile.querySelector(".tile-compare");
    var img = tile.querySelector(".review-thumb");
    var refs = btn ? (btn.dataset.refs || "").split(",").filter(Boolean) : [];
    compareImages.innerHTML = "";
    if (img && img.dataset.zoom) {
      var self_img = document.createElement("img");
      self_img.src = "/thumb/" + img.dataset.zoom;
      self_img.alt = "candidate";
      compareImages.appendChild(self_img);
    }
    refs.forEach(function (key) {
      var refImg = document.createElement("img");
      refImg.src = "/thumb/" + key;
      refImg.alt = "reference";
      compareImages.appendChild(refImg);
    });
    compareOverlay.hidden = false;
  }

  zoomOverlay.addEventListener("click", closeOverlays);
  compareOverlay.addEventListener("click", closeOverlays);

  // Reason tags can be ticked before or after a verdict exists; if the tile already has one, re-send it with
  // the updated tag set immediately so a tag change doesn't silently wait for the next verdict key-press.
  grid.addEventListener("change", function (ev) {
    if (!ev.target.classList.contains("tile-tag-input")) return;
    var tile = ev.target.closest(".review-tile");
    if (tile && tile.dataset.verdict) {
      postVerdict(tile, tile.dataset.verdict, tileNote(tile)).catch(function (err) { console.error(err); });
    }
  });

  grid.addEventListener("click", function (ev) {
    var tile = ev.target.closest(".review-tile");
    if (tile) {
      lastActiveTile = tile;
      if (ev.target.tagName !== "INPUT") tile.focus();
    }
    if (ev.target.classList.contains("review-thumb")) {
      openZoom(tile);
    } else if (ev.target.classList.contains("tile-compare")) {
      openCompare(tile);
    }
  });

  // Covers focus arriving any other way (tile.focus() above, Tab, the arrow-key navigation below) so
  // lastActiveTile stays correct even when the click branch above isn't what moved focus.
  grid.addEventListener("focusin", function (ev) {
    var tile = ev.target.closest ? ev.target.closest(".review-tile") : null;
    if (tile) lastActiveTile = tile;
  });

  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") {
      if (ev.key === "Escape") closeOverlays();
      return;
    }
    if (ev.key === "Escape") { closeOverlays(); return; }
    var key = ev.key.toLowerCase();
    if (key === "z") { var f = focusedTile(); if (f) openZoom(f); ev.preventDefault(); return; }
    if (key === "c") { var f2 = focusedTile(); if (f2) openCompare(f2); ev.preventDefault(); return; }
    if (key in VERDICT_KEYS) {
      var verdict = VERDICT_KEYS[key];
      var targets = targetTiles();
      if (!targets.length) return;
      if (verdict === "MODIFY") {
        openModifyPrompt(targets);
      } else {
        applyVerdict(verdict, targets);
      }
      ev.preventDefault();
      return;
    }
    if (["arrowright", "arrowdown", "arrowleft", "arrowup"].indexOf(key) !== -1) {
      var visible = tiles.filter(isVisible);
      var idx = visible.indexOf(focusedTile());
      var step = (key === "arrowright" || key === "arrowdown") ? 1 : -1;
      var next = visible[Math.min(Math.max(idx + step, 0), visible.length - 1)];
      if (next) next.focus();
      ev.preventDefault();
    }
  });

  // Bulk toolbar.
  document.getElementById("select-all").addEventListener("click", function () {
    tiles.filter(isVisible).forEach(function (t) { t.querySelector(".tile-select").checked = true; });
  });
  document.getElementById("select-none").addEventListener("click", function () {
    tiles.forEach(function (t) { t.querySelector(".tile-select").checked = false; });
  });
  Array.prototype.slice.call(document.querySelectorAll(".bulk-verdict")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      var verdict = btn.dataset.verdict;
      var targets = selectedTiles();
      if (!targets.length) return;
      if (verdict === "MODIFY") openModifyPrompt(targets);
      else applyVerdict(verdict, targets);
    });
  });

  // Filters (client-side only; nothing here touches state). Every filter but "verdict" is config-driven
  // (plugins.review_rounds.filters) and rendered generically by the template as a `.review-filter` control
  // naming its own `data-field`/`data-kind`; a project with no filters configured still gets the built-in
  // verdict one. Reading the tile's filter value through `getAttribute` (not `.dataset`) sidesteps the
  // dash/underscore camel-casing `dataset` does to a configured field's own name.
  var filterEls = Array.prototype.slice.call(document.querySelectorAll(".review-filter"));

  function applyFilters() {
    tiles.forEach(function (tile) {
      var ok = true;
      filterEls.forEach(function (el) {
        if (!ok || !el.value) return;
        var field = el.dataset.field;
        var kind = el.dataset.kind;
        if (kind === "verdict") {
          if (el.value === "_NONE") { if (tile.dataset.verdict) ok = false; }
          else if (tile.dataset.verdict !== el.value) ok = false;
          return;
        }
        var raw = tile.getAttribute("data-filter-" + field);
        if (kind === "select") {
          if (raw !== el.value) ok = false;
        } else if (kind === "number_min") {
          var num = raw === null || raw === "" ? null : parseFloat(raw);
          if (num === null || isNaN(num) || num < parseFloat(el.value)) ok = false;
        } else {
          if ((raw || "").toLowerCase().indexOf(el.value.toLowerCase()) === -1) ok = false;
        }
      });
      tile.style.display = ok ? "" : "none";
    });
  }
  filterEls.forEach(function (el) {
    el.addEventListener("input", applyFilters);
    el.addEventListener("change", applyFilters);
  });
  var filterClear = document.getElementById("filter-clear");
  if (filterClear) {
    filterClear.addEventListener("click", function () {
      filterEls.forEach(function (el) { el.value = ""; });
      applyFilters();
    });
  }

  // Session timer (labelling mode only; harmless no-op otherwise).
  var timerEl = document.getElementById("review-timer");
  var sessionField = document.getElementById("session-seconds-field");
  var startedAt = Date.now();
  if (timerEl) {
    setInterval(function () {
      var elapsed = Math.floor((Date.now() - startedAt) / 1000);
      var mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
      var ss = String(elapsed % 60).padStart(2, "0");
      timerEl.textContent = mm + ":" + ss;
    }, 1000);
  }
  var submitForm = document.getElementById("submit-round-form");
  if (submitForm) {
    submitForm.addEventListener("submit", function (ev) {
      if (sessionField) sessionField.value = Math.floor((Date.now() - startedAt) / 1000);
      var answeredCount = tiles.filter(function (t) { return t.dataset.verdict; }).length;
      if (answeredCount < tiles.length && !document.getElementById("submit-partial").checked) {
        if (!window.confirm((tiles.length - answeredCount) + " image(s) have no verdict. Submit anyway as partial?")) {
          ev.preventDefault();
          return;
        }
        document.getElementById("submit-partial").checked = true;
      }
    });
  }
})();
