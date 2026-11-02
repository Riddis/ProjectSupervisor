"use strict";
/* Minimal hand-written DOM stub for running supervisor_plugin_review_rounds/static/review.js under plain node
   (no jsdom, no npm dependency - the cheapest way to prove the keyboard-focus fix actually targets the right
   tile). It implements only what review.js touches: id/class lookup, simple attribute selectors, classList,
   dataset, bubbling click/keydown/focusin, and focus(). */

function makeClassList() {
  var set = new Set();
  return {
    add: function (c) { set.add(c); },
    remove: function (c) { set.delete(c); },
    contains: function (c) { return set.has(c); },
  };
}

function matches(el, selector) {
  if (!el || !selector) return false;
  if (selector[0] === ".") return el.classList.contains(selector.slice(1));
  if (selector[0] === "#") return el.id === selector.slice(1);
  var attr = selector.match(/^\[data-([a-z-]+)="([^"]*)"\]$/);
  if (attr) {
    var key = attr[1].replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
    return !!el.dataset && el.dataset[key] === attr[2];
  }
  return el.tagName === selector.toUpperCase();
}

function collect(root, selector, out) {
  (root.children || []).forEach(function (child) {
    if (matches(child, selector)) out.push(child);
    collect(child, selector, out);
  });
  return out;
}

function buildDom() {
  var idMap = {};
  var document = { tagName: "#document", children: [], parentElement: null, _listeners: {}, activeElement: null };

  function fireEvent(startNode, type, props) {
    var ev = Object.assign({ target: startNode, preventDefault: function () {} }, props || {});
    var node = startNode;
    while (node) {
      (node._listeners[type] || []).slice().forEach(function (fn) { fn(ev); });
      node = node.parentElement;
    }
    return ev;
  }

  function makeElement(tag) {
    var el = {
      tagName: (tag || "div").toUpperCase(), id: "", children: [], parentElement: null,
      dataset: {}, style: { display: "" }, classList: makeClassList(),
      checked: false, value: "", textContent: "", innerHTML: "", hidden: false, _listeners: {},
    };
    el.addEventListener = function (type, fn) { (el._listeners[type] = el._listeners[type] || []).push(fn); };
    el.removeEventListener = function (type, fn) {
      var list = el._listeners[type] || []; var i = list.indexOf(fn); if (i !== -1) list.splice(i, 1);
    };
    el.appendChild = function (child) {
      child.parentElement = el;
      el.children.push(child);
      if (child.id) idMap[child.id] = child;
      return child;
    };
    el.querySelector = function (selector) { return collect(el, selector, [])[0] || null; };
    el.querySelectorAll = function (selector) { return collect(el, selector, []); };
    el.closest = function (selector) {
      var node = el;
      while (node) { if (matches(node, selector)) return node; node = node.parentElement; }
      return null;
    };
    el.getAttribute = function (name) {
      if (name === "id") return el.id;
      if (name.indexOf("data-") === 0) {
        var key = name.slice(5).replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
        return el.dataset[key] === undefined ? null : el.dataset[key];
      }
      return null;
    };
    el.focus = function () {
      document.activeElement = el;
      fireEvent(el, "focusin", {});
    };
    return el;
  }

  document.getElementById = function (id) { return idMap[id] || null; };
  document.createElement = function (tag) { return makeElement(tag); };
  document.querySelector = function (selector) { return collect(document, selector, [])[0] || null; };
  document.querySelectorAll = function (selector) { return collect(document, selector, []); };
  document.addEventListener = function (type, fn) { (document._listeners[type] = document._listeners[type] || []).push(fn); };
  document.appendChild = function (child) {
    child.parentElement = document;
    document.children.push(child);
    if (child.id) idMap[child.id] = child;
    return child;
  };

  return { document: document, makeElement: makeElement, fireEvent: fireEvent };
}

module.exports = { buildDom: buildDom };
