"use strict";
/* Minimal hand-written DOM stub for running supervisor/ui/static/live.js under plain node (no jsdom, no npm
   dependency), modelled on packages/supervisor-plugin-review-rounds/tests/js/dom_stub.js. It implements only what
   live.js touches: id lookup, simple tag/class/[attr="value"] selectors (plus a comma list and a bare [data-x]
   test), classList, dataset-free attribute get/set, appendChild/contains, and a document root with a body. */

function makeClassList() {
  var set = new Set();
  return {
    add: function (c) { set.add(c); },
    remove: function (c) { set.delete(c); },
    contains: function (c) { return set.has(c); },
  };
}

function matches(el, selector) {
  if (!el || el.nodeType !== 1) return false;
  selector = selector.trim();
  if (selector.indexOf(",") !== -1) {
    return selector.split(",").some(function (part) { return matches(el, part); });
  }
  if (selector[0] === "#") return el.id === selector.slice(1);
  if (selector[0] === ".") return el.classList.contains(selector.slice(1));
  var bare = selector.match(/^\[([a-zA-Z-]+)\]$/);
  if (bare) return el.hasAttribute(bare[1]);
  var withTag = selector.match(/^([a-zA-Z0-9]*)\[([a-zA-Z-]+)="([^"]*)"\]$/);
  if (withTag) {
    var tag = withTag[1], attr = withTag[2], value = withTag[3];
    if (tag && el.tagName !== tag.toUpperCase()) return false;
    return el.getAttribute(attr) === value;
  }
  return el.tagName === selector.toUpperCase();
}

function collectDesc(root, selector, out) {
  (root.children || []).forEach(function (child) {
    if (matches(child, selector)) out.push(child);
    collectDesc(child, selector, out);
  });
  return out;
}

function makeElement(tag) {
  var el = {
    nodeType: 1, tagName: (tag || "div").toUpperCase(), id: "", children: [], childNodes: [], parentElement: null,
    _attrs: {}, _listeners: {}, value: "", defaultValue: "", type: "", hidden: false, title: "", textContent: "",
  };
  el.classList = makeClassList();
  el.addEventListener = function (type, fn) { (el._listeners[type] = el._listeners[type] || []).push(fn); };
  el.removeEventListener = function () {};
  el.appendChild = function (child) {
    child.parentElement = el;
    el.children.push(child);
    el.childNodes.push(child);
    return child;
  };
  el.setAttribute = function (name, value) { el._attrs[name] = String(value); if (name === "id") el.id = String(value); };
  el.getAttribute = function (name) {
    if (name === "id") return el.id || null;
    return Object.prototype.hasOwnProperty.call(el._attrs, name) ? el._attrs[name] : null;
  };
  el.hasAttribute = function (name) {
    return name === "id" ? !!el.id : Object.prototype.hasOwnProperty.call(el._attrs, name);
  };
  el.querySelector = function (selector) { return collectDesc(el, selector, [])[0] || null; };
  el.querySelectorAll = function (selector) { return collectDesc(el, selector, []); };
  el.closest = function (selector) {
    var node = el;
    while (node) { if (matches(node, selector)) return node; node = node.parentElement; }
    return null;
  };
  el.contains = function (node) {
    while (node) { if (node === el) return true; node = node.parentElement; }
    return false;
  };
  return el;
}

function buildDom() {
  var idMap = {};

  function registerIds(el) {
    if (el.id) idMap[el.id] = el;
    (el.children || []).forEach(registerIds);
  }

  var document = {
    nodeType: 9, tagName: "#document", children: [], childNodes: [], parentElement: null,
    _listeners: {}, hidden: false, readyState: "complete", title: "",
  };
  document.getElementById = function (id) { return idMap[id] || null; };
  document.querySelector = function (selector) { return collectDesc(document, selector, [])[0] || null; };
  document.querySelectorAll = function (selector) { return collectDesc(document, selector, []); };
  document.addEventListener = function (type, fn) { (document._listeners[type] = document._listeners[type] || []).push(fn); };
  document.appendChild = function (child) {
    child.parentElement = document;
    document.children.push(child);
    document.childNodes.push(child);
    registerIds(child);
    return child;
  };
  document.body = makeElement("body");
  document.appendChild(document.body);

  return { document: document, el: makeElement, registerIds: registerIds };
}

module.exports = { buildDom: buildDom, makeElement: makeElement };
