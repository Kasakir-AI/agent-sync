const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {runInNewContext} = require("node:vm");

const {page, plan, applied} = JSON.parse(readFileSync(0, "utf8"));

class Element {
  constructor(tag) { this.tag = tag; }
  children = [];
  classList = {toggle() {}};
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(event, handler) { this[event] = handler; }
  setAttribute() {}
  focus() {}
  showModal() { this.open = true; }
  find(tag, text) {
    if (this.tag === tag && this.textContent === text) return this;
    return this.children.map(child => child.find(tag, text)).find(Boolean);
  }
}

const ids = new Map();
const document = {
  createElement: (tag) => new Element(tag),
  createDocumentFragment: () => new Element(),
  querySelectorAll: () => [],
  querySelector(selector) {
    if (!ids.has(selector)) ids.set(selector, new Element());
    return ids.get(selector);
  },
};
document.querySelector("#scan").textContent = page.match(/id="scan">([^]*?)<\/script>/)[1];
const script = page.match(/<script nonce="[^"]+">([^]*?)<\/script>/)[1];
const requests = [];
async function fetch(route) {
  requests.push(route);
  return {ok: true, json: async () => ({plan, apply: applied})[route]};
}

async function check() {
  runInNewContext(script, {document, fetch});
  const preview = document.querySelector("#preview");
  assert.equal(preview.disabled, false);
  await preview.click();
  const dialog = document.querySelector("#confirmation");
  assert.equal(dialog.open, true);
  const apply = dialog.find("button", "Apply");
  assert.ok(!apply.disabled, "Apply must be enabled when a Check once choice has no file changes");
  await apply.click();
  assert.deepEqual(requests, ["plan", "apply"]);
  assert.ok(dialog.find("h2", "Changes applied"));
}

check();
