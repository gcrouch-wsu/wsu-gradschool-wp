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
    disconnected: "Disconnected island", "needs-verification": "Needs verification",
    "expected-development": "Expected development", "non-public": "Non-public", linked: "Linked",
    publish: "Published", inherit: "Inherited", draft: "Draft", private: "Private", pending: "Pending",
    unreviewed: "Unreviewed", keep: "Keep", expected: "Expected development", verify: "Verify",
    candidate: "Deletion candidate", approved: "Approved to delete",
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
    if (!writeEnabled) return "Local WordPress Trash is not enabled.";
    if (item.can_trash) return "";
    if (item.live_state === "unchecked") return "Live-check this record first, then mark Candidate or Approved.";
    if (item.live_state === "missing") return "This record was not found on live WordPress.";
    if (item.live_state === "trashed") return "Already in WordPress Trash.";
    if (item.live_state === "not-in-rest") return "This content type cannot be trashed through WordPress REST.";
    if (item.live_state === "error") return "Live check failed, so Trash is blocked.";
    if (!["candidate", "approved"].includes(item.review_decision)) return "Mark Candidate or Approved to enable Trash.";
    return "Trash is not available for this record.";
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
    if (!response.ok) throw new Error(payload.error || `The server returned HTTP ${response.status}.`);
    return payload;
  }

  async function uploadInChunks(form) {
    const input = qs('input[type="file"]', form), file = input?.files?.[0];
    if (!file) return;
    const loading = qs("#loading"), progress = qs("#upload-progress");
    const label = qs("#loading-label"), detail = qs("#loading-detail");
    loading.hidden = false; progress.value = 0; label.textContent = "Uploading the WordPress export";
    try {
      const totalChunks = Math.ceil(file.size / chunkSize);
      await responseJson(await fetch("/api/upload-session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size, total_chunks: totalChunks }),
      }));
      for (let index = 0; index < totalChunks; index += 1) {
        const start = index * chunkSize, end = Math.min(start + chunkSize, file.size);
        detail.textContent = `Securely uploading part ${index + 1} of ${totalChunks}…`;
        await responseJson(await fetch(`/api/upload-chunk/${index}`, {
          method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file.slice(start, end),
        }));
        progress.value = Math.round(((index + 1) / totalChunks) * 75);
      }
      label.textContent = "Analyzing the WordPress export";
      detail.textContent = "Building the inventory, taxonomies, and reference graph…";
      progress.value = 82;
      const completed = await responseJson(await fetch("/api/complete-upload", { method: "POST" }));
      progress.value = 100;
      detail.textContent = `${completed.records.toLocaleString()} records analyzed. Opening the dashboard…`;
      window.location.assign(completed.redirect || "/");
    } catch (error) {
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
      summaryItem("Review", group.review_candidates, "Needs attention"), summaryItem("Expected", group.expected_development, "Development content"),
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
    const preview = titles.slice(0, 8).join("\n");
    const extra = titles.length > 8 ? `\n…and ${titles.length - 8} more` : "";
    const confirmed = window.confirm(
      `Move ${ids.length} record${ids.length === 1 ? "" : "s"} to Trash in live WordPress?\n\nThis can be undone in WordPress Trash. It is not a permanent delete.\n\n${preview}${extra}`,
    );
    if (!confirmed) return false;
    const payload = await responseJson(await fetch("/api/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, confirm: "trash", csrf: runtime.write_csrf || "" }),
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
    ["unreviewed", "keep", "expected", "verify", "candidate", "approved"].forEach((value) => {
      const option = el("option", "", humanize(value)); option.value = value; option.selected = item.review_decision === value; select.append(option);
    });
    select.dataset.state = item.review_decision;
    select.addEventListener("change", async () => {
      const prior = select.dataset.state; select.disabled = true;
      try {
        const response = await fetch(`/api/items/${encodeURIComponent(item.id)}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: select.value }) });
        if (!response.ok) throw new Error("The review decision could not be saved.");
        select.dataset.state = select.value; select.classList.add("is-saved"); setTimeout(() => select.classList.remove("is-saved"), 900);
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
      if (writeEnabled) actions.append(makeTrashButton(item));
      if (writeEnabled) {
        const select = document.createElement("td");
        const box = el("input", "row-select");
        box.type = "checkbox";
        box.dataset.id = item.id;
        box.checked = selected.has(item.id);
        box.disabled = !item.can_trash;
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
        qs("#result-count").after(el("p", "trash-hint", "Checkboxes and Trash stay locked until a record is found live and marked Candidate or Approved. Open Review to do that."));
      }
      if (state.queue) qs("#summary-strip").replaceChildren(summaryItem("In queue", payload.total, "Needs attention"));
      qs("#page-status").textContent = `Page ${payload.page.toLocaleString()} of ${payload.page_count.toLocaleString()}`;
      qs("#previous-page").disabled = payload.page <= 1; qs("#next-page").disabled = payload.page >= payload.page_count; qs("#empty-state").hidden = payload.total !== 0;
      const exportParams = queryParams(false); qs("#export-csv").href = `/api/export.csv?${exportParams}`; qs("#export-json").href = `/api/export.json?${exportParams}`;
    } catch (error) { qs("#result-count").textContent = error.message; qs("#empty-state").hidden = false; }
    finally { qs("#results-body").classList.remove("is-loading"); }
  }
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
    [
      ["keep", "Keep"],
      ["expected", "Expected"],
      ["verify", "Verify"],
      ["candidate", "Candidate"],
      ["approved", "Approved"],
    ].forEach(([value, label]) => {
      const button = el("button", "button button-compact review-decision", label);
      button.type = "button";
      button.classList.toggle("is-active", item.review_decision === value);
      button.addEventListener("click", () => saveReviewDecision(value, { advance: value === "keep" }));
      decisions.append(button);
    });
    const actions = el("div", "review-actions");
    const liveHref = safeLink(item.live_link) || safeLink(item.url);
    if (liveHref) {
      const publicLink = el("a", "button button-primary button-compact", item.live_link ? "View live page" : "View exported URL");
      publicLink.href = liveHref; publicLink.target = "_blank"; publicLink.rel = "noopener noreferrer"; actions.append(publicLink);
    }
    const adminHref = safeLink(item.wp_admin_url);
    if (adminHref) {
      const admin = el("a", "button button-outline button-compact", item.live_state === "trashed" ? "Open wp-admin Trash" : "Edit in wp-admin");
      admin.href = adminHref; admin.target = "_blank"; admin.rel = "noopener noreferrer"; actions.append(admin);
    }
    if (writeEnabled) actions.append(makeTrashButton(item));
    const note = liveNote || trashReason(item);
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
    const links = el("section", "review-links");
    const liveHref = safeLink(item.live_link);
    const exportHref = safeLink(item.url);
    const adminHref = safeLink(item.wp_admin_url);
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
    appendLinkRow(
      "WordPress admin editor",
      adminHref,
      "No wp-admin edit URL is available.",
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
      return "Mark Candidate or Approved to enable Trash. Keep and Trash stay on this sheet.";
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
    renderReviewToolbar(review.item, "Checking live WordPress for this record…");
    try {
      await responseJson(await fetch("/api/live-check", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [id] }),
      }));
      if (seq !== review.seq) return;
      await refreshCurrentItem();
      loadItems();
      loadQueueCount();
    } catch (error) {
      if (seq !== review.seq) return;
      renderReviewToolbar(review.item, error.message || "Live WordPress check failed.");
    }
  }
  async function saveReviewDecision(decision, options = {}) {
    const item = review.item;
    if (!item) return;
    try {
      await responseJson(await fetch(`/api/items/${encodeURIComponent(item.id)}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      }));
      await refreshCurrentItem();
      loadItems();
      if (options.advance) await goReview(1);
    } catch (error) {
      window.alert(error.message || "The review decision could not be saved.");
    }
  }
  async function trashCurrentRecord(button) {
    const item = review.item;
    if (!item) return;
    button.disabled = true;
    try {
      const ok = await trashRecords(
        [item.id],
        [item.title || item.file_name || `ID ${item.id}`],
        { quiet: true, skipReload: true },
      );
      if (!ok) { button.disabled = false; return; }
      updateSelectionUI();
      await loadItems();
      loadQueueCount();
      if (review.index < review.ids.length - 1) await goReview(1);
      else await refreshCurrentItem();
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
      if (restEnabled && (!item.live_state || item.live_state === "unchecked")) {
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
    const response = await fetch("/api/audit", { method: "DELETE" });
    if (response.ok) window.location.assign("/");
    else window.alert("The stored audit could not be removed.");
  });
  qs("#select-page")?.addEventListener("change", (event) => {
    qsa(".row-select").forEach((box) => {
      if (box.disabled) return;
      box.checked = event.target.checked;
      const itemId = box.dataset.id;
      if (!itemId) return;
      if (box.checked) selected.set(itemId, { id: itemId, title: box.getAttribute("aria-label") || itemId, can_trash: true });
      else selected.delete(itemId);
    });
    updateSelectionUI();
  });
  qs("#bulk-trash")?.addEventListener("click", async () => {
    const items = [...selected.values()];
    const button = qs("#bulk-trash");
    button.disabled = true;
    try {
      await trashRecords(items.map((item) => item.id), items.map((item) => item.title || item.file_name || `ID ${item.id}`));
    } catch (error) {
      window.alert(error.message || "WordPress Trash failed.");
    } finally {
      updateSelectionUI();
    }
  });
  qs("#clear-taxonomy-filter").addEventListener("click", () => { state.taxonomy = ""; state.term = ""; state.page = 1; review.filterKey = ""; review.ids = []; qs("#active-taxonomy-filter").hidden = true; loadItems(); });
  qs("#previous-page").addEventListener("click", () => { state.page -= 1; loadItems(); }); qs("#next-page").addEventListener("click", () => { state.page += 1; loadItems(); });
  controls.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 1; review.filterKey = ""; review.ids = []; loadItems(); }, 250); });
  [controls.classification, controls.status, controls.author, controls.subtype, controls.fileType, controls.decision, controls.live, controls.sort, controls.pageSize].filter(Boolean).forEach((control) => control.addEventListener("change", () => { state.page = 1; if (control !== controls.pageSize) { review.filterKey = ""; review.ids = []; } loadItems(); }));
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
