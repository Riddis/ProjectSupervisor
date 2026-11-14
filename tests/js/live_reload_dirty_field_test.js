"use strict";
/* Proves the reload-side fix to C-2's refresh decision against the real live.js source, under plain node (no npm,
   no jsdom) - same harness as live_poll_refresh_test.js. Run directly from a source checkout:
   `node tests/js/live_reload_dirty_field_test.js` (exit 0 = pass). An optional first argument names live.js's own
   path instead - the pytest wrapper passes the installed package's own static/live.js.

   Scenario: the Control Center restarted (the background poll's boot id no longer matches the page's own), nothing
   is in flight and nothing is selected, but the owner has typed into a field on the page and not yet submitted it.
   A reload is due but must be held back exactly as a morph would be: window.location.reload() would wipe the typed
   text with no prompt, so it must not be called while that field is still dirty. The pill must say a reload is
   waiting instead. */
var assert = require("assert");
var path = require("path");
var buildDom = require("./live_dom_stub.js").buildDom;

var LIVE_JS = process.argv[2] || path.resolve(__dirname, "..", "..", "supervisor", "ui", "static", "live.js");
var OLD_BOOT = "boot-1", NEW_BOOT = "boot-2", VERSION = "v1";

var realSetTimeout = global.setTimeout, realClearTimeout = global.clearTimeout;
global.setTimeout = function (fn) { return realSetTimeout(fn, 0); };
global.clearTimeout = function (id) { return realClearTimeout(id); };

function jsonResponse(obj) { return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(obj); } }); }

function buildCurrentPage() {
  var dom = buildDom();
  var document = dom.document, el = dom.el;

  var metaVersion = el("meta"); metaVersion.setAttribute("name", "live-version"); metaVersion.setAttribute("content", VERSION);
  var metaBoot = el("meta"); metaBoot.setAttribute("name", "live-boot"); metaBoot.setAttribute("content", OLD_BOOT);
  document.appendChild(metaVersion);
  document.appendChild(metaBoot);

  var pill = el("button"); pill.id = "live-status";
  document.appendChild(pill);

  var main = el("div"); main.id = "main";
  var field = el("input"); field.type = "text"; field.defaultValue = "original"; field.value = "typed but not sent";
  main.appendChild(field);
  document.appendChild(main);

  return { dom: dom, document: document, pill: pill, main: main, field: field };
}

function delay(ms) { return new Promise(function (resolve) { realSetTimeout(resolve, ms); }); }

function loadLiveJs() {
  delete require.cache[require.resolve(LIVE_JS)];
  require(LIVE_JS);
}

(async function main() {
  var page = buildCurrentPage();

  global.document = page.document;
  global.window = {
    location: { href: "http://x.example/overview", reload: function () { throw new Error("reload must not be called while a field is dirty"); } },
    localStorage: { getItem: function () { return null; }, setItem: function () {}, removeItem: function () {} },
    getSelection: function () { return { isCollapsed: true, anchorNode: null }; },
    Idiomorph: { morph: function () {} },
    confirm: function () { return true; },
  };
  global.window.setTimeout = global.setTimeout;
  global.window.clearTimeout = global.clearTimeout;
  global.fetch = function (url) {
    if (url === "/live/version") return jsonResponse({ v: VERSION, boot: NEW_BOOT });
    throw new Error("the reload-is-held-back path must never fetch the whole page");
  };

  loadLiveJs();                                      // start() runs synchronously: schedules the first tick

  await delay(150);                                  // let the tick -> boot mismatch -> reload decision settle

  assert.strictEqual(page.field.value, "typed but not sent", "the dirty field's text must survive");
  assert.strictEqual(page.pill.getAttribute("data-state"), "waiting",
    "a pending reload with a dirty field on the page must leave the pill on \"waiting\", not reload silently");
  assert.ok(/reload/i.test(page.pill.textContent), "the pill must say a reload is waiting");

  console.log("live_reload_dirty_field_test.js: scenario passed");
  process.exit(0);
})().catch(function (err) {
  console.error(err && err.stack || err);
  process.exit(1);
});
