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
    this.className = "";
    this.title = "";
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  async dispatch(type) { await this.listeners.get(type)?.({ currentTarget: this, preventDefault() {} }); }
}

test("Workflow Executions renders generic list and detail state", async () => {
  const script = await readFile(new URL("../mabel/web/portal.js", import.meta.url), "utf8");
  const ids = new Map();
  for (const id of [
    "announcements", "refresh-status", "show-add-connection", "cancel-connection-form",
    "cancel-connection-form-secondary", "connection-form", "show-add-repository",
    "cancel-repository-form", "cancel-repository-form-secondary", "repository-form",
    "generate-api-token", "refresh-capabilities", "portal-logout", "capability-feedback",
    "capability-list", "health-badge", "health-value", "health-meta", "connection-count",
    "connection-value", "repository-count", "repository-value", "refresh-workflow-executions",
    "workflow-executions-feedback", "workflow-execution-list", "workflow-executions-list-view",
    "workflow-execution-detail",
  ]) ids.set(id, new Element(id));
  const views = ["workspace", "workflow-executions"].map((view) => Object.assign(new Element(), { dataset: { view } }));
  const navigation = ["workspace", "workflow-executions"].map((view) => Object.assign(new Element(), { dataset: { viewTarget: view } }));
  const shell = new Element();
  let ready;
  const execution = {
    execution_id: "00000000-0000-4000-8000-000000000001",
    workflow: { id: "manga_flow", path: "workflows/manga-flow.md", version: "1.0" },
    status: "stopped", created_at: "2026-08-29T12:00:00.000000+00:00",
    updated_at: "2026-08-29T12:01:00.000000+00:00", next_step_id: "write_file",
    active_gate_id: "operator_review", requires_attention: true, failure_code: null,
  };
  const detail = {
    ...execution, base_revision: "a".repeat(40), latest_gate_decision: null,
    proposal_id: null, committed_revision: null, failure: null,
    progression: { completed_steps: [{ step_id: "prepare_story", outcome: "success" }], next_step_id: "write_file", terminal_status: null },
  };
  const document = {
    cookie: "", getElementById: (id) => ids.get(id),
    querySelector: (selector) => selector === ".portal-shell" ? shell : null,
    querySelectorAll: (selector) => selector === "[data-view]" ? views : selector === "[data-view-target]" ? navigation : [],
    createElement: () => new Element(), addEventListener: (type, listener) => { if (type === "DOMContentLoaded") ready = listener; },
  };
  const calls = [];
  const response = (body) => ({ status: 200, ok: true, json: async () => body });
  const fetch = async (path) => {
    calls.push(path);
    if (path === "/api/portal/workflow-executions") return response({ executions: [execution], limit: 50, has_more: false });
    if (path.endsWith(execution.execution_id)) return response(detail);
    if (path === "/health") return response({ ok: true, service: "MABEL" });
    if (path === "/api/connections" || path === "/api/managed-repositories") return response([]);
    return response({});
  };
  const window = { addEventListener() {}, dispatchEvent() {} };
  vm.runInNewContext(script, { document, window, fetch, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } } });

  ready();
  await navigation.find((button) => button.dataset.viewTarget === "workflow-executions").dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(calls.includes("/api/portal/workflow-executions"));
  assert.equal(ids.get("workflow-execution-list").children.length, 1);
  await ids.get("workflow-execution-list").children[0].children[0].dispatch("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.ok(calls.some((path) => path.endsWith(execution.execution_id)));
  assert.equal(ids.get("workflow-executions-list-view").hidden, true);
  assert.equal(ids.get("workflow-execution-detail").hidden, false);
  assert.equal(ids.get("workflow-execution-detail").children[1].children[0].textContent, "Execution state");
});
