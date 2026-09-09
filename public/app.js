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
  const writeEnabled = Boolean(runtime.rest_write_enabled);

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
  async function trashRecords(ids, titles) {
    if (!ids.length) return;
    if (ids.length > 25) {
      window.alert("Move at most 25 records to Trash at a time.");
      return;
    }
    const preview = titles.slice(0, 8).join("\n");
    const extra = titles.length > 8 ? `\n…and ${titles.length - 8} more` : "";
    const confirmed = window.confirm(
      `Move ${ids.length} record${ids.length === 1 ? "" : "s"} to Trash in live WordPress?\n\nThis can be undone in WordPress Trash. It is not a permanent delete.\n\n${preview}${extra}`,
    );
    if (!confirmed) return;
    const payload = await responseJson(await fetch("/api/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, confirm: "trash" }),
    }));
    payload.results.filter((result) => result.ok).forEach((result) => selected.delete(result.id));
    const failed = payload.results.filter((result) => !result.ok);
    if (failed.length) {
      window.alert(`Moved ${payload.trashed} to Trash.\n${failed.length} failed:\n${failed.map((result) => `${result.title}: ${result.error}`).join("\n")}`);
    } else {
      window.alert(`Moved ${payload.trashed} record${payload.trashed === 1 ? "" : "s"} to WordPress Trash.`);
    }
    updateSelectionUI();
    await loadItems();
    loadQueueCount();
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
      live.append(el("span", `badge badge-live-${item.live_state || "unchecked"}`, humanize(item.live_state || "unchecked")));
      if (item.live_status) live.append(meta(humanize(item.live_status)));
      const title = el("button", "content-title", item.title || item.file_name || "Untitled"); title.type = "button"; title.addEventListener("click", () => openDetails(item.id));
      content.append(title, meta(`${humanize(item.content_class)} · ${humanize(item.status)} · ID ${item.id}`));
      if (item.group === "media") content.append(meta(`${item.mime_type || "Unknown format"} · ${formatBytes(item.file_size)}${item.width && item.height ? ` · ${item.width} × ${item.height}` : ""}`));
      author.textContent = item.author_name || item.author_login || "Unknown";
      if (item.author_name && item.author_login && item.author_name !== item.author_login) author.append(meta(item.author_login));
      taxonomy.append(taxonomySummary(item)); published.textContent = dateOnly(item.created); updated.textContent = dateOnly(item.modified); review.append(reviewSelect(item));
      const button = el("button", "button button-detail", "Details"); button.type = "button"; button.addEventListener("click", () => openDetails(item.id)); actions.append(button);
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
      if (state.queue) qs("#summary-strip").replaceChildren(summaryItem("In queue", payload.total, "Needs attention"));
      qs("#page-status").textContent = `Page ${payload.page.toLocaleString()} of ${payload.page_count.toLocaleString()}`;
      qs("#previous-page").disabled = payload.page <= 1; qs("#next-page").disabled = payload.page >= payload.page_count; qs("#empty-state").hidden = payload.total !== 0;
      const exportParams = queryParams(false); qs("#export-csv").href = `/api/export.csv?${exportParams}`; qs("#export-json").href = `/api/export.json?${exportParams}`;
    } catch (error) { qs("#result-count").textContent = error.message; qs("#empty-state").hidden = false; }
    finally { qs("#results-body").classList.remove("is-loading"); }
  }
  function detailField(list, name, value) { const field = el("div", "detail-field"); field.append(el("dt", "", name), el("dd", "", value || "—")); list.append(field); }
  async function openDetails(id) {
    const dialog = qs("#detail-dialog"); qs("#detail-title").textContent = "Loading record…"; qs("#detail-content").replaceChildren(el("p", "loading-copy", "Retrieving metadata and evidence…")); dialog.showModal();
    try {
      const response = await fetch(`/api/items/${encodeURIComponent(id)}`); if (!response.ok) throw new Error("Record details could not be loaded."); const item = await response.json();
      qs("#detail-title").textContent = item.title || item.file_name || "Untitled"; qs("#detail-kicker").textContent = `${item.group_label} · WordPress ID ${item.id}`;
      const content = qs("#detail-content"); content.replaceChildren(); const finding = el("section", "detail-finding"), findingText = el("div");
      findingText.append(el("strong", "", item.recommendation), el("p", "", item.reasons.join(" "))); finding.append(badge(item.classification), findingText); content.append(finding);
      const actionsBar = el("div", "detail-actions");
      const adminHref = safeLink(item.wp_admin_url);
      if (adminHref) {
        const admin = el("a", "button button-primary", item.live_state === "trashed" ? "Open Trash in WordPress" : "Open in WordPress");
        admin.href = adminHref; admin.target = "_blank"; admin.rel = "noopener noreferrer"; actionsBar.append(admin);
      }
      const liveHref = safeLink(item.live_link || item.url);
      if (liveHref) { const publicLink = el("a", "button button-outline", "Open public URL"); publicLink.href = liveHref; publicLink.target = "_blank"; publicLink.rel = "noopener noreferrer"; actionsBar.append(publicLink); }
      if (writeEnabled && item.can_trash) {
        const trash = el("button", "button button-danger button-compact", "Move to Trash in WordPress");
        trash.type = "button";
        trash.addEventListener("click", async () => {
          trash.disabled = true;
          try {
            await trashRecords([item.id], [item.title || item.file_name || `ID ${item.id}`]);
            qs("#detail-dialog").close();
          } catch (error) {
            window.alert(error.message || "WordPress Trash failed.");
            trash.disabled = false;
          }
        });
        actionsBar.append(trash);
      }
      if (actionsBar.childNodes.length) content.append(actionsBar);
      const details = el("dl", "detail-grid"); detailField(details, "Content type", `${item.group_label} · ${humanize(item.content_class)}`); detailField(details, "Status", humanize(item.status));
      detailField(details, "Live WordPress", item.live_state ? `${humanize(item.live_state)}${item.live_status ? ` · ${humanize(item.live_status)}` : ""}` : "Not checked");
      detailField(details, "Author", item.author_name || item.author_login || "Unknown"); detailField(details, "Author login", item.author_login); detailField(details, "Published", item.created);
      detailField(details, "Author email", item.author_email); detailField(details, "Last updated", item.modified); detailField(details, "Parent ID", item.parent_id === "0" ? "None" : item.parent_id);
      detailField(details, "Inbound evidence", `${item.inbound_strong} strong · ${item.inbound_possible} possible · ${item.inbound_structural} structural`); detailField(details, "Outbound references", String(item.outbound));
      if (item.group === "media" || item.group === "documents") {
        const previewUrl = safeLink(item.url), extension = (item.file_extension || "").toLowerCase();
        const isImage = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"].includes(extension);
        if (previewUrl) {
          const preview = el("section", "media-review");
          const previewPane = el("div", "media-preview");
          if (isImage) { const image = document.createElement("img"); image.src = previewUrl; image.alt = item.alt_text || item.title || "Media preview"; image.loading = "lazy"; image.referrerPolicy = "no-referrer"; image.addEventListener("error", () => { previewPane.replaceChildren(el("p", "muted", "Preview unavailable. The exported URL may be private or no longer reachable.")); }); previewPane.append(image); }
          else previewPane.append(el("div", "file-preview-icon", fileTypeLabel(item.file_extension || "<none>")));
          const previewInfo = el("div", "media-preview-info"); previewInfo.append(el("strong", "", isImage ? "Image preview" : "File preview"), el("span", "muted", isImage ? "Loaded from the exported attachment URL." : "Binary file is not included in the WXR export."));
          if (previewUrl) { const link = el("a", "detail-link", "Open original"); link.href = previewUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; previewInfo.append(link); }
          preview.append(previewPane, previewInfo); content.append(preview);
        }
        detailField(details, "File", item.file_name || item.url); detailField(details, "File type", fileTypeLabel(item.file_extension || "<none>")); detailField(details, "MIME type", item.mime_type || "Not exported"); detailField(details, "File size", formatBytes(item.file_size));
        detailField(details, "Dimensions", item.width && item.height ? `${item.width} × ${item.height} px` : "Not exported"); detailField(details, "ALT text", item.alt_text || "Not set");
        detailField(details, "Caption", item.caption || "Not set"); detailField(details, "Description", item.description || "Not set");
        detailField(details, "Generated variants", String(item.derivative_count || 0)); detailField(details, "Stored path", item.stored_path || "Not exported"); }
      detailField(details, "Exported meta keys", (item.meta_keys || []).join(", ") || "None");
      content.append(details);
      const taxonomies = Object.entries(item.taxonomy_terms || {});
      if (taxonomies.length) { content.append(el("h3", "detail-section-title", "Taxonomies")); const list = el("div", "detail-taxonomies");
        taxonomies.forEach(([key, values]) => { const group = el("div"); group.append(el("strong", "", taxonomyLabels[key] || humanize(key)), el("span", "", values.join(", "))); list.append(group); }); content.append(list); }
      if (item.url) { const href = safeLink(item.url), link = href ? el("a", "detail-link", item.url) : el("span", "detail-link", item.url);
        if (href) { link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; } content.append(el("h3", "detail-section-title", "Exported URL"), link); }
      content.append(el("h3", "detail-section-title", `Where-used evidence (${item.evidence.length})`));
      if (!item.evidence.length) content.append(el("p", "muted", "No inbound evidence appears in the site export."));
      else { const evidence = el("div", "evidence-list"); item.evidence.forEach((entry) => { const card = el("article", "evidence-card");
        card.append(el("strong", "", entry.source_title || `Source ${entry.source_id}`), el("span", "", `${humanize(entry.strength)} · ${humanize(entry.kind)} · ${entry.field}`));
        if (entry.evidence) card.append(el("code", "", entry.evidence)); evidence.append(card); }); content.append(evidence); }
      if (item.derivative_files?.length) { content.append(el("h3", "detail-section-title", `Generated media variants (${item.derivative_files.length})`)); const list = el("ul", "variant-list"); item.derivative_files.forEach((file) => list.append(el("li", "", file))); content.append(list); }
    } catch (error) { qs("#detail-content").replaceChildren(el("p", "alert alert-error", error.message)); }
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
          button.addEventListener("click", () => { state.taxonomy = taxonomy.key; state.term = term.name; state.page = 1; qs("#active-taxonomy-text").textContent = `${taxonomy.label}: ${term.name}`;
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
  qs("#live-check")?.addEventListener("click", async () => {
    const button = qs("#live-check");
    button.disabled = true;
    button.textContent = "Checking WordPress…";
    try {
      const payload = await responseJson(await fetch("/api/live-check", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group: state.queue ? "" : state.group }),
      }));
      window.alert(`Checked ${payload.checked.toLocaleString()} records in this group.\nFound live: ${payload.found}\nIn trash: ${payload.trashed || 0}\nMissing: ${payload.missing}\nNot in REST: ${payload.not_in_rest}\nErrors: ${payload.error}`);
      loadItems();
      loadQueueCount();
    } catch (error) {
      window.alert(error.message || "The live WordPress check failed.");
    } finally {
      button.disabled = false;
      button.textContent = "Check this group live";
    }
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
  qs("#clear-taxonomy-filter").addEventListener("click", () => { state.taxonomy = ""; state.term = ""; state.page = 1; qs("#active-taxonomy-filter").hidden = true; loadItems(); });
  qs("#previous-page").addEventListener("click", () => { state.page -= 1; loadItems(); }); qs("#next-page").addEventListener("click", () => { state.page += 1; loadItems(); });
  controls.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 1; loadItems(); }, 250); });
  [controls.classification, controls.status, controls.author, controls.subtype, controls.fileType, controls.decision, controls.live, controls.sort, controls.pageSize].filter(Boolean).forEach((control) => control.addEventListener("change", () => { state.page = 1; loadItems(); }));
  qs("#close-dialog").addEventListener("click", () => qs("#detail-dialog").close()); qs("#detail-dialog").addEventListener("click", (event) => { if (event.target === qs("#detail-dialog")) qs("#detail-dialog").close(); });
  renderGroupHeader(); loadItems(true); loadTaxonomies(); loadQueueCount();
})();
