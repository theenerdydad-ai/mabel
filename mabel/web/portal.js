"use strict";

const state = { connections: [], repositories: [], capabilities: [], workflowExecutions: [], editingId: null, reconnectingRepositoryId: null, hasApiToken: false };

const element = (id) => document.getElementById(id);

function cookie(name) {
  const prefix = `${name}=`;
  return document.cookie.split(";").map((value) => value.trim()).find((value) => value.startsWith(prefix))?.slice(prefix.length) || null;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (!/^(GET|HEAD|OPTIONS)$/i.test(options.method || "GET")) {
    const csrf = cookie("mabel_csrf");
    if (csrf) headers["X-MABEL-CSRF"] = csrf;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const loginUrl = body?.detail?.login_url || body?.login_url;
    if (response.status === 401 && loginUrl) window.location.assign(loginUrl);
    throw new Error(`Request failed (${response.status}).`);
  }
  return response.json();
}

function announce(message) {
  element("announcements").textContent = message;
}

function showFeedback(message, isError = false) {
  const feedback = element("connection-feedback");
  feedback.hidden = false;
  feedback.classList.toggle("error", isError);
  feedback.textContent = message;
}

function setView(view) {
  document.querySelector(".portal-shell").classList.toggle("workspace-active", view === "workspace");
  document.querySelectorAll("[data-view]").forEach((section) => {
    section.hidden = section.dataset.view !== view;
  });
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    const active = button.dataset.viewTarget === view;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (view === "connections") refreshConnections();
  if (view === "repositories") refreshRepositories();
  if (view === "capabilities") refreshCapabilities();
  if (view === "workflow-executions") showWorkflowExecutionList();
  if (view === "api-access") refreshApiTokenStatus();
  window.dispatchEvent(new CustomEvent("mabel:viewchange", { detail: { view } }));
}

function renderApiTokenStatus(status) {
  state.hasApiToken = status.has_token;
  element("api-token-status").textContent = status.has_token
    ? "An API token is active for this MABEL account."
    : "No API token has been generated for this MABEL account.";
  element("generate-api-token").textContent = status.has_token ? "Regenerate token" : "Generate token";
}

async function refreshApiTokenStatus() {
  try {
    renderApiTokenStatus(await api("/api/access/token-status"));
  } catch (_) {
    element("api-token-status").textContent = "Token status could not be loaded.";
  }
}

async function generateApiToken() {
  const button = element("generate-api-token");
  button.disabled = true;
  try {
    const issued = await api("/api/access/token", { method: "POST" });
    const disclosure = element("issued-api-token");
    element("issued-api-token-value").textContent = issued.token;
    disclosure.hidden = false;
    renderApiTokenStatus({ has_token: true });
    announce("A new MABEL API token was generated. Copy it now.");
  } catch (_) {
    announce("A MABEL API token could not be generated.");
  } finally {
    button.disabled = false;
  }
}

function updateStatus(health, connections, repositories = []) {
  const healthy = health && health.ok === true;
  const healthBadge = element("health-badge");
  healthBadge.textContent = healthy ? "Healthy" : "Unavailable";
  healthBadge.className = `badge ${healthy ? "" : "warning"}`;
  element("health-value").textContent = healthy ? "MABEL is running" : "MABEL health is unavailable";
  element("health-meta").textContent = healthy
    ? `Service: ${health.service}`
    : "The health endpoint did not return a healthy response.";

  const count = connections.length;
  element("connection-count").textContent = String(count);
  element("connection-value").textContent = count === 1 ? "1 registered connection" : `${count} registered connections`;
  const repositoryCount = repositories.length;
  element("repository-count").textContent = String(repositoryCount);
  element("repository-value").textContent = repositoryCount === 1 ? "1 registered context repository" : `${repositoryCount} registered context repositories`;
}

async function refreshStatus() {
  const refreshButton = element("refresh-status");
  refreshButton.disabled = true;
  try {
    const [health, connections, repositories] = await Promise.all([
      api("/health"), api("/api/connections"), api("/api/managed-repositories"),
    ]);
    state.connections = connections;
    state.repositories = repositories;
    updateStatus(health, connections, repositories);
    announce("MABEL status refreshed.");
  } catch (_) {
    updateStatus(null, state.connections, state.repositories);
    announce("MABEL status could not be refreshed.");
  } finally {
    refreshButton.disabled = false;
  }
}

function renderTestResult(container, result) {
  container.hidden = false;
  container.classList.toggle("error", !result.ok);
  if (!result.ok) {
    container.textContent = `Connection test did not succeed: ${result.error_code}.`;
    return;
  }
  const sha = result.bootstrap.sha ? ` Bootstrap SHA: ${result.bootstrap.sha}.` : "";
  container.textContent = `Connected. Bootstrap and root manifest loaded; ${result.root_manifest.collection_count} root collections found.${sha}`;
}

function renderConnection(connection) {
  const card = element("connection-template").content.firstElementChild.cloneNode(true);
  const head = card.querySelector(".connection-head");
  const detail = card.querySelector(".connection-detail");
  const stateBadge = card.querySelector(".connection-state");
  card.querySelector(".connection-name").textContent = connection.display_name;
  card.querySelector(".connection-endpoint").textContent = connection.endpoint;
  stateBadge.textContent = connection.enabled ? "Enabled" : "Disabled";
  stateBadge.className = `badge ${connection.enabled ? "" : "warning"} connection-state`;
  card.querySelector(".connection-type").textContent = connection.connection_type;
  card.querySelector(".connection-transport").textContent = connection.transport.replaceAll("_", " ");
  card.querySelector(".connection-credential").textContent = connection.credential_configured ? "Configured" : "Not configured";

  head.addEventListener("click", () => {
    const expanded = head.getAttribute("aria-expanded") === "true";
    head.setAttribute("aria-expanded", String(!expanded));
    detail.hidden = expanded;
  });

  const testResult = card.querySelector(".test-result");
  const testAction = card.querySelector(".test-action");
  testAction.addEventListener("click", async () => {
    testAction.disabled = true;
    try {
      renderTestResult(testResult, await api(`/api/connections/${connection.id}/test`, { method: "POST" }));
    } catch (_) {
      renderTestResult(testResult, { ok: false, error_code: "connection_request_failed" });
    } finally {
      testAction.disabled = false;
    }
  });

  const toggleAction = card.querySelector(".toggle-action");
  toggleAction.textContent = connection.enabled ? "Disable" : "Enable";
  toggleAction.addEventListener("click", async () => {
    toggleAction.disabled = true;
    try {
      await api(`/api/connections/${connection.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !connection.enabled }),
      });
      await refreshConnections();
      showFeedback(`Connection ${connection.enabled ? "disabled" : "enabled"}.`);
    } catch (_) {
      showFeedback("Connection state could not be updated.", true);
    }
  });

  card.querySelector(".edit-action").addEventListener("click", () => showEditForm(connection));
  card.querySelector(".delete-action").addEventListener("click", async () => {
    if (!window.confirm(`Delete ${connection.display_name}?`)) return;
    try {
      await api(`/api/connections/${connection.id}`, { method: "DELETE" });
      await refreshConnections();
      showFeedback("Connection deleted.");
    } catch (_) {
      showFeedback("Connection could not be deleted.", true);
    }
  });
  return card;
}

function renderConnections() {
  const list = element("connection-list");
  list.replaceChildren();
  if (state.connections.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No AMIE connections are registered yet.";
    list.append(empty);
    return;
  }
  state.connections.forEach((connection) => list.append(renderConnection(connection)));
}

async function refreshConnections() {
  try {
    state.connections = await api("/api/connections");
    renderConnections();
    updateStatus({ ok: true, service: "mabel" }, state.connections, state.repositories);
  } catch (_) {
    showFeedback("Connections could not be loaded.", true);
  }
}

function showRepositoryFeedback(message, isError = false) {
  const feedback = element("repository-feedback");
  feedback.hidden = false;
  feedback.classList.toggle("error", isError);
  feedback.textContent = message;
}

function renderRepositoryTestResult(container, result) {
  container.hidden = false;
  container.classList.toggle("error", !result.ok);
  container.textContent = result.ok
    ? `Verified ${result.repository.canonical_locator}; default branch: ${result.repository.default_branch}.`
    : `Repository test did not succeed: ${result.error_code}.`;
}

function renderRepository(repository) {
  const card = element("repository-template").content.firstElementChild.cloneNode(true);
  const head = card.querySelector(".connection-head");
  const detail = card.querySelector(".connection-detail");
  const stateBadge = card.querySelector(".repository-state");
  card.querySelector(".repository-name").textContent = repository.display_name;
  card.querySelector(".repository-locator").textContent = repository.canonical_locator;
  stateBadge.textContent = repository.enabled ? repository.status.replaceAll("_", " ") : "Disabled";
  stateBadge.className = `badge ${repository.status === "connected" && repository.enabled ? "" : "warning"} repository-state`;
  card.querySelector(".repository-provider-id").textContent = repository.provider_repository_id;
  card.querySelector(".repository-branch").textContent = repository.default_branch;
  head.addEventListener("click", () => {
    const expanded = head.getAttribute("aria-expanded") === "true";
    head.setAttribute("aria-expanded", String(!expanded));
    detail.hidden = expanded;
  });

  const result = card.querySelector(".test-result");
  const testAction = card.querySelector(".repository-test-action");
  testAction.addEventListener("click", async () => {
    testAction.disabled = true;
    try {
      renderRepositoryTestResult(result, await api(`/api/managed-repositories/${repository.id}/test`, { method: "POST" }));
    } catch (_) {
      renderRepositoryTestResult(result, { ok: false, error_code: "repository_request_failed" });
    } finally {
      testAction.disabled = false;
    }
  });
  card.querySelector(".repository-reconnect-action").addEventListener("click", () => showRepositoryForm(repository));
  const toggleAction = card.querySelector(".repository-toggle-action");
  toggleAction.textContent = repository.enabled ? "Disable" : "Enable";
  toggleAction.addEventListener("click", async () => {
    try {
      await api(`/api/managed-repositories/${repository.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !repository.enabled }),
      });
      await refreshRepositories();
      showRepositoryFeedback(`Repository ${repository.enabled ? "disabled" : "enabled"}.`);
    } catch (_) {
      showRepositoryFeedback("Repository state could not be updated.", true);
    }
  });
  card.querySelector(".repository-delete-action").addEventListener("click", async () => {
    if (!window.confirm(`Delete ${repository.display_name}?`)) return;
    try {
      await api(`/api/managed-repositories/${repository.id}`, { method: "DELETE" });
      await refreshRepositories();
      showRepositoryFeedback("Repository deleted.");
    } catch (_) {
      showRepositoryFeedback("Repository could not be deleted.", true);
    }
  });
  return card;
}

function renderRepositories() {
  const list = element("repository-list");
  list.replaceChildren();
  if (state.repositories.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No MABEL context repositories are registered yet.";
    list.append(empty);
    return;
  }
  state.repositories.forEach((repository) => list.append(renderRepository(repository)));
}

async function refreshRepositories() {
  try {
    state.repositories = await api("/api/managed-repositories");
    renderRepositories();
  } catch (_) {
    showRepositoryFeedback("Repositories could not be loaded.", true);
  }
}

function showCapabilityFeedback(message, isError = false) {
  const feedback = element("capability-feedback");
  feedback.hidden = false;
  feedback.classList.toggle("error", isError);
  feedback.textContent = message;
}

function capabilityStatusLabel(capability) {
  return capability.status.replaceAll("_", " ");
}

function renderCapability(capability) {
  const card = document.createElement("article");
  card.className = "connection-card";
  const head = document.createElement("button");
  head.className = "connection-head";
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  const title = document.createElement("span"); title.className = "connection-name"; title.textContent = capability.title;
  const identifier = document.createElement("span"); identifier.className = "connection-endpoint"; identifier.textContent = `${capability.id} · v${capability.version}`;
  const badge = document.createElement("span");
  badge.className = `badge ${capability.status === "active" ? "" : "warning"}`;
  badge.textContent = capabilityStatusLabel(capability);
  const chevron = document.createElement("span"); chevron.className = "connection-chevron"; chevron.setAttribute("aria-hidden", "true"); chevron.textContent = "⌄";
  head.append(title, identifier, badge, chevron);
  const detail = document.createElement("div"); detail.className = "connection-detail"; detail.hidden = true;
  head.addEventListener("click", () => {
    const expanded = head.getAttribute("aria-expanded") === "true";
    head.setAttribute("aria-expanded", String(!expanded)); detail.hidden = expanded;
  });
  const facts = document.createElement("dl"); facts.className = "connection-details";
  const addFact = (label, value) => { const row = document.createElement("div"); const dt = document.createElement("dt"); const dd = document.createElement("dd"); dt.textContent = label; dd.textContent = value; row.append(dt, dd); facts.append(row); };
  addFact("Enabled", capability.enabled ? "Yes" : "No");
  addFact("Validation", capability.validation.state.replaceAll("_", " "));
  if (capability.reason_code) addFact("Readiness", capability.reason_code.replaceAll("_", " "));
  if (capability.validation.last_checked_at) addFact("Last checked", new Date(capability.validation.last_checked_at).toLocaleString());
  if (capability.validation.error_code) addFact("Validation result", capability.validation.error_code.replaceAll("_", " "));
  detail.append(facts);
  const result = document.createElement("div"); result.className = "test-result"; result.hidden = true; detail.append(result);
  const actions = document.createElement("div"); actions.className = "connection-actions";
  const validate = document.createElement("button"); validate.className = "button secondary"; validate.type = "button"; validate.textContent = "Check readiness";
  validate.addEventListener("click", async () => {
    validate.disabled = true;
    try {
      const updated = await api(`/api/portal/capabilities/${encodeURIComponent(capability.id)}/validate`, { method: "POST" });
      result.hidden = false; result.classList.toggle("error", updated.status !== "active");
      result.textContent = updated.status === "active" ? "Readiness check passed." : `Readiness check: ${capabilityStatusLabel(updated)}${updated.reason_code ? ` (${updated.reason_code})` : ""}.`;
      await refreshCapabilities();
    } catch (_) { result.hidden = false; result.classList.add("error"); result.textContent = "Capability readiness could not be checked."; }
    finally { validate.disabled = false; }
  });
  const toggle = document.createElement("button"); toggle.className = "button secondary"; toggle.type = "button"; toggle.textContent = capability.enabled ? "Disable" : "Enable";
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      await api(`/api/portal/capabilities/${encodeURIComponent(capability.id)}`, { method: "PATCH", body: JSON.stringify({ enabled: !capability.enabled }) });
      showCapabilityFeedback(`Capability ${capability.enabled ? "disabled" : "enabled"}.`); await refreshCapabilities();
    } catch (_) { showCapabilityFeedback("Capability state could not be updated.", true); }
    finally { toggle.disabled = false; }
  });
  actions.append(validate, toggle); detail.append(actions); card.append(head, detail);
  return card;
}

function renderCapabilities() {
  const list = element("capability-list"); list.replaceChildren();
  if (!state.capabilities.length) {
    const empty = document.createElement("p"); empty.className = "empty-list"; empty.textContent = "No declared capabilities are available from the active context repository."; list.append(empty); return;
  }
  state.capabilities.forEach((capability) => list.append(renderCapability(capability)));
}

async function refreshCapabilities() {
  const refresh = element("refresh-capabilities"); if (refresh) refresh.disabled = true;
  try {
    const response = await api("/api/portal/capabilities"); state.capabilities = response.capabilities; renderCapabilities();
  } catch (_) { showCapabilityFeedback("Capabilities could not be loaded. Check the active context repository.", true); }
  finally { if (refresh) refresh.disabled = false; }
}

function showWorkflowExecutionsFeedback(message, isError = false) {
  const feedback = element("workflow-executions-feedback");
  feedback.hidden = false;
  feedback.classList.toggle("error", isError);
  feedback.textContent = message;
}

function workflowStatusLabel(status) {
  if (status === "accepted") return "Accepted for local continuation";
  if (status === "running") return "Running";
  return String(status || "unknown").replaceAll("_", " ");
}

function workflowStatusClass(execution) {
  return execution.status === "completed" ? "" : "warning";
}

function shortExecutionId(value) {
  return typeof value === "string" && value.length > 12 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function localTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Unknown" : date.toLocaleString();
}

function renderWorkflowExecution(execution) {
  const card = document.createElement("article");
  card.className = "connection-card";
  const head = document.createElement("button");
  head.className = "connection-head execution-head";
  head.type = "button";
  const workflow = document.createElement("span");
  workflow.className = "connection-name";
  workflow.textContent = execution.workflow.id;
  const identity = document.createElement("span");
  identity.className = "connection-endpoint";
  identity.textContent = `${shortExecutionId(execution.execution_id)} · ${execution.workflow.path}`;
  const status = document.createElement("span");
  status.className = `badge ${workflowStatusClass(execution)}`;
  status.textContent = workflowStatusLabel(execution.status);
  const updated = document.createElement("span");
  updated.className = "execution-recency";
  updated.textContent = localTime(execution.updated_at);
  head.append(workflow, identity, status, updated);
  if (execution.active_gate_id) head.title = `Awaiting operator decision: ${execution.active_gate_id}`;
  else if (execution.failure_code) head.title = `Failure code: ${execution.failure_code}`;
  else if (execution.next_step_id) head.title = `Persisted next step: ${execution.next_step_id}`;
  head.addEventListener("click", () => openWorkflowExecution(execution.execution_id));
  card.append(head);
  return card;
}

function renderWorkflowExecutions() {
  const list = element("workflow-execution-list");
  list.replaceChildren();
  if (!state.workflowExecutions.length) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No workflow executions have been recorded yet.";
    list.append(empty);
    return;
  }
  state.workflowExecutions.forEach((execution) => list.append(renderWorkflowExecution(execution)));
}

function showWorkflowExecutionList() {
  element("workflow-executions-list-view").hidden = false;
  element("workflow-execution-detail").hidden = true;
  refreshWorkflowExecutions();
}

async function refreshWorkflowExecutions() {
  const list = element("workflow-execution-list");
  const refresh = element("refresh-workflow-executions");
  if (refresh) refresh.disabled = true;
  list.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "empty-list";
  loading.textContent = "Loading workflow executions…";
  list.append(loading);
  try {
    const response = await api("/api/portal/workflow-executions");
    state.workflowExecutions = response.executions;
    element("workflow-executions-feedback").hidden = true;
    renderWorkflowExecutions();
  } catch (_) {
    state.workflowExecutions = [];
    list.replaceChildren();
    const failed = document.createElement("p");
    failed.className = "empty-list";
    failed.textContent = "Workflow executions could not be loaded.";
    list.append(failed);
    showWorkflowExecutionsFeedback("Workflow executions could not be loaded.", true);
  } finally {
    if (refresh) refresh.disabled = false;
  }
}

function addExecutionFact(facts, label, value) {
  if (value === null || value === undefined || value === "") return;
  const row = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value;
  row.append(term, description);
  facts.append(row);
}

function renderSafeEvidence(evidence) {
  const values = [];
  if (evidence.reason_code) values.push(`Reason: ${evidence.reason_code}`);
  if (typeof evidence.accepted === "boolean") values.push(`Accepted: ${evidence.accepted ? "yes" : "no"}`);
  if (typeof evidence.verified === "boolean") values.push(`Verified: ${evidence.verified ? "yes" : "no"}`);
  if (typeof evidence.awaiting_operator === "boolean") values.push(`Awaiting operator: ${evidence.awaiting_operator ? "yes" : "no"}`);
  if (Array.isArray(evidence.changed_files) && evidence.changed_files.length) values.push(`Changed files: ${evidence.changed_files.join(", ")}`);
  if (evidence.validation) {
    const validation = [];
    if (evidence.validation.provider) validation.push(evidence.validation.provider);
    if (Number.isInteger(evidence.validation.elapsed_ms)) validation.push(`${evidence.validation.elapsed_ms} ms`);
    if (Number.isInteger(evidence.validation.exit_code)) validation.push(`exit ${evidence.validation.exit_code}`);
    if (validation.length) values.push(`Validation: ${validation.join(" · ")}`);
  }
  return values.join("; ");
}

function renderWorkflowExecutionDetail(execution) {
  const detail = element("workflow-execution-detail");
  detail.replaceChildren();
  const heading = document.createElement("div"); heading.className = "execution-detail-heading";
  const headingText = document.createElement("div");
  const eyebrow = document.createElement("p"); eyebrow.className = "eyebrow"; eyebrow.textContent = "Workflow operations";
  const title = document.createElement("h1"); title.textContent = execution.workflow.id;
  const subtitle = document.createElement("p"); subtitle.className = "page-intro"; subtitle.textContent = `Execution ${execution.execution_id}`;
  headingText.append(eyebrow, title, subtitle);
  const back = document.createElement("button"); back.className = "button secondary"; back.type = "button"; back.textContent = "Back to executions";
  back.addEventListener("click", showWorkflowExecutionList);
  heading.append(headingText, back); detail.append(heading);

  const stateSection = document.createElement("section"); stateSection.className = "execution-detail-section";
  const stateTitle = document.createElement("h2"); stateTitle.textContent = "Execution state";
  const facts = document.createElement("dl"); facts.className = "connection-details";
  addExecutionFact(facts, "Status", workflowStatusLabel(execution.status));
  addExecutionFact(facts, "Created", localTime(execution.created_at));
  addExecutionFact(facts, "Last updated", localTime(execution.updated_at));
  addExecutionFact(facts, "Workflow path", execution.workflow.path);
  addExecutionFact(facts, "Workflow version", execution.workflow.version);
  addExecutionFact(facts, "Base revision", execution.base_revision);
  addExecutionFact(facts, "Persisted next step", execution.next_step_id);
  addExecutionFact(facts, "Active operator gate", execution.active_gate_id);
  addExecutionFact(facts, "Latest gate decision", execution.latest_gate_decision);
  addExecutionFact(facts, "Proposal / draft", execution.proposal_id);
  addExecutionFact(facts, "Committed revision", execution.committed_revision);
  if (execution.failure?.code) addExecutionFact(facts, "Failure code", execution.failure.code);
  stateSection.append(stateTitle, facts); detail.append(stateSection);

  if (execution.active_gate_id) {
    const gate = document.createElement("section"); gate.className = "execution-detail-section";
    const gateTitle = document.createElement("h2"); gateTitle.textContent = "Awaiting operator decision";
    const gateCopy = document.createElement("p"); gateCopy.className = "page-intro";
    gateCopy.textContent = execution.next_step_id ? `Gate ${execution.active_gate_id}; persisted continuation step: ${execution.next_step_id}.` : `Gate ${execution.active_gate_id}.`;
    gate.append(gateTitle, gateCopy); detail.append(gate);
  }

  const completed = execution.progression.completed_steps;
  if (completed.length) {
    const steps = document.createElement("section"); steps.className = "execution-detail-section";
    const stepsTitle = document.createElement("h2"); stepsTitle.textContent = "Recorded completed steps";
    const note = document.createElement("p"); note.className = "page-intro"; note.textContent = "Stored step state is not an event timeline.";
    const list = document.createElement("ul"); list.className = "execution-step-list";
    completed.forEach((step) => {
      const item = document.createElement("li"); item.className = "execution-step";
      const stepTitle = document.createElement("div"); stepTitle.className = "execution-step-title"; stepTitle.textContent = `${step.step_id} · ${workflowStatusLabel(step.outcome)}`;
      item.append(stepTitle);
      const safeEvidence = renderSafeEvidence(step.evidence || {});
      if (safeEvidence) { const meta = document.createElement("div"); meta.className = "execution-step-meta"; meta.textContent = safeEvidence; item.append(meta); }
      list.append(item);
    });
    steps.append(stepsTitle, note, list); detail.append(steps);
  }
  detail.hidden = false;
}

async function openWorkflowExecution(executionId) {
  try {
    const execution = await api(`/api/portal/workflow-executions/${encodeURIComponent(executionId)}`);
    element("workflow-executions-list-view").hidden = true;
    renderWorkflowExecutionDetail(execution);
  } catch (_) {
    showWorkflowExecutionsFeedback("Workflow execution detail could not be loaded.", true);
  }
}

function showRepositoryForm(repository = null) {
  state.reconnectingRepositoryId = repository ? repository.id : null;
  const form = element("repository-form");
  form.reset();
  element("repository-form-card").hidden = false;
  const displayLabel = element("repository-display-name").closest("label");
  const locatorLabel = element("repository-locator").closest("label");
  displayLabel.hidden = Boolean(repository);
  locatorLabel.hidden = Boolean(repository);
  element("repository-form-title").textContent = repository ? "Reconnect repository" : "Register MABEL context repository";
  element("save-repository").textContent = repository ? "Verify and replace token" : "Verify and register";
  if (repository) element("repository-github-token").focus();
  else element("repository-display-name").focus();
}

function hideRepositoryForm() {
  element("repository-form-card").hidden = true;
  state.reconnectingRepositoryId = null;
}

async function saveRepository(event) {
  event.preventDefault();
  const saveButton = element("save-repository");
  const token = element("repository-github-token").value.trim();
  saveButton.disabled = true;
  try {
    if (state.reconnectingRepositoryId) {
      await api(`/api/managed-repositories/${state.reconnectingRepositoryId}/reconnect`, {
        method: "POST", body: JSON.stringify({ github_token: token }),
      });
      showRepositoryFeedback("Repository reconnected.");
    } else {
      await api("/api/managed-repositories", {
        method: "POST",
        body: JSON.stringify({
          display_name: element("repository-display-name").value.trim(),
          provider: "github", purpose: "mabel_context_repository",
          repository: element("repository-locator").value.trim(), github_token: token,
        }),
      });
      showRepositoryFeedback("Repository verified and registered.");
    }
    hideRepositoryForm();
    await refreshRepositories();
  } catch (_) {
    showRepositoryFeedback("Repository could not be verified or saved.", true);
  } finally {
    saveButton.disabled = false;
  }
}

function showConnectionForm(connection = null) {
  state.editingId = connection ? connection.id : null;
  const formCard = element("connection-form-card");
  const form = element("connection-form");
  form.reset();
  formCard.hidden = false;
  element("connection-form-title").textContent = connection ? "Edit connection" : "Add connection";
  element("connection-form-hint").textContent = connection
    ? "Leave the AMIE API token blank to keep its current value."
    : "The current transport is AMIE over Streamable HTTP.";
  element("connection-display-name").value = connection ? connection.display_name : "";
  element("connection-endpoint").value = connection ? connection.endpoint : "";
  element("clear-amie-api-token-wrap").hidden = !connection || !connection.credential_configured;
  element("credential-note").textContent = connection && connection.credential_configured
    ? "An AMIE API token is encrypted in MABEL. It is never returned to the portal."
    : "Generate this token in AMIE Settings → API access. MABEL encrypts it and uses it only for AMIE MCP requests.";
  element("save-connection").textContent = connection ? "Save changes" : "Save connection";
  element("connection-display-name").focus();
}

function showEditForm(connection) {
  showConnectionForm(connection);
  element("connection-form-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function hideConnectionForm() {
  element("connection-form-card").hidden = true;
  state.editingId = null;
}

async function saveConnection(event) {
  event.preventDefault();
  const saveButton = element("save-connection");
  const displayName = element("connection-display-name").value.trim();
  const endpoint = element("connection-endpoint").value.trim();
  const amieApiToken = element("connection-amie-api-token").value.trim();
  const clearAmieApiToken = element("clear-amie-api-token").checked;
  saveButton.disabled = true;
  try {
    if (state.editingId) {
      const changes = { display_name: displayName, endpoint };
      if (amieApiToken) changes.amie_api_token = amieApiToken;
      if (clearAmieApiToken) changes.clear_amie_api_token = true;
      await api(`/api/connections/${state.editingId}`, { method: "PATCH", body: JSON.stringify(changes) });
      showFeedback("Connection updated.");
    } else {
      await api("/api/connections", {
        method: "POST",
        body: JSON.stringify({
          display_name: displayName,
          connection_type: "amie",
          transport: "mcp_streamable_http",
          endpoint,
          ...(amieApiToken ? { amie_api_token: amieApiToken } : {}),
        }),
      });
      showFeedback("Connection added.");
    }
    hideConnectionForm();
    await refreshConnections();
  } catch (_) {
    showFeedback("Connection could not be saved. Check the fields and try again.", true);
  } finally {
    saveButton.disabled = false;
  }
}

function initialize() {
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.viewTarget));
  });
  element("refresh-status").addEventListener("click", refreshStatus);
  element("show-add-connection").addEventListener("click", () => showConnectionForm());
  element("cancel-connection-form").addEventListener("click", hideConnectionForm);
  element("cancel-connection-form-secondary").addEventListener("click", hideConnectionForm);
  element("connection-form").addEventListener("submit", saveConnection);
  element("show-add-repository").addEventListener("click", () => showRepositoryForm());
  element("cancel-repository-form").addEventListener("click", hideRepositoryForm);
  element("cancel-repository-form-secondary").addEventListener("click", hideRepositoryForm);
  element("repository-form").addEventListener("submit", saveRepository);
  element("generate-api-token").addEventListener("click", generateApiToken);
  element("refresh-capabilities").addEventListener("click", refreshCapabilities);
  element("refresh-workflow-executions")?.addEventListener("click", refreshWorkflowExecutions);
  api("/api/portal/session").then((session) => {
    element("portal-logout").hidden = false;
  }).catch(() => {});
  element("portal-logout").addEventListener("click", async () => {
    try {
      await api("/auth/logout", { method: "POST" });
      window.location.assign("/portal");
    } catch (_) { announce("MABEL could not complete logout."); }
  });
  setView("workspace");
  refreshStatus();
}

window.mabelPortal = { api, announce, setView };
document.addEventListener("DOMContentLoaded", initialize);
