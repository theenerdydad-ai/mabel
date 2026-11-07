import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

class Element {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = { add() {}, remove() {}, toggle() {} };
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  async dispatch(type) { await this.listeners.get(type)?.({ currentTarget: this, preventDefault() {} }); }
}

test("selecting Capabilities loads and renders declared capabilities", async () => {
  const script = await readFile(new URL("../mabel/web/portal.js", import.meta.url), "utf8");
  const ids = new Map();
  for (const id of [
    "announcements", "refresh-status", "show-add-connection", "cancel-connection-form",
    "cancel-connection-form-secondary", "connection-form", "show-add-repository",
    "cancel-repository-form", "cancel-repository-form-secondary", "repository-form",
    "generate-api-token", "refresh-capabilities", "portal-logout", "capability-feedback",
    "capability-list", "health-badge", "health-value", "health-meta", "connection-count",
    "connection-value", "repository-count", "repository-value",
  ]) ids.set(id, new Element(id));
  const views = ["workspace", "capabilities"].map((view) => Object.assign(new Element(), { dataset: { view } }));
  const navigation = ["workspace", "capabilities"].map((view) => Object.assign(new Element(), { dataset: { viewTarget: view } }));
  const shell = new Element();
  let ready;
  const document = {
    cookie: "",
    getElementById: (id) => ids.get(id),
    querySelector: (selector) => selector === ".portal-shell" ? shell : null,
    querySelectorAll: (selector) => selector === "[data-view]" ? views : selector === "[data-view-target]" ? navigation : [],
    createElement: () => new Element(),
    addEventListener: (type, listener) => { if (type === "DOMContentLoaded") ready = listener; },
  };
  const calls = [];
  const response = (body) => ({ status: 200, ok: true, json: async () => body });
  const fetch = async (path) => {
    calls.push(path);
    if (path === "/api/portal/capabilities") return response({ capabilities: [{
      id: "echo", title: "Echo", version: "1.0", enabled: true, status: "active",
      validation: { state: "not_checked" },
    }] });
    if (path === "/health") return response({ ok: true, service: "MABEL" });
    if (path === "/api/connections" || path === "/api/managed-repositories") return response([]);
    return response({});
  };
  const window = { addEventListener() {}, dispatchEvent() {} };
  vm.runInNewContext(script, {
    document, window, fetch, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  });

  ready();
  await navigation.find((button) => button.dataset.viewTarget === "capabilities").dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.ok(calls.includes("/api/portal/capabilities"));
  assert.equal(ids.get("capability-list").children.length, 1);
  assert.equal(ids.get("capability-list").children[0].children[0].children[0].textContent, "Echo");
});
