"use strict";
/* Proves C-2's refresh regression against the real live.js source, under plain node (shutil.which("node") gates the
   pytest wrapper, test_live_refresh.py) - no npm, no jsdom. Run directly from a source checkout:
   `node tests/js/live_poll_refresh_test.js` (exit 0 = pass). An optional first argument names live.js's own path
   instead - the pytest wrapper passes the installed package's own static/live.js.

   Scenario: the page's own 5-second background poll (the element htmx marks ".htmx-request" while
   `#now-running`'s own fetch is in flight) is the ONLY thing in flight; no field is dirty, nothing is selected.
   A new fingerprint is waiting. The fixed decision must still fetch the whole page and apply it; the broken one
   (any ".htmx-request" anywhere counts as "busy") never does, and the pill is left on "waiting" forever. */
var assert = require("assert");
var path = require("path");
var buildDom = require("./live_dom_stub.js").buildDom;

var LIVE_JS = process.argv[2] || path.resolve(__dirname, "..", "..", "supervisor", "ui", "static", "live.js");
var BOOT_ID = "boot-1", OLD_VERSION = "v1", NEW_VERSION = "v2";

// setTimeout fires on the next real tick instead of after 2s/30s, so the test runs in well under a second.
var realSetTimeout = global.setTimeout, realClearTimeout = global.clearTimeout;
global.setTimeout = function (fn) { return realSetTimeout(fn, 0); };
global.clearTimeout = function (id) { return realClearTimeout(id); };

function jsonResponse(obj) { return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(obj); } }); }
function textResponse(text) { return Promise.resolve({ ok: true, status: 200, text: function () { return Promise.resolve(text); } }); }

function buildCurrentPage() {
  var dom = buildDom();
  var document = dom.document, el = dom.el;

  var metaVersion = el("meta"); metaVersion.setAttribute("name", "live-version"); metaVersion.setAttribute("content", OLD_VERSION);
  var metaBoot = el("meta"); metaBoot.setAttribute("name", "live-boot"); metaBoot.setAttribute("content", BOOT_ID);
  document.appendChild(metaVersion);
  document.appendChild(metaBoot);

  var pill = el("button"); pill.id = "live-status";
  document.appendChild(pill);

  var main = el("div"); main.id = "main";
  var nowRunning = el("section"); nowRunning.id = "now-running";
  nowRunning.classList.add("htmx-request");          // htmx's own 5s poll of this element is in flight
  nowRunning.setAttribute("data-live-poll", "1");     // ... and nothing else: it is the page's OWN background poll
  main.appendChild(nowRunning);
  document.appendChild(main);

  return { dom: dom, document: document, pill: pill, main: main };
}

function buildNextPage() {
  var dom = buildDom();
  var document = dom.document, el = dom.el;

  var metaVersion = el("meta"); metaVersion.setAttribute("name", "live-version"); metaVersion.setAttribute("content", NEW_VERSION);
  var metaBoot = el("meta"); metaBoot.setAttribute("name", "live-boot"); metaBoot.setAttribute("content", BOOT_ID);
  document.appendChild(metaVersion);
  document.appendChild(metaBoot);

  var main = el("div"); main.id = "main";
  var marker = el("div"); marker.id = "updated-marker";
  main.appendChild(marker);
  document.appendChild(main);

  return document;
}

function delay(ms) { return new Promise(function (resolve) { realSetTimeout(resolve, ms); }); }

function loadLiveJs() {
  delete require.cache[require.resolve(LIVE_JS)];
  require(LIVE_JS);
}

(async function main() {
  var page = buildCurrentPage();
  var nextDoc = buildNextPage();

  global.document = page.document;
  global.window = {
    location: { href: "http://x.example/overview", reload: function () { throw new Error("reload must not be called in this scenario"); } },
    localStorage: { getItem: function () { return null; }, setItem: function () {}, removeItem: function () {} },
    getSelection: function () { return { isCollapsed: true, anchorNode: null }; },
    Idiomorph: {
      morph: function (target, children) {
        target.children = []; target.childNodes = [];
        (children || []).forEach(function (c) { target.appendChild(c); });
      },
    },
    confirm: function () { return true; },
  };
  global.window.setTimeout = global.setTimeout;
  global.window.clearTimeout = global.clearTimeout;
  global.fetch = function (url) {
    if (url === "/live/version") return jsonResponse({ v: NEW_VERSION, boot: BOOT_ID });
    return textResponse("<html></html>");          // ignored by the DOMParser stub below; content is fixed
  };
  global.DOMParser = function () {};
  global.DOMParser.prototype.parseFromString = function () { return nextDoc; };

  loadLiveJs();                                      // start() runs synchronously: schedules the first tick

  await delay(150);                                  // let the tick -> fetch -> refresh -> apply chain settle

  assert.strictEqual(page.main.querySelector("#updated-marker") !== null, true,
    "the page-wide refresh never applied while only the page's own background poll was in flight");
  assert.strictEqual(page.pill.getAttribute("data-state"), "live",
    "the Live pill stayed on \"waiting\" although nothing but the page's own poll was in flight");

  console.log("live_poll_refresh_test.js: scenario passed");
  process.exit(0);
})().catch(function (err) {
  console.error(err && err.stack || err);
  process.exit(1);
});
