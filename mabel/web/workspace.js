"use strict";

(() => {
  const state = {
    snapshot: null, tree: [], selectedCapability: null, editor: null, expandedTreePaths: new Set(),
  };
  const element = (id) => document.getElementById(id);
  const api = (...args) => window.mabelPortal.api(...args);

  function toast(message, error = false) {
    const target = element("workspace-toast");
    target.textContent = message;
    target.classList.toggle("error", error);
    target.hidden = false;
    window.clearTimeout(toast.timeout);
    toast.timeout = window.setTimeout(() => { target.hidden = true; }, 5000);
    window.mabelPortal.announce(message);
  }

  function setBusy(busy) {
    element("workspace-layout").setAttribute("aria-busy", String(busy));
    element("refresh-workspace").disabled = busy;
  }

  function shortCommit(commit) { return commit ? commit.slice(0, 12) : "No projection"; }

  function configureEditor() {
    if (state.editor || !window.ace) return;
    state.editor = window.ace.edit("workspace-editor");
    state.editor.setTheme("ace/theme/tomorrow_night");
    state.editor.setReadOnly(true);
    state.editor.setOptions({ fontSize: "13px", showPrintMargin: false, wrap: true, useWorker: false });
  }

  function showFile(file) {
    element("workspace-file-title").textContent = file.path;
    element("workspace-file-meta").textContent = `${file.language} · ${shortCommit(file.repository_commit)} · editing is not enabled`;
    configureEditor();
    if (state.editor) {
      state.editor.session.setMode(`ace/mode/${file.language}`);
      state.editor.setValue(file.content, -1);
      state.editor.clearSelection();
      state.editor.focus();
    } else element("workspace-editor").textContent = file.content;
  }

  function treeMatches(node, filter) {
    const searchable = [node.name, node.title, node.id, node.content_type].filter(Boolean).join(" ").toLowerCase();
    return !filter || searchable.includes(filter) || (node.children && node.children.some((child) => treeMatches(child, filter)));
  }

  function treeLabel(node, className = "tree-name") {
    const label = document.createElement("span");
    label.className = className;
    label.textContent = node.name;
    return label;
  }

  function treeMetadata(node) {
    const values = [node.title, node.content_type].filter(Boolean);
    if (!values.length) return null;
    const metadata = document.createElement("span");
    metadata.className = "tree-type";
    metadata.textContent = values.join(" · ");
    return metadata;
  }

  function revealTreePath(path) {
    const parts = path.split("/");
    let ancestor = "";
    for (const part of parts.slice(0, -1)) {
      ancestor = ancestor ? `${ancestor}/${part}` : part;
      state.expandedTreePaths.add(ancestor);
    }
  }

  function renderTreeList(nodes, filter, nested = false) {
    const list = document.createElement("ul");
    list.className = "workspace-tree-list";
    list.setAttribute("role", nested ? "group" : "tree");
    for (const node of nodes.filter((item) => treeMatches(item, filter))) {
      const item = document.createElement("li");
      item.setAttribute("role", "treeitem");
      if (node.type === "directory") {
        const expanded = Boolean(filter) || state.expandedTreePaths.has(node.path);
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "tree-directory";
        toggle.setAttribute("aria-expanded", String(expanded));
        toggle.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${node.name}`);
        const chevron = document.createElement("span");
        chevron.className = "tree-chevron";
        chevron.setAttribute("aria-hidden", "true");
        chevron.textContent = "›";
        toggle.append(chevron, treeLabel(node));
        const metadata = treeMetadata(node);
        if (metadata) toggle.append(metadata);
        const children = renderTreeList(node.children, filter, true);
        children.hidden = !expanded;
        toggle.addEventListener("click", () => {
          if (state.expandedTreePaths.has(node.path)) state.expandedTreePaths.delete(node.path);
          else state.expandedTreePaths.add(node.path);
          renderTree();
        });
        item.append(toggle, children);
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tree-file";
        button.append(treeLabel(node));
        const metadata = treeMetadata(node);
        if (metadata) button.append(metadata);
        button.title = node.path;
        button.addEventListener("click", () => openFile(node.path));
        item.append(button);
      }
      list.append(item);
    }
    return list;
  }

  function renderTree() {
    const filter = element("workspace-filter").value.trim().toLowerCase();
    const container = element("workspace-tree");
    container.replaceChildren();
    const list = renderTreeList(state.tree, filter);
    if (!list.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "workspace-muted";
      empty.textContent = "No declared files match this filter.";
      container.append(empty);
    } else container.append(list);
  }

  async function openFile(path) {
    if (!state.snapshot) return;
    try {
      const commit = state.snapshot.projection.commit_sha;
      const file = await api(`/api/portal/workspace/file?commit=${encodeURIComponent(commit)}&path=${encodeURIComponent(path)}`);
      revealTreePath(path);
      renderTree();
      showFile(file);
      const match = /^capabilities\/([^/]+)\//.exec(path);
      if (match) await selectCapability(match[1]);
    } catch (_) { toast("The declared file could not be loaded from this projection.", true); }
  }

  function definitionLine(label, value) {
    const line = document.createElement("div");
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = value;
    line.append(term, detail);
    return line;
  }

  async function selectCapability(capabilityId) {
    if (!state.snapshot) return;
    try {
      const commit = state.snapshot.projection.commit_sha;
      const detail = await api(`/api/portal/workspace/capabilities/${encodeURIComponent(capabilityId)}?commit=${encodeURIComponent(commit)}`);
      state.selectedCapability = detail;
      const definitions = document.createElement("dl");
      definitions.className = "workspace-definition-list";
      definitions.append(
        definitionLine("ID", detail.id), definitionLine("Version", detail.version),
        definitionLine("Runtime", detail.runtime), definitionLine("Executor", detail.executor.path),
        definitionLine("Tests", detail.tests.declared ? detail.tests.path : "Not declared"),
        definitionLine("Commit", shortCommit(detail.repository_commit)),
      );
      element("capability-detail").replaceChildren(definitions);
      element("capability-status").textContent = detail.status;
      element("capability-status").className = "badge";
      element("run-capability").disabled = false;
    } catch (_) { toast("Capability details could not be loaded from this projection.", true); }
  }

  function renderReport(report) {
    element("console-status").textContent = report.status;
    element("console-status").className = `badge ${report.status === "succeeded" ? "" : "warning"}`;
    const lines = [`Capability: ${report.capability_id}`, `Commit: ${report.repository_commit}`, `Status: ${report.status}`, `Elapsed: ${report.elapsed_ms} ms`];
    if (report.result !== undefined) lines.push("", "Structured result:", JSON.stringify(report.result, null, 2));
    if (report.stdout) lines.push("", "Bounded stdout:", report.stdout);
    if (report.stderr) lines.push("", "Bounded stderr:", report.stderr);
    if (report.error) lines.push("", `Error: ${report.error}`);
    element("workspace-console").textContent = lines.join("\n");
  }

  async function runCapability() {
    if (!state.selectedCapability) return;
    const button = element("run-capability");
    button.disabled = true;
    element("console-status").textContent = "Running";
    try {
      const report = await api(`/api/portal/workspace/capabilities/${encodeURIComponent(state.selectedCapability.id)}/run`, { method: "POST" });
      renderReport(report);
      toast(`Capability ${report.capability_id} ${report.status}.`, report.status !== "succeeded");
    } catch (_) {
      element("console-status").textContent = "Failed";
      toast("Capability execution could not be started.", true);
    } finally { button.disabled = false; }
  }

  async function loadWorkspace() {
    setBusy(true);
    try {
      const snapshot = await api("/api/portal/workspace");
      state.snapshot = snapshot;
      element("workspace-repository-name").textContent = `${snapshot.repository.display_name} · ${snapshot.repository.canonical_locator}`;
      element("workspace-commit").textContent = shortCommit(snapshot.projection.commit_sha);
      element("workspace-commit").title = snapshot.projection.commit_sha;
      element("workspace-projection-status").textContent = snapshot.projection.status;
      element("workspace-sync-time").textContent = `Projection synchronized ${new Date(snapshot.projection.synced_at).toLocaleString()} · ${snapshot.projection.read_only ? "read-only" : "writable"}`;
      const tree = await api(`/api/portal/workspace/tree?commit=${encodeURIComponent(snapshot.projection.commit_sha)}`);
      state.tree = tree.tree;
      state.expandedTreePaths = new Set();
      renderTree();
      if (snapshot.capabilities.length) await selectCapability(snapshot.capabilities[0].id);
      toast("Workspace projection loaded.");
    } catch (_) {
      toast("The active MABEL context repository could not be loaded. Register and enable one context repository first.", true);
    } finally { setBusy(false); }
  }

  element("workspace-filter").addEventListener("input", () => renderTree());
  element("workspace-filter").addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.currentTarget.value = ""; renderTree(); event.currentTarget.blur(); }
  });
  element("refresh-workspace").addEventListener("click", loadWorkspace);
  element("run-capability").addEventListener("click", runCapability);
  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault(); element("workspace-filter").focus();
    }
  });
  window.addEventListener("mabel:viewchange", (event) => {
    if (event.detail.view === "workspace" && !state.snapshot) loadWorkspace();
  });
})();
