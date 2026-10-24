/* Live Control Center: an open page keeps itself current, no F5 needed.
 *
 * Every POLL_MS the page asks GET /live/version for the project's fingerprint (supervisor/ui/live.py). When it moved
 * since the page was rendered (or FALLBACK_MS passed, so ages and timers stay fresh) the page is fetched again and
 * the new content is morphed into place with idiomorph: the DOM is patched rather than replaced, so scroll position,
 * focus and opened <details> survive. Never lost:
 *  - what the owner is typing or has changed in a form, or text selected on the page: the update is held back
 *    ("Updates waiting") until the form is untouched again; clicking the badge applies it at once and keeps the
 *    typed values in their own form;
 *  - elements marked data-live-keep (action results) and SSE streams (log and transcript tails).
 * A restarted Control Center (new boot id) or a changed stylesheet reloads the page fully. Polling stops while the
 * tab is hidden. Clicking "Live" pauses updates in this browser; clicking again resumes.
 * A page that keeps its own client-side state (a keyboard-driven grid, say) opts out by carrying an element with
 * data-live-off: it is never refreshed and the badge says so.
 */
(function () {
  "use strict";
  var POLL_MS = 2000, FALLBACK_MS = 30000, PAUSE_KEY = "cc-live-paused";
  var IGNORED_TYPES = { hidden: 1, submit: 1, button: 1, reset: 1, image: 1 };

  var version = metaContent(document, "live-version");
  var boot = metaContent(document, "live-boot");
  var lastRefresh = Date.now();
  var waiting = false;        // a change is known but held back
  var reloadNeeded = false;   // the Control Center restarted or its styles changed: a full reload is due
  var offline = false, inflight = false, timer = null, pill = null;

  function metaContent(doc, name) {
    var m = doc.querySelector('meta[name="' + name + '"]');
    return m ? m.getAttribute("content") || "" : "";
  }
  function stylesheet(doc) {
    var l = doc.querySelector('link[rel="stylesheet"]');
    return l ? l.getAttribute("href") || "" : "";
  }
  function optedOut() { return !!document.querySelector("[data-live-off]"); }
  function paused() {
    try { return window.localStorage.getItem(PAUSE_KEY) === "1"; } catch (e) { return false; }
  }
  function setPaused(on) {
    try {
      if (on) window.localStorage.setItem(PAUSE_KEY, "1"); else window.localStorage.removeItem(PAUSE_KEY);
    } catch (e) { /* storage blocked: the toggle lasts for this page only */ }
  }

  function fieldDirty(f) {
    if (f.tagName === "SELECT") {
      var opts = f.options, anyDefault = false, i;
      for (i = 0; i < opts.length; i++) if (opts[i].defaultSelected) anyDefault = true;
      for (i = 0; i < opts.length; i++) {
        var def = anyDefault ? opts[i].defaultSelected : (!f.multiple && i === 0);
        if (opts[i].selected !== def) return true;
      }
      return false;
    }
    var t = (f.type || "").toLowerCase();
    if (IGNORED_TYPES[t]) return false;
    if (t === "checkbox" || t === "radio") return f.checked !== f.defaultChecked;
    if (t === "file") return !!(f.files && f.files.length);
    return f.value !== f.defaultValue;
  }
  function dirtyFields(root) {
    var out = [], fields = root.querySelectorAll("input, textarea, select");
    for (var i = 0; i < fields.length; i++) if (fieldDirty(fields[i])) out.push(fields[i]);
    return out;
  }
  function formAction(el) {
    var f = el && el.closest ? el.closest("form") : null;
    return f ? f.getAttribute("action") || "" : "";
  }

  // True while an update would get in the owner's way.
  function busy() {
    var main = document.getElementById("main");
    if (!main) return true;
    if (dirtyFields(main).length) return true;
    var sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed && sel.anchorNode && main.contains(sel.anchorNode)) return true;
    return !!document.querySelector(".htmx-request");
  }

  function kept(node) {
    return node.nodeType === 1 && (node.hasAttribute("data-live-keep") || node.hasAttribute("sse-connect"));
  }

  function apply(doc) {
    var main = document.getElementById("main"), next = doc.getElementById("main");
    if (!main || !next) return;
    // A changed field stays untouched, but only where it still belongs: same form action and name.
    var dirty = new Map();
    dirtyFields(main).forEach(function (f) { dirty.set(f, formAction(f)); });
    // Only the CHILDREN are morphed into the live <main>. Handing idiomorph the parsed <main> element itself, even with
    // morphStyle "innerHTML", makes that element the new content: a second <main> nested inside the first (with the
    // same padding, so the page shifted after every refresh). An array of child nodes is morphed as the content.
    var nextChildren = Array.prototype.slice.call(next.childNodes);
    window.Idiomorph.morph(main, nextChildren, {
      morphStyle: "innerHTML",
      ignoreActiveValue: true,
      restoreFocus: true,
      callbacks: {
        beforeNodeMorphed: function (oldNode, newNode) {
          if (oldNode.nodeType !== 1) return true;
          if (kept(oldNode)) return false;
          if (dirty.has(oldNode) && newNode.nodeType === 1 && newNode.getAttribute("name") === oldNode.getAttribute("name")
              && formAction(newNode) === dirty.get(oldNode)) return false;
          return true;
        },
        beforeAttributeUpdated: function (attr, el) {
          return !(attr === "open" && el.tagName === "DETAILS");   // the owner opened or closed it
        }
      }
    });
    var nav = document.querySelector(".topbar nav"), nextNav = doc.querySelector(".topbar nav");
    if (nav && nextNav) window.Idiomorph.morph(nav, Array.prototype.slice.call(nextNav.childNodes), { morphStyle: "innerHTML" });
    if (doc.title) document.title = doc.title;
    if (window.htmx) window.htmx.process(main);
  }

  function refresh(force) {
    if (inflight) return Promise.resolve();
    inflight = true;
    return fetch(window.location.href, { credentials: "same-origin", cache: "no-store",
                                         headers: { "X-Live-Refresh": "1" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        lastRefresh = Date.now();
        var doc = new DOMParser().parseFromString(html, "text/html");
        var nextBoot = metaContent(doc, "live-boot"), nextCss = stylesheet(doc), css = stylesheet(document);
        if ((nextBoot && boot && nextBoot !== boot) || (nextCss && css && nextCss !== css)) reloadNeeded = true;
        if (reloadNeeded) {
          if (force || !busy()) { window.location.reload(); return; }
          waiting = true;
          return;
        }
        if (!force && busy()) { waiting = true; return; }
        if (!window.Idiomorph) { window.location.reload(); return; }
        apply(doc);
        version = metaContent(doc, "live-version") || version;   // stamped before that page read any state
        waiting = false;
      })
      .catch(function (e) {                                        // e.g. the page is gone; the next change retries
        lastRefresh = Date.now();
        if (window.console) window.console.warn("live update failed:", e);
      })
      .then(function () { inflight = false; render(); });
  }

  function tick() {
    timer = null;
    if (document.hidden) return;                                   // visibilitychange restarts the loop
    if (optedOut()) { render(); return; }
    if (paused()) { render(); schedule(); return; }
    fetch("/live/version", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        var wasOffline = offline;
        offline = false;
        if (d.boot && boot && d.boot !== boot) reloadNeeded = true;
        if (reloadNeeded) {
          if (!busy()) { window.location.reload(); return; }
          waiting = true;
          return;
        }
        if (d.v !== version || wasOffline) waiting = true;
        if (waiting) {
          if (!busy()) return refresh(false);
          return;
        }
        if (Date.now() - lastRefresh > FALLBACK_MS && !busy()) return refresh(false);
      })
      .catch(function () { offline = true; })
      .then(function () { render(); schedule(); });
  }

  function schedule() {
    if (!timer && !document.hidden && !optedOut()) timer = window.setTimeout(tick, POLL_MS);
  }

  function render() {
    if (!pill) return;
    var state, label, title;
    pill.disabled = false;
    if (optedOut()) {
      state = "off"; label = "Live off on this page"; pill.disabled = true;
      title = "This page keeps its own state, so it does not update itself; reload it to see changes";
    } else if (paused()) {
      state = "paused"; label = "Live updates paused"; title = "Click to resume live updates on this browser";
    } else if (offline) {
      state = "offline"; label = "Reconnecting…"; title = "The Control Center is not answering; retrying";
    } else if (waiting) {
      state = "waiting";
      label = reloadNeeded ? "Control Center restarted — reload" : "Updates waiting — apply";
      title = reloadNeeded ? "Click to reload this page (unsent text on it is lost)"
                           : "Held back so nothing you are typing or selecting is lost. Click to apply now; typed text is kept.";
    } else {
      state = "live"; label = "Live"; title = "This page updates itself when the project changes. Click to pause.";
    }
    pill.setAttribute("data-state", state);
    pill.textContent = label;
    pill.title = title;
    pill.hidden = false;
  }

  function onPill() {
    if (paused()) { setPaused(false); render(); if (timer) { window.clearTimeout(timer); timer = null; } tick(); return; }
    if (waiting) {
      if (reloadNeeded) {
        var main = document.getElementById("main");
        if (main && dirtyFields(main).length && !window.confirm("Reload this page? What you typed on it is lost.")) return;
        window.location.reload();
        return;
      }
      refresh(true);
      return;
    }
    setPaused(true);
    render();
  }

  function start() {
    pill = document.getElementById("live-status");
    if (pill) pill.addEventListener("click", onPill);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !timer) tick();
    });
    render();
    schedule();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
