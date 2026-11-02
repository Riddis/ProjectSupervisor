"use strict";
/* Proves the exact reviewer scenario against the real review.js source, under plain node (shutil.which("node")
   gates the pytest wrapper, test_review_ui.py) - no npm, no jsdom. Run directly from a source checkout:
   `node tests/js/review_interaction_test.js` (exit 0 = pass). An optional first argument names review.js's own
   path instead - the pytest wrapper passes the installed package's own static/review.js, since on an installed
   wheel (tools/ci_package.py step 4) this script's `tests/` directory has no package source tree beside it to
   find a relative path into. */
var assert = require("assert");
var path = require("path");
var buildDom = require("./dom_stub.js").buildDom;

var REVIEW_JS = process.argv[2] || path.resolve(__dirname, "..", "..", "supervisor_plugin_review_rounds", "static", "review.js");

function buildReviewPage(dom) {
  var document = dom.document;
  var el = dom.makeElement;

  function required(id, tag) {
    var e = el(tag || "div");
    e.id = id;
    return e;
  }

  var grid = required("review-grid");
  grid.classList.add("review-grid");
  grid.dataset.itemId = "R-1";
  grid.dataset.csrf = "test-csrf";
  grid.dataset.hasTags = "";

  var tiles = ["im0", "im1", "im2"].map(function (id) {
    var tile = el("div");
    tile.id = "";
    tile.classList.add("review-tile");
    tile.dataset.id = id;
    tile.dataset.verdict = "";

    var select = el("input");
    select.classList.add("tile-select");
    tile.appendChild(select);

    var thumb = el("img");
    thumb.classList.add("review-thumb");
    thumb.dataset.zoom = id + "-zoom-key";
    tile.appendChild(thumb);

    var badge = el("div");
    badge.classList.add("tile-verdict-badge");
    badge.dataset.role = "verdict-badge";
    tile.appendChild(badge);

    var compare = el("button");
    compare.classList.add("tile-compare");
    compare.dataset.refs = "ref-key-1";
    tile.appendChild(compare);

    var note = el("div");
    note.dataset.role = "note";
    tile.appendChild(note);

    grid.appendChild(tile);
    return tile;
  });

  document.appendChild(grid);

  document.appendChild(required("zoom-overlay"));
  document.appendChild(required("zoom-image", "img"));
  document.appendChild(required("compare-overlay"));
  document.appendChild(required("compare-images"));
  document.appendChild(required("modify-prompt"));
  document.appendChild(required("modify-note-input", "input"));
  document.appendChild(required("modify-note-confirm", "button"));
  document.appendChild(required("modify-note-cancel", "button"));
  document.appendChild(required("select-all", "button"));
  document.appendChild(required("select-none", "button"));

  return { grid: grid, tiles: tiles };
}

function loadReviewJs() {
  delete require.cache[require.resolve(REVIEW_JS)];
  require(REVIEW_JS);
}

function freshHarness() {
  var dom = buildDom();
  var fetchCalls = [];
  global.document = dom.document;
  global.window = { confirm: function () { return true; } };
  global.fetch = function (url, opts) {
    fetchCalls.push({ url: url, opts: opts });
    return new Promise(function () {}); // never resolves - the test only needs the synchronous call captured
  };
  var page = buildReviewPage(dom);
  loadReviewJs();
  return { dom: dom, page: page, fetchCalls: fetchCalls };
}

// ---- scenario 1: click Compare on im1, then press A -> verdict must target im1, not im0 -------------------

(function scenarioCompareThenKey() {
  var h = freshHarness();
  var im1 = h.page.tiles[1];
  var compareBtn = im1.querySelector(".tile-compare");

  h.dom.fireEvent(compareBtn, "click", {});
  assert.strictEqual(h.dom.document.activeElement, im1, "Compare must focus its own tile");
  h.dom.fireEvent(h.dom.document, "keydown", { key: "a", target: h.dom.document.activeElement });

  assert.strictEqual(h.fetchCalls.length, 1, "expected exactly one verdict POST");
  var body = h.fetchCalls[0].opts.body; // URLSearchParams
  assert.strictEqual(body.get("image_id"), "im1", "keyboard verdict after Compare must target im1");
  assert.strictEqual(body.get("verdict"), "ACCEPT");
  console.log("scenario 1 (compare then key) OK");
})();

// ---- scenario 2: press A with no prior interaction -> falls back to the first visible tile -----------------

(function scenarioNoPriorInteraction() {
  var h = freshHarness();
  assert.strictEqual(h.dom.document.activeElement, null, "no click/focus must have happened yet");

  h.dom.fireEvent(h.dom.document, "keydown", { key: "a" });

  assert.strictEqual(h.fetchCalls.length, 1, "expected exactly one verdict POST");
  var body = h.fetchCalls[0].opts.body;
  assert.strictEqual(body.get("image_id"), "im0", "with no prior interaction, A must target the first visible tile");
  console.log("scenario 2 (no prior interaction) OK");
})();

console.log("review_interaction_test.js: all scenarios passed");
