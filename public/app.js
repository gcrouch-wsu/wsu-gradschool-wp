(() => {
  "use strict";
  const qs = (s, r = document) => r.querySelector(s);
  const qsa = (s, r = document) => [...r.querySelectorAll(s)];
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const labels = {
    unreferenced: "Unreferenced", "unreferenced-media": "Unreferenced media",
    disconnected: "Disconnected island", "needs-verification": "Needs manual check",
    "expected-development": "Development author", "non-public": "Non-public", linked: "Linked",
    publish: "Published", inherit: "Inherited", draft: "Draft", private: "Private", pending: "Pending",
    unreviewed: "Not reviewed", keep: "Keep", expected: "Keep", verify: "Review later",
    candidate: "Review later", approved: "Approved to delete",
    unchecked: "Not checked", found: "Found live", trashed: "In trash", missing: "Missing live",
    "not-in-rest": "Not in REST", error: "Live check error",
  };
  const humanize = (value) => labels[value] || String(value || "Unknown")
    .replaceAll("_", " ").replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  const formatBytes = (bytes) => {
    const size = Number(bytes);
    if (!Number.isFinite(size) || size <= 0) return "Unknown size";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
    return `${(size / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
  };
  const safeLink = (url) => {
    try { const parsed = new URL(url); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : ""; }
    catch { return ""; }
  };
  function trashReason(item) {
    if (!writeEnabled) return "WordPress Trash is not enabled for this environment.";
    if (item.live_checking) return "Wait for the fresh WordPress check to finish.";
    if (item.can_trash) return "";
    if (item.live_state === "unchecked") return "Wait for the live WordPress check, then choose Approve to delete.";
    if (item.live_state === "missing") return "This record was not found on live WordPress.";
    if (item.live_state === "trashed") return "Already in WordPress Trash.";
    if (item.live_state === "not-in-rest") return "This content type cannot be trashed through WordPress REST.";
    if (item.live_state === "error") return "Live check failed, so Trash is blocked.";
    if (item.live_state === "found" && !item.live_snapshot_ready) return "WordPress did not return a complete version snapshot, so Trash is blocked.";
    if (item.live_snapshot_ready && !item.live_identity_matches) return "The live title or type differs from the export. Review a current export before Trash.";
    if (item.review_decision !== "approved") return "Choose Approve to delete first.";
    return "Trash is not available for this record.";
  }
  function trashSelectionReason(item) {
    if (!writeEnabled) return "WordPress Trash is not enabled for this environment.";
    if (item.live_state === "trashed") return "Already in WordPress Trash.";
    if (!item.selectable_for_trash) return "This content type cannot be moved to Trash through WordPress REST.";
    return "";
  }
  function makeTrashButton(item, className) {
    const button = el("button", className || "button button-danger button-compact", "Move to Trash");
    button.type = "button";
    const blocked = trashReason(item);
    if (blocked) {
      button.disabled = true;
      button.title = blocked;
      return button;
    }
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (qs("#detail-dialog")?.open && review.item && String(review.item.id) === String(item.id)) {
        trashCurrentRecord(button);
      } else {
        trashRecords([item.id], [item.title || item.file_name || `ID ${item.id}`]).catch((error) => {
          window.alert(error.message || "WordPress Trash failed.");
        });
      }
    });
    return button;
  }

  function wireUpload(inputSelector, labelSelector) {
    const input = qs(inputSelector), label = qs(labelSelector), drop = input?.closest(".file-drop");
    if (!input || !label || !drop) return;
    const show = (file) => { if (file) label.textContent = `${file.name} · ${formatBytes(file.size)}`; };
    input.addEventListener("change", () => show(input.files[0]));
    ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
      event.preventDefault(); drop.classList.add("is-dragging");
    }));
    ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
      event.preventDefault(); drop.classList.remove("is-dragging");
    }));
    drop.addEventListener("drop", (event) => {
      const file = event.dataTransfer?.files?.[0];
      if (!file) return;
      const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files; show(file);
    });
  }
  wireUpload("#file-input", "#file-label");
  wireUpload("#replacement-file", "#replacement-label");
  const runtime = JSON.parse(qs("#runtime-data")?.textContent || "{}");
  const chunkSize = Number(runtime.chunk_size) || (3 * 1024 * 1024);
  const restEnabled = Boolean(runtime.rest_enabled);
  const writeEnabled = Boolean(runtime.rest_write_enabled);
  const csrfHeaders = (headers = {}) => ({ ...headers, "X-CSRF-Token": runtime.write_csrf || "" });
  const review = { ids: [], index: 0, total: 0, open: false, item: null, seq: 0, filterKey: "" };

  function uploadError(form, message) {
    form.parentElement?.querySelector(".upload-runtime-error")?.remove();
    const alert = el("div", "alert alert-error upload-runtime-error");
    alert.setAttribute("role", "alert");
    alert.append(el("strong", "", "Analysis stopped"), el("span", "", message));
    form.before(alert);
  }

  async function responseJson(response) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || `The server returned HTTP ${response.status}.`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  const uploadUi = {
    steps: (name) => qsa("#loading-steps li").forEach((item) => {
      const order = ["upload", "analyze", "open"], mine = order.indexOf(item.dataset.step), current = order.indexOf(name);
      item.classList.toggle("is-done", mine < current); item.classList.toggle("is-active", mine === current);
    }),
    timer: null,
    startClock(started) {
      const node = qs("#loading-elapsed"); clearInterval(this.timer);
      this.timer = setInterval(() => {
        const seconds = Math.round((Date.now() - started) / 1000);
        let note = "";
        if (seconds >= 150) note = " · Large exports can take several minutes; stay on this page.";
        else if (seconds >= 45) note = " · Still working. Large exports take a while to analyze.";
        node.textContent = `${seconds}s elapsed${note}`;
      }, 1000);
    },
    stopClock() { clearInterval(this.timer); this.timer = null; },
  };

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function recoverAnalysis(detail, deadlineMs) {
    // The analysis response can be lost after the report was stored
    // (a proxy timeout on a large export). Poll until it shows up.
    const started = Date.now();
    while (Date.now() - started < deadlineMs) {
      detail.textContent = "The analysis response was interrupted. Checking whether the export finished analyzing…";
      await wait(5000);
      try {
        const status = await responseJson(await fetch("/api/upload-status", { method: "POST", headers: csrfHeaders() }));
        if (status.analyzed) return status;
        if (!status.pending) return null;
      } catch (error) { /* keep polling until the deadline */ }
    }
    return null;
  }

  async function uploadInChunks(form) {
    const input = qs('input[type="file"]', form), file = input?.files?.[0];
    if (!file) return;
    const loading = qs("#loading"), progress = qs("#upload-progress");
    const label = qs("#loading-label"), detail = qs("#loading-detail");
    const started = Date.now();
    const megabytes = (file.size / (1024 * 1024)).toFixed(1);
    loading.querySelector(".button")?.remove();
    loading.hidden = false; progress.value = 0; label.textContent = "Uploading the WordPress export";
    uploadUi.steps("upload"); uploadUi.startClock(started);
    try {
      const totalChunks = Math.ceil(file.size / chunkSize);
      await responseJson(await fetch("/api/upload-session", {
        method: "POST", headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ filename: file.name, size: file.size, total_chunks: totalChunks }),
      }));
      for (let index = 0; index < totalChunks; index += 1) {
        const start = index * chunkSize, end = Math.min(start + chunkSize, file.size);
        const sent = (end / (1024 * 1024)).toFixed(1);
        detail.textContent = `Securely uploading part ${index + 1} of ${totalChunks} (${sent} of ${megabytes} MB)…`;
        await responseJson(await fetch(`/api/upload-chunk/${index}`, {
          method: "POST", headers: csrfHeaders({ "Content-Type": "application/octet-stream" }), body: file.slice(start, end),
        }));
        progress.value = Math.round(((index + 1) / totalChunks) * 60);
      }
      label.textContent = "Analyzing the WordPress export";
      detail.textContent = `Upload complete (${megabytes} MB). Building the inventory, taxonomies, and reference graph — this is one long step with no partial progress.`;
      progress.removeAttribute("value"); uploadUi.steps("analyze");
      let completed;
      try {
        completed = await responseJson(await fetch("/api/complete-upload", { method: "POST", headers: csrfHeaders() }));
      } catch (error) {
        // A 4xx is a definite server verdict; a network failure or gateway
        // timeout may mean the report was stored but the response was lost.
        if (error.status && error.status < 500) throw error;
        completed = await recoverAnalysis(detail, 4 * 60 * 1000);
        if (!completed) throw new Error(`${error.message} The export did not finish analyzing. Choose the file again to retry.`);
      }
      progress.value = 100; uploadUi.steps("open"); uploadUi.stopClock();
      const seconds = completed.analysis_seconds ? ` in ${completed.analysis_seconds}s` : "";
      label.textContent = "Export analyzed";
      detail.textContent = `${Number(completed.records || 0).toLocaleString()} records analyzed${seconds}. Live WordPress states start as “Not checked” for a new export. Opening the dashboard…`;
      window.location.assign(completed.redirect || "/");
    } catch (error) {
      uploadUi.stopClock();
      loading.hidden = true;
      uploadError(form, error.message || "The export could not be uploaded.");
    }
  }

  qsa(".upload-form").forEach((form) => form.addEventListener("submit", (event) => {
    if (!form.checkValidity()) return;
    event.preventDefault();
    uploadInChunks(form);
  }));

  const dataNode = qs("#dashboard-data");
  if (!dataNode) return;
  const dashboard = JSON.parse(dataNode.textContent || "{}"), groups = dashboard.groups || [];
  const taxonomyLabels = dashboard.taxonomy_labels || {};
  if (!groups.length) return;
  const controls = {
    search: qs("#search-filter"), classification: qs("#classification-filter"), status: qs("#status-filter"),
    author: qs("#author-filter"), subtype: qs("#subtype-filter"), fileType: qs("#file-type-filter"), decision: qs("#decision-filter"),
    live: qs("#live-filter"), sort: qs("#sort-select"), pageSize: qs("#page-size"),
  };
  const state = { group: groups[0].id, page: 1, taxonomy: "", term: "", queue: false, requestNumber: 0 };
  const selected = new Map();
  let searchTimer;

  const activeGroup = () => groups.find((group) => group.id === state.group) || groups[0];
  function queryParams(includePage = true) {
    const params = new URLSearchParams();
    if (state.queue) params.set("queue", "1");
    else params.set("group", state.group);
    if (includePage) { params.set("page", state.page); params.set("page_size", controls.pageSize.value); }
    const values = { q: controls.search.value.trim(), classification: controls.classification.value,
      status: controls.status.value, author: controls.author.value, subtype: controls.subtype.value, file_type: controls.fileType.value,
      decision: controls.decision.value, live: controls.live?.value || "", sort: controls.sort.value, taxonomy: state.taxonomy, term: state.term };
    Object.entries(values).forEach(([key, value]) => { if (value) params.set(key, value); });
    return params;
  }
  function setOptions(select, values, defaultLabel, formatter = humanize) {
    const old = select.value; select.replaceChildren();
    const first = el("option", "", defaultLabel); first.value = ""; select.append(first);
    values.forEach((value) => { const option = el("option", "", formatter(value)); option.value = value; select.append(option); });
    if (values.includes(old)) select.value = old;
  }
  const fileTypeLabel = (value) => value === "<none>" ? "Unknown / no extension" : value.replace(/^\./, "").toUpperCase();
  function summaryItem(label, value, note) {
    const card = el("div", "summary-item"); card.append(el("span", "", label), el("strong", "", Number(value).toLocaleString()));
    if (note) card.append(el("small", "", note)); return card;
  }
  function renderGroupHeader() {
    const strip = qs("#summary-strip");
    if (state.queue) {
      qs("#active-group-kicker").textContent = "Cross-group queue";
      qs("#active-group-title").textContent = "Work queue";
      qs("#active-group-description").textContent = "Unreferenced records plus items that are missing from live WordPress or already in Trash.";
      qs("#taxonomy-tab-count").textContent = "";
      strip.replaceChildren(summaryItem("In queue", 0, "Needs attention"));
      qs(".group-note")?.remove();
      return;
    }
    const group = activeGroup();
    qs("#active-group-kicker").textContent = "Selected group";
    qs("#active-group-title").textContent = group.label;
    qs("#active-group-description").textContent = group.description;
    if (group.related_records) qs("#active-group-description").textContent += " Related records: " +
      Object.entries(group.related_records).map(([name, count]) => `${count.toLocaleString()} ${name.toLowerCase()}`).join(" · ") + ".";
    qs("#taxonomy-tab-count").textContent = group.taxonomy_count ? String(group.taxonomy_count) : "";
    strip.replaceChildren(summaryItem("Total", group.count, "Exported records"), summaryItem("Public", group.public, "Published or inherited"),
      summaryItem("Review", group.review_candidates, "Needs attention"), summaryItem("Dev author", group.expected_development, "Configured author match"),
      summaryItem("Linked", group.linked, "Evidence found"), summaryItem("Taxonomies", group.taxonomy_count, "Typed groupings"));
    qs(".group-note")?.remove();
    if (group.note) { const note = el("aside", "group-note"); note.append(el("strong", "", "Export limitation"), el("span", "", group.note)); strip.after(note); }
  }
  function showPanel(panelId) {
    qsa(".workspace-tab").forEach((tab) => {
      const isContent = tab.dataset.panel === "content-panel";
      const isQueueTab = tab.dataset.queue === "1";
      const active = tab.dataset.panel === panelId && (!isContent || isQueueTab === state.queue);
      tab.classList.toggle("is-active", active);
    });
    qsa(".workspace-panel").forEach((panel) => { const active = panel.id === panelId; panel.classList.toggle("is-active", active); panel.hidden = !active; });
  }
  function updateSelectionUI() {
    const countNode = qs("#selection-count");
    const button = qs("#bulk-trash");
    const selectPage = qs("#select-page");
    if (countNode) countNode.textContent = `${selected.size} selected`;
    if (button) button.disabled = selected.size === 0;
    const boxes = qsa(".row-select");
    if (selectPage && boxes.length) {
      const enabled = boxes.filter((box) => !box.disabled);
      selectPage.checked = enabled.length > 0 && enabled.every((box) => box.checked);
      selectPage.indeterminate = !selectPage.checked && enabled.some((box) => box.checked);
    } else if (selectPage) {
      selectPage.checked = false;
      selectPage.indeterminate = false;
    }
  }
  function clearSelection() {
    selected.clear();
    updateSelectionUI();
  }
  async function loadQueueCount() {
    const node = qs("#queue-tab-count");
    if (!node) return;
    try {
      const payload = await responseJson(await fetch("/api/items?queue=1&page_size=1"));
      node.textContent = payload.total ? String(payload.total) : "";
    } catch {
      node.textContent = "";
    }
  }
  async function trashRecords(ids, titles, options = {}) {
    if (!ids.length) return false;
    if (ids.length > 25) {
      window.alert("Move at most 25 records to Trash at a time.");
      return false;
    }
    if (!options.skipConfirm) {
      const preview = titles.slice(0, 8).join("\n");
      const extra = titles.length > 8 ? `\n…and ${titles.length - 8} more` : "";
      const site = dashboard.site?.site_url || dashboard.site?.home_url || "the configured WordPress site";
      const confirmed = window.confirm(
        `Move ${ids.length} record${ids.length === 1 ? "" : "s"} to Trash in live WordPress?\n\nSite: ${site}\nThis can be undone in WordPress Trash. It is not a permanent delete.\n\n${preview}${extra}`,
      );
      if (!confirmed) return false;
    }
    const payload = await responseJson(await fetch("/api/trash", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        ids,
        confirm: "trash",
        csrf: runtime.write_csrf || "",
        preflight_token: options.preflightToken || "",
      }),
    }));
    payload.results.filter((result) => result.ok).forEach((result) => selected.delete(result.id));
    const failed = payload.results.filter((result) => !result.ok);
    if (failed.length) {
      window.alert(`Moved ${payload.trashed} to Trash.\n${failed.length} failed:\n${failed.map((result) => `${result.title}: ${result.error}`).join("\n")}`);
    } else if (!options.quiet) {
      window.alert(`Moved ${payload.trashed} record${payload.trashed === 1 ? "" : "s"} to WordPress Trash.`);
    }
    if (!options.skipReload) {
      updateSelectionUI();
      await loadItems();
      loadQueueCount();
    }
    return failed.length === 0 && payload.trashed === ids.length;
  }
  async function reviewBulkTrash(items) {
    if (!items.length) return false;
    if (items.length > 25) {
      window.alert("Review at most 25 records for Trash at a time.");
      return false;
    }
    const preflight = await responseJson(await fetch("/api/trash/preflight", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ ids: items.map((item) => item.id), csrf: runtime.write_csrf || "" }),
    }));
    const ready = preflight.ready || [];
    const blocked = preflight.blocked || [];
    const blockedPreview = blocked.slice(0, 8).map((item) => `${item.title}: ${item.reason}`).join("\n");
    const blockedExtra = blocked.length > 8 ? `\n…and ${blocked.length - 8} more blocked` : "";
    if (!ready.length) {
      window.alert(`No selected records are ready for Trash.${blocked.length ? `\n\n${blockedPreview}${blockedExtra}` : ""}`);
      return false;
    }
    const readyPreview = ready.slice(0, 8).map((item) => item.title).join("\n");
    const readyExtra = ready.length > 8 ? `\n…and ${ready.length - 8} more ready` : "";
    const site = preflight.site_url || dashboard.site?.site_url || dashboard.site?.home_url || "the configured WordPress site";
    const confirmed = window.confirm(
      `Fresh WordPress check complete.\n\nReady to move: ${ready.length}\nBlocked: ${blocked.length}\n\nSite: ${site}\nThis can be undone in WordPress Trash. It is not a permanent delete.\n\nReady:\n${readyPreview}${readyExtra}${blocked.length ? `\n\nBlocked:\n${blockedPreview}${blockedExtra}` : ""}\n\nMove the ready records to Trash?`,
    );
    if (!confirmed) return false;
    const completed = await trashRecords(
      ready.map((item) => item.id),
      ready.map((item) => item.title),
      { preflightToken: preflight.preflight_token, quiet: true, skipConfirm: true },
    );
    if (completed && blocked.length) {
      window.alert(`Moved ${ready.length} record${ready.length === 1 ? "" : "s"} to Trash.\n${blocked.length} blocked record${blocked.length === 1 ? " remains" : "s remain"} selected for review.`);
    } else if (completed) {
      window.alert(`Moved ${ready.length} record${ready.length === 1 ? "" : "s"} to WordPress Trash.`);
    }
    return completed;
  }
  const badge = (value) => el("span", `badge badge-${value}`, humanize(value));
  const meta = (text) => el("small", "content-meta", text || "—");
  const dateOnly = (value) => value ? String(value).slice(0, 10) : "—";
  function taxonomySummary(item) {
    const entries = Object.entries(item.taxonomy_terms || {});
    if (!entries.length) return meta("No taxonomy assignments");
    const wrapper = el("div", "taxonomy-summary");
    entries.slice(0, 3).forEach(([key, values]) => {
      const row = el("span", "taxonomy-line");
      row.append(el("b", "", taxonomyLabels[key] || humanize(key)), document.createTextNode(` ${values.slice(0, 2).join(", ")}${values.length > 2 ? ` +${values.length - 2}` : ""}`)); wrapper.append(row);
    });
    if (entries.length > 3) wrapper.append(meta(`+${entries.length - 3} more taxonomies`));
    return wrapper;
  }
  function reviewSelect(item) {
    const select = el("select", "review-select"); select.setAttribute("aria-label", `Review decision for ${item.title || "untitled item"}`);
    ["unreviewed", "keep", "verify", "approved"].forEach((value) => {
      const option = el("option", "", humanize(value)); option.value = value; option.selected = item.review_decision === value; select.append(option);
    });
    select.dataset.state = item.review_decision;
    select.addEventListener("change", async () => {
      const prior = select.dataset.state; select.disabled = true;
      try {
        const response = await fetch(`/api/items/${encodeURIComponent(item.id)}/decision`, { method: "POST", headers: csrfHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ decision: select.value }) });
        if (!response.ok) throw new Error("The review decision could not be saved.");
        select.dataset.state = select.value; select.classList.add("is-saved"); setTimeout(() => select.classList.remove("is-saved"), 900);
        await loadItems();
        loadQueueCount();
      } catch (error) { select.value = prior; window.alert(error.message); }
      finally { select.disabled = false; }
    });
    return select;
  }
  function renderRows(items) {
    const tbody = qs("#results-body"); tbody.replaceChildren();
    items.forEach((item) => {
      const row = document.createElement("tr"), finding = document.createElement("td"), live = document.createElement("td"), content = document.createElement("td"),
        author = document.createElement("td"), taxonomy = document.createElement("td"), published = document.createElement("td"),
        updated = document.createElement("td"), review = document.createElement("td"), actions = document.createElement("td");
      finding.append(badge(item.classification), meta(`${humanize(item.confidence)} confidence`));
      if (item.expected_development) finding.append(badge("expected-development"));
      live.append(el("span", `badge badge-live-${item.live_state || "unchecked"}`, humanize(item.live_state || "unchecked")));
      if (item.live_status) live.append(meta(humanize(item.live_status)));
      if (item.live_checked_at) live.append(meta(`Checked ${item.live_checked_at}`));
      const title = el("button", "content-title", item.title || item.file_name || "Untitled"); title.type = "button"; title.addEventListener("click", () => openDetails(item.id));
      content.append(title, meta(`${humanize(item.content_class)} · ${humanize(item.status)} · ID ${item.id}`));
      if (item.group === "media") content.append(meta(`${item.mime_type || "Unknown format"} · ${formatBytes(item.file_size)}${item.width && item.height ? ` · ${item.width} × ${item.height}` : ""}`));
      author.textContent = item.author_name || item.author_login || "Unknown";
      if (item.author_name && item.author_login && item.author_name !== item.author_login) author.append(meta(item.author_login));
      taxonomy.append(taxonomySummary(item)); published.textContent = dateOnly(item.created); updated.textContent = dateOnly(item.modified); review.append(reviewSelect(item));
      const button = el("button", "button button-detail", "Review"); button.type = "button"; button.addEventListener("click", () => openDetails(item.id)); actions.append(button);
      actions.className = "actions-cell";
      if (writeEnabled) {
        const select = document.createElement("td");
        select.className = "select-col";
        const box = el("input", "row-select");
        box.type = "checkbox";
        box.dataset.id = item.id;
        box.dataset.title = item.title || item.file_name || `ID ${item.id}`;
        if (selected.has(item.id) && !item.selectable_for_trash) selected.delete(item.id);
        box.checked = selected.has(item.id);
        box.disabled = !item.selectable_for_trash;
        box.title = box.disabled ? trashSelectionReason(item) : "Select this record for bulk Trash review";
        box.setAttribute("aria-label", `Select ${item.title || item.file_name || item.id}`);
        box.addEventListener("change", () => {
          if (box.checked) selected.set(item.id, item);
          else selected.delete(item.id);
          updateSelectionUI();
        });
        select.append(box);
        row.append(select);
      }
      row.append(finding, live, content, author, taxonomy, published, updated, review, actions); tbody.append(row);
    });
    updateSelectionUI();
  }
  async function loadItems(resetFacets = false) {
    const number = ++state.requestNumber; qs("#result-count").textContent = "Loading content…"; qs("#results-body").classList.add("is-loading");
    try {
      const response = await fetch(`/api/items?${queryParams()}`); if (!response.ok) throw new Error("Content could not be loaded.");
      const payload = await response.json(); if (number !== state.requestNumber) return;
      if (resetFacets) {
        setOptions(controls.classification, payload.facets.classifications, "All findings"); setOptions(controls.status, payload.facets.statuses, "All statuses");
        setOptions(controls.author, payload.facets.authors, "All authors", (value) => value); setOptions(controls.subtype, payload.facets.subtypes, "All subtypes");
        setOptions(controls.fileType, payload.facets.file_types, "All file types", fileTypeLabel);
        setOptions(controls.decision, payload.facets.decisions, "All decisions");
        if (controls.live) setOptions(controls.live, payload.facets.live || [], "All live states");
      }
      renderRows(payload.items); qs("#result-count").textContent = `${payload.total.toLocaleString()} record${payload.total === 1 ? "" : "s"} in this view`;
      if (writeEnabled && !qs(".trash-hint")) {
        qs("#result-count").after(el("p", "trash-hint", "Checkboxes unlock after the live WordPress check passes and you choose Approve to delete in the review card."));
      }
      if (state.queue) qs("#summary-strip").replaceChildren(summaryItem("In queue", payload.total, "Needs attention"));
      renderLiveCoverage(payload.live_summary);
      qs("#page-status").textContent = `Page ${payload.page.toLocaleString()} of ${payload.page_count.toLocaleString()}`;
      qs("#previous-page").disabled = payload.page <= 1; qs("#next-page").disabled = payload.page >= payload.page_count; qs("#empty-state").hidden = payload.total !== 0;
      const exportParams = queryParams(false); qs("#export-csv").href = `/api/export.csv?${exportParams}`; qs("#export-json").href = `/api/export.json?${exportParams}`;
    } catch (error) { qs("#result-count").textContent = error.message; qs("#empty-state").hidden = false; }
    finally { qs("#results-body").classList.remove("is-loading"); }
  }
  const liveRun = { active: false };
  function renderLiveCoverage(summary) {
    const node = qs("#live-coverage"), button = qs("#live-check-group");
    if (!node || !summary) return;
    const { total = 0, checked = 0, trashed = 0, missing = 0, error = 0 } = summary;
    const scope = state.queue ? "the work queue" : `the ${activeGroup().label || "selected"} group`;
    node.hidden = false; node.classList.toggle("is-warning", total > 0 && checked === 0);
    node.replaceChildren();
    if (!total) { node.append(el("span", "", "No records to check live.")); if (button) button.disabled = true; return; }
    if (button) { button.disabled = liveRun.active; button.textContent = liveRun.active ? "Checking live WordPress…" : (checked ? "Re-check live WordPress" : "Check live WordPress"); }
    const bar = document.createElement("progress"); bar.max = total; bar.value = checked;
    const summaryText = el("span", "");
    summaryText.append(el("strong", "", `${checked.toLocaleString()} of ${total.toLocaleString()}`), ` records in ${scope} checked against live WordPress`);
    node.append(bar, summaryText);
    if (checked) node.append(el("span", "", `${trashed.toLocaleString()} in Trash · ${missing.toLocaleString()} missing · ${error.toLocaleString()} errors`));
    else node.append(el("span", "", "Live states reset whenever a new export is analyzed. Run the check to populate the “Live WordPress” filter, including “In trash”."));
  }

  async function liveCheckGroup() {
    const button = qs("#live-check-group"), node = qs("#live-coverage");
    if (!button || liveRun.active) return;
    liveRun.active = true; button.disabled = true; button.textContent = "Checking live WordPress…";
    const scopeParams = new URLSearchParams(state.queue ? { queue: "1" } : { group: state.group });
    const errors = [];
    let checked = 0, total = 0;
    try {
      const ids = (await responseJson(await fetch(`/api/items/ids?${scopeParams}`))).ids.map(String);
      total = ids.length;
      const batch = 100;
      for (let start = 0; start < ids.length; start += batch) {
        const chunk = ids.slice(start, start + batch);
        node.replaceChildren(el("span", "", `Checking live WordPress: ${checked.toLocaleString()} of ${total.toLocaleString()} records…`));
        try {
          const result = await responseJson(await fetch("/api/live-check", {
            method: "POST", headers: csrfHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ ids: chunk }),
          }));
          checked += result.checked || chunk.length;
        } catch (error) {
          errors.push(error.message);
          if (error.status === 403 || error.status === 409) break;
        }
      }
    } catch (error) { errors.push(error.message); }
    liveRun.active = false;
    await loadItems(true);
    await loadQueueCount();
    if (errors.length) {
      const alert = el("p", "review-live-note", `Live check stopped after ${checked.toLocaleString()} of ${total.toLocaleString()} records: ${errors[0]}`);
      qs("#live-coverage")?.append(alert);
    }
  }
  qs("#live-check-group")?.addEventListener("click", liveCheckGroup);

  function detailField(list, name, value) { const field = el("div", "detail-field"); field.append(el("dt", "", name), el("dd", "", value || "—")); list.append(field); }
  function reviewContext(item) {
    return [
      `${review.index + 1} of ${review.total}`,
      state.queue ? "Work queue" : (item.group_label || activeGroup().label || "Content"),
      humanize(item.classification),
    ].join(" · ");
  }
  function updateReviewNav() {
    qs("#review-prev").disabled = review.index <= 0;
    qs("#review-next").disabled = review.index >= review.ids.length - 1;
    qs("#review-position").textContent = review.total ? `${review.index + 1} of ${review.total}` : "—";
  }
  async function ensureReviewIds(id) {
    const key = queryParams(false).toString();
    if (review.filterKey !== key || !review.ids.length) {
      const payload = await responseJson(await fetch(`/api/items/ids?${queryParams(false)}`));
      review.ids = payload.ids.map(String);
      review.total = payload.total;
      review.filterKey = key;
    }
    const wanted = String(id);
    let index = review.ids.indexOf(wanted);
    if (index < 0) {
      review.ids = [wanted];
      review.total = 1;
      index = 0;
    }
    review.index = index;
  }
  function renderReviewToolbar(item, liveNote) {
    const toolbar = qs("#review-toolbar");
    toolbar.replaceChildren();
    const decisions = el("div", "review-decisions");
    decisions.append(el("span", "review-outcome-label", "Review outcome"));
    [
      ["keep", "Keep"],
      ["verify", "Review later"],
    ].forEach(([value, label]) => {
      const button = el("button", "button button-compact review-decision", label);
      button.type = "button";
      button.disabled = Boolean(item.live_checking);
      button.classList.toggle("is-active", item.review_decision === value);
      button.addEventListener("click", () => saveReviewDecision(value, { advance: value === "keep" }));
      decisions.append(button);
    });
    const actions = el("div", "review-actions");
    const adminHref = safeLink(item.wp_admin_url);
    if (adminHref) {
      const admin = el("a", "button button-outline button-compact", item.live_state === "trashed" ? "Open wp-admin Trash" : "Edit in wp-admin");
      admin.href = adminHref; admin.target = "_blank"; admin.rel = "noopener noreferrer"; actions.append(admin);
    }
    const note = liveNote || "";
    if (note) actions.append(el("p", "review-live-note", note));
    toolbar.append(decisions, actions);
  }
  function renderReviewBody(item) {
    qs("#detail-title").textContent = item.title || item.file_name || "Untitled";
    qs("#detail-kicker").textContent = reviewContext(item);
    updateReviewNav();
    const content = qs("#detail-content"); content.replaceChildren();
    const finding = el("section", "detail-finding"), findingText = el("div");
    findingText.append(el("strong", "", item.recommendation), el("p", "", item.reasons.join(" ")));
    finding.append(badge(item.classification), findingText); content.append(finding);
    if (item.expected_development) finding.append(badge("expected-development"));
    if (item.live_state === "trashed") {
      const complete = el("section", "review-complete");
      complete.append(el("strong", "", "This record is in WordPress Trash."), el("p", "", "The move was reversible; restoration remains a WordPress admin action."));
      content.append(complete);
    } else if (restEnabled || writeEnabled) {
      const readiness = el("section", "review-readiness");
      readiness.append(
        el("h3", "", "Move this record to Trash"),
        el("p", "review-readiness-intro", "The app checks the live record before allowing this reversible WordPress action."),
      );
      const checks = el("div", "review-checklist");
      const addCheck = (ready, label, detail) => {
        const row = el("div", `review-check ${ready ? "is-ready" : "is-pending"}`);
        row.append(el("span", "review-check-icon", ready ? "✓" : "—"));
        const copy = el("span"); copy.append(el("strong", "", label));
        if (detail) copy.append(el("small", "", detail));
        row.append(copy); checks.append(row);
      };
      addCheck(
        item.live_state === "found" && !item.live_checking,
        "Live WordPress check",
        item.live_checking ? "Checking now…" : item.live_state === "found" ? "Record found." : humanize(item.live_state || "unchecked"),
      );
      addCheck(
        Boolean(item.live_identity_matches) && !item.live_checking,
        "Export matches the live record",
        item.live_identity_matches ? "ID, type, title, and version are ready." : "Trash stays blocked when the record differs or the check is incomplete.",
      );
      addCheck(
        item.review_decision === "approved",
        "Approval",
        item.review_decision === "approved" ? "Approved to delete." : "Choose Approve to delete below.",
      );
      readiness.append(checks);
      if (writeEnabled) {
        const trashAction = el("div", "review-trash-action");
        const approve = el(
          "button",
          `button button-approve-delete${item.review_decision === "approved" ? " is-active" : ""}`,
          item.review_decision === "approved" ? "Approved to delete" : "Approve to delete",
        );
        approve.type = "button";
        approve.disabled = Boolean(item.live_checking || item.review_decision === "approved");
        approve.addEventListener("click", async () => {
          approve.disabled = true;
          if (!await saveReviewDecision("approved")) approve.disabled = false;
        });
        trashAction.append(approve, makeTrashButton(item, "button button-danger"));
        const reason = trashReason(item);
        if (reason) trashAction.append(el("small", "", reason));
        readiness.append(trashAction);
      }
      content.append(readiness);
    }
    if (["found", "trashed"].includes(item.live_state)) {
      const comparison = el("section", "review-comparison");
      comparison.append(el("h3", "", "Export and live identity"));
      const grid = el("div", "comparison-grid");
      const head = el("div", "comparison-row comparison-head");
      head.append(el("strong", "", "Field"), el("span", "", "Export"), el("span", "", "Live WordPress"));
      grid.append(head);
      [
        ["WordPress ID", item.id, item.id],
        ["Type", humanize(item.type), humanize(item.live_type || "Not returned")],
        ["Title", item.title || "Untitled", item.live_title || "Untitled"],
        ["Status", humanize(item.status), humanize(item.live_status || "Not returned")],
        ["Modified", item.modified || "Not exported", item.live_modified || "Not returned"],
      ].forEach(([label, exported, live]) => {
        const row = el("div", "comparison-row");
        row.append(el("strong", "", label), el("span", "", exported), el("span", "", live));
        grid.append(row);
      });
      comparison.append(grid); content.append(comparison);
    }
    const links = el("section", "review-links");
    const liveHref = safeLink(item.live_link);
    const exportHref = safeLink(item.url);
    function appendLinkRow(label, href, emptyText) {
      const row = el("div", "review-link-row");
      row.append(el("strong", "", label));
      if (href) {
        const link = el("a", "detail-link", href);
        link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
        row.append(link);
      } else {
        row.append(el("span", "muted", emptyText));
      }
      links.append(row);
    }
    appendLinkRow(
      item.live_link ? "Live page on the site" : "Public URL from the export",
      liveHref || exportHref,
      "No public URL is available for this record.",
    );
    if (exportHref && liveHref && exportHref !== liveHref) {
      appendLinkRow("URL stored in the export", exportHref, "");
    }
    content.append(links);
    const details = el("dl", "detail-grid");
    detailField(details, "Content type", `${item.group_label} · ${humanize(item.content_class)}`);
    detailField(details, "Status", humanize(item.status));
    detailField(details, "Live WordPress", item.live_state ? `${humanize(item.live_state)}${item.live_status ? ` · ${humanize(item.live_status)}` : ""}` : "Not checked");
    detailField(details, "Live check time", item.live_checked_at || "Not checked");
    detailField(details, "Review decision", humanize(item.review_decision));
    detailField(details, "Author", item.author_name || item.author_login || "Unknown");
    detailField(details, "Author login", item.author_login);
    detailField(details, "Published", item.created);
    detailField(details, "Author email", item.author_email);
    detailField(details, "Last updated", item.modified);
    detailField(details, "Parent ID", item.parent_id === "0" ? "None" : item.parent_id);
    detailField(details, "Inbound evidence", `${item.inbound_strong} strong · ${item.inbound_possible} possible · ${item.inbound_structural} structural`);
    detailField(details, "Outbound references", String(item.outbound));
    if (item.group === "media" || item.group === "documents") {
      const previewUrl = safeLink(item.url), extension = (item.file_extension || "").toLowerCase();
      const isImage = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"].includes(extension);
      if (previewUrl) {
        const preview = el("section", "media-review");
        const previewPane = el("div", "media-preview");
        if (isImage) {
          const image = document.createElement("img");
          image.src = previewUrl; image.alt = item.alt_text || item.title || "Media preview";
          image.loading = "lazy"; image.referrerPolicy = "no-referrer";
          image.addEventListener("error", () => { previewPane.replaceChildren(el("p", "muted", "Preview unavailable. The exported URL may be private or no longer reachable.")); });
          previewPane.append(image);
        } else previewPane.append(el("div", "file-preview-icon", fileTypeLabel(item.file_extension || "<none>")));
        const previewInfo = el("div", "media-preview-info");
        previewInfo.append(el("strong", "", isImage ? "Image preview" : "File preview"), el("span", "muted", isImage ? "Loaded from the exported attachment URL." : "Binary file is not included in the WXR export."));
        if (previewUrl) { const link = el("a", "detail-link", "Open original"); link.href = previewUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; previewInfo.append(link); }
        preview.append(previewPane, previewInfo); content.append(preview);
      }
      detailField(details, "File", item.file_name || item.url);
      detailField(details, "File type", fileTypeLabel(item.file_extension || "<none>"));
      detailField(details, "MIME type", item.mime_type || "Not exported");
      detailField(details, "File size", formatBytes(item.file_size));
      detailField(details, "Dimensions", item.width && item.height ? `${item.width} × ${item.height} px` : "Not exported");
      detailField(details, "ALT text", item.alt_text || "Not set");
      detailField(details, "Caption", item.caption || "Not set");
      detailField(details, "Description", item.description || "Not set");
      detailField(details, "Generated variants", String(item.derivative_count || 0));
      detailField(details, "Stored path", item.stored_path || "Not exported");
    }
    detailField(details, "Exported meta keys", (item.meta_keys || []).join(", ") || "None");
    content.append(details);
    const taxonomies = Object.entries(item.taxonomy_terms || {});
    if (taxonomies.length) {
      content.append(el("h3", "detail-section-title", "Taxonomies"));
      const list = el("div", "detail-taxonomies");
      taxonomies.forEach(([key, values]) => {
        const group = el("div"); group.append(el("strong", "", taxonomyLabels[key] || humanize(key)), el("span", "", values.join(", "))); list.append(group);
      });
      content.append(list);
    }
    content.append(el("h3", "detail-section-title", `Where-used evidence (${item.evidence.length})`));
    if (!item.evidence.length) content.append(el("p", "muted", "No inbound evidence appears in the site export."));
    else {
      const evidence = el("div", "evidence-list");
      item.evidence.forEach((entry) => {
        const card = el("article", "evidence-card");
        card.append(el("strong", "", entry.source_title || `Source ${entry.source_id}`), el("span", "", `${humanize(entry.strength)} · ${humanize(entry.kind)} · ${entry.field}`));
        if (entry.evidence) card.append(el("code", "", entry.evidence));
        evidence.append(card);
      });
      content.append(evidence);
    }
    if (item.derivative_files?.length) {
      content.append(el("h3", "detail-section-title", `Generated media variants (${item.derivative_files.length})`));
      const list = el("ul", "variant-list");
      item.derivative_files.forEach((file) => list.append(el("li", "", file)));
      content.append(list);
    }
  }
  function liveNoteFor(item, extra) {
    if (extra) return extra;
    if (!restEnabled) return "";
    if (item.live_state === "unchecked") return "Checking live WordPress for this record…";
    if (writeEnabled && item.live_state === "found" && !item.can_trash) {
      return trashReason(item);
    }
    if (item.live_state === "found" && item.can_trash) return "This record is live. Confirm Trash to move it to WordPress Trash.";
    return "";
  }
  async function refreshCurrentItem() {
    const id = review.ids[review.index];
    const item = await responseJson(await fetch(`/api/items/${encodeURIComponent(id)}`));
    review.item = item;
    renderReviewBody(item);
    renderReviewToolbar(item, liveNoteFor(item));
    return item;
  }
  async function liveCheckCurrent(seq) {
    const id = review.ids[review.index];
    review.item = { ...review.item, can_trash: false, live_checking: true };
    renderReviewBody(review.item);
    renderReviewToolbar(review.item, "Checking live WordPress for this record…");
    try {
      await responseJson(await fetch("/api/live-check", {
        method: "POST", headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ ids: [id] }),
      }));
      if (seq !== review.seq) return;
      await refreshCurrentItem();
      loadItems();
      loadQueueCount();
    } catch (error) {
      if (seq !== review.seq) return;
      review.item = { ...review.item, live_checking: false, can_trash: false };
      renderReviewBody(review.item);
      renderReviewToolbar(review.item, error.message || "Live WordPress check failed.");
    }
  }
  async function saveReviewDecision(decision, options = {}) {
    const item = review.item;
    if (!item) return false;
    try {
      await responseJson(await fetch(`/api/items/${encodeURIComponent(item.id)}/decision`, {
        method: "POST", headers: csrfHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ decision }),
      }));
      await refreshCurrentItem();
      loadItems();
      if (options.advance) await goReview(1);
      return true;
    } catch (error) {
      window.alert(error.message || "The review decision could not be saved.");
      return false;
    }
  }
  async function trashCurrentRecord(button) {
    const item = review.item;
    if (!item) return;
    button.disabled = true;
    try {
      const ok = await trashRecords(
        [item.id],
        [`${item.live_title || item.title || item.file_name || "Untitled"} (${humanize(item.live_type || item.type)} #${item.id})`],
        { quiet: true, skipReload: true },
      );
      if (!ok) { button.disabled = false; return; }
      updateSelectionUI();
      await loadItems();
      loadQueueCount();
      await refreshCurrentItem();
    } catch (error) {
      window.alert(error.message || "WordPress Trash failed.");
      button.disabled = false;
    }
  }
  async function showReviewRecord() {
    const seq = ++review.seq;
    const id = review.ids[review.index];
    qs("#detail-title").textContent = "Loading record…";
    qs("#detail-content").replaceChildren(el("p", "loading-copy", "Retrieving metadata and evidence…"));
    qs("#review-toolbar").replaceChildren();
    updateReviewNav();
    try {
      const item = await responseJson(await fetch(`/api/items/${encodeURIComponent(id)}`));
      if (seq !== review.seq) return;
      review.item = item;
      renderReviewBody(item);
      renderReviewToolbar(item, liveNoteFor(item));
      if (restEnabled) {
        await liveCheckCurrent(seq);
      }
    } catch (error) {
      if (seq !== review.seq) return;
      qs("#detail-content").replaceChildren(el("p", "alert alert-error", error.message));
    }
  }
  async function goReview(delta) {
    const next = review.index + delta;
    if (next < 0 || next >= review.ids.length) return;
    review.index = next;
    await showReviewRecord();
  }
  async function openDetails(id) {
    review.open = true;
    const dialog = qs("#detail-dialog");
    qs("#detail-title").textContent = "Loading record…";
    qs("#detail-kicker").textContent = "Review sheet";
    qs("#detail-content").replaceChildren(el("p", "loading-copy", "Retrieving metadata and evidence…"));
    qs("#review-toolbar").replaceChildren();
    if (!dialog.open) dialog.showModal();
    try {
      await ensureReviewIds(id);
      await showReviewRecord();
    } catch (error) {
      qs("#detail-content").replaceChildren(el("p", "alert alert-error", error.message));
    }
  }
  async function loadTaxonomies() {
    const grid = qs("#taxonomy-grid"); grid.replaceChildren(el("p", "loading-copy", "Loading taxonomy assignments…"));
    try {
      const response = await fetch(`/api/taxonomies/${encodeURIComponent(state.group)}`); if (!response.ok) throw new Error("Taxonomies could not be loaded."); const payload = await response.json(); grid.replaceChildren();
      qs("#taxonomy-empty").hidden = payload.taxonomies.length !== 0;
      payload.taxonomies.forEach((taxonomy) => {
        const card = el("article", "taxonomy-card"), head = el("header", "taxonomy-card-head"), title = el("div"), stats = el("div", "taxonomy-stats"), terms = el("div", "term-cloud");
        title.append(el("h3", "", taxonomy.label), el("code", "taxonomy-key", taxonomy.key)); stats.append(el("span", "", `${taxonomy.unique_terms.toLocaleString()} terms`), el("span", "", `${taxonomy.records_tagged.toLocaleString()} records`), el("span", "", `${taxonomy.assignments.toLocaleString()} assignments`)); head.append(title, stats);
        taxonomy.terms.forEach((term) => { const button = el("button", "term-button"); button.type = "button"; button.append(el("span", "", term.name), el("b", "", term.count.toLocaleString()));
          button.addEventListener("click", () => { state.taxonomy = taxonomy.key; state.term = term.slug || term.name; state.page = 1; qs("#active-taxonomy-text").textContent = `${taxonomy.label}: ${term.name}`;
            qs("#active-taxonomy-filter").hidden = false; showPanel("content-panel"); loadItems(); }); terms.append(button); });
        card.append(head, terms); grid.append(card);
      });
    } catch (error) { grid.replaceChildren(el("p", "alert alert-error", error.message)); }
  }
  function chooseGroup(groupId) {
    clearSelection();
    state.queue = false;
    state.group = groupId; state.page = 1; state.taxonomy = ""; state.term = ""; controls.search.value = "";
      [controls.classification, controls.status, controls.author, controls.subtype, controls.fileType, controls.decision].forEach((control) => { control.value = ""; });
      if (controls.live) controls.live.value = "";
    qs("#active-taxonomy-filter").hidden = true; qsa(".group-card").forEach((card) => card.classList.toggle("is-active", card.dataset.group === groupId));
    review.filterKey = ""; review.ids = [];
    showPanel("content-panel"); renderGroupHeader(); loadItems(true); loadTaxonomies();
  }
  qsa(".group-card").forEach((card) => card.addEventListener("click", () => chooseGroup(card.dataset.group)));
  qsa(".workspace-tab").forEach((tab) => tab.addEventListener("click", () => {
    const nextQueue = tab.dataset.queue === "1";
    if (tab.dataset.panel === "content-panel" && state.queue !== nextQueue) {
      state.queue = nextQueue;
      clearSelection();
      state.page = 1;
      if (state.queue) qsa(".group-card").forEach((card) => card.classList.remove("is-active"));
      else qsa(".group-card").forEach((card) => card.classList.toggle("is-active", card.dataset.group === state.group));
      renderGroupHeader();
      review.filterKey = ""; review.ids = [];
      loadItems(true);
    }
    showPanel(tab.dataset.panel);
  }));
  qs("#coverage-shortcut").addEventListener("click", () => { showPanel("coverage-panel"); qs(".audit-workspace").scrollIntoView({ behavior: "smooth", block: "start" }); });
  qs("#new-analysis").addEventListener("click", () => { const details = qs("#replace-export"); details.open = true; details.scrollIntoView({ behavior: "smooth", block: "center" }); });
  qs("#delete-audit")?.addEventListener("click", async () => {
    if (!window.confirm("Remove this audit report and its review decisions from storage?")) return;
    const response = await fetch("/api/audit", { method: "DELETE", headers: csrfHeaders() });
    if (response.ok) window.location.assign("/");
    else window.alert("The stored audit could not be removed.");
  });
  qs("#select-page")?.addEventListener("change", (event) => {
    qsa(".row-select").forEach((box) => {
      if (box.disabled) return;
      box.checked = event.target.checked;
      const itemId = box.dataset.id;
      if (!itemId) return;
      if (box.checked) selected.set(itemId, { id: itemId, title: box.dataset.title || itemId, selectable_for_trash: true });
      else selected.delete(itemId);
    });
    updateSelectionUI();
  });
  qs("#bulk-trash")?.addEventListener("click", async () => {
    const items = [...selected.values()];
    const button = qs("#bulk-trash");
    button.disabled = true;
    try {
      await reviewBulkTrash(items);
    } catch (error) {
      window.alert(error.message || "WordPress Trash failed.");
    } finally {
      updateSelectionUI();
    }
  });
  qs("#clear-taxonomy-filter").addEventListener("click", () => { clearSelection(); state.taxonomy = ""; state.term = ""; state.page = 1; review.filterKey = ""; review.ids = []; qs("#active-taxonomy-filter").hidden = true; loadItems(); });
  qs("#previous-page").addEventListener("click", () => { state.page -= 1; loadItems(); }); qs("#next-page").addEventListener("click", () => { state.page += 1; loadItems(); });
  controls.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { clearSelection(); state.page = 1; review.filterKey = ""; review.ids = []; loadItems(); }, 250); });
  [controls.classification, controls.status, controls.author, controls.subtype, controls.fileType, controls.decision, controls.live, controls.sort, controls.pageSize].filter(Boolean).forEach((control) => control.addEventListener("change", () => { clearSelection(); state.page = 1; if (control !== controls.pageSize) { review.filterKey = ""; review.ids = []; } loadItems(); }));
  qs("#review-prev").addEventListener("click", () => goReview(-1));
  qs("#review-next").addEventListener("click", () => goReview(1));
  qs("#close-dialog").addEventListener("click", () => qs("#detail-dialog").close());
  qs("#detail-dialog").addEventListener("click", (event) => { if (event.target === qs("#detail-dialog")) qs("#detail-dialog").close(); });
  qs("#detail-dialog").addEventListener("close", () => { review.open = false; review.seq += 1; });
  document.addEventListener("keydown", (event) => {
    if (!qs("#detail-dialog")?.open) return;
    const tag = (event.target && event.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (event.key === "ArrowLeft" || event.key === "k" || event.key === "K") { event.preventDefault(); goReview(-1); }
    else if (event.key === "ArrowRight" || event.key === "j" || event.key === "J") { event.preventDefault(); goReview(1); }
  });
  renderGroupHeader(); loadItems(true); loadTaxonomies(); loadQueueCount();
})();
