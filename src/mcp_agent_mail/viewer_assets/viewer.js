const manifestOutput = document.getElementById("manifest-json");
const messageList = document.getElementById("message-list");
const searchInput = document.getElementById("search-input");
const messageMeta = document.getElementById("message-meta");

let messages = [];

async function loadJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to fetch ${path} (${response.status})`);
  }
  return response.json();
}

function renderProjects(manifest) {
  const list = document.getElementById("projects-list");
  list.innerHTML = "";
  const entries = manifest.project_scope?.included ?? [];
  if (!entries.length) {
    const li = document.createElement("li");
    li.textContent = "All projects";
    list.append(li);
    return;
  }
  for (const entry of entries) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${entry.slug}</strong> <span>${entry.human_key}</span>`;
    list.append(li);
  }
}

function renderScrub(manifest) {
  const scrub = manifest.scrub ?? {};
  const stats = document.getElementById("scrub-stats");
  stats.innerHTML = "";
  for (const [key, value] of Object.entries(scrub)) {
    const dt = document.createElement("dt");
    dt.textContent = key.replace(/_/g, " ");
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    stats.append(dt, dd);
  }
}

function renderAttachments(manifest) {
  const stats = manifest.attachments?.stats ?? {};
  const config = manifest.attachments?.config ?? {};
  const list = document.getElementById("attachment-stats");
  list.innerHTML = "";
  const entries = {
    "Inline assets": stats.inline,
    "Bundled files": stats.copied,
    "External references": stats.externalized,
    "Missing references": stats.missing,
    "Bytes copied": stats.bytes_copied,
    "Inline threshold (bytes)": config.inline_threshold,
    "External threshold (bytes)": config.detach_threshold,
  };
  for (const [label, value] of Object.entries(entries)) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value !== undefined ? String(value) : "—";
    list.append(dt, dd);
  }
}

function renderBundleInfo(manifest) {
  const paragraph = document.getElementById("bundle-info");
  const generated = manifest.generated_at ?? "";
  const schema = manifest.schema_version ?? "";
  const exporter = manifest.exporter_version ?? "";
  paragraph.textContent = `Generated ${generated} • Schema ${schema} • Exporter ${exporter}`;
}

function renderManifest(manifest) {
  manifestOutput.textContent = JSON.stringify(manifest, null, 2);
}

function renderMessages(list) {
  messageList.innerHTML = "";
  if (!list.length) {
    const empty = document.createElement("li");
    empty.textContent = "No messages match your filters.";
    messageList.append(empty);
    return;
  }
  for (const message of list) {
    const item = document.createElement("li");
    const header = document.createElement("h3");
    header.textContent = message.subject || "(no subject)";
    const meta = document.createElement("div");
    meta.className = "message-meta-line";
    meta.textContent = `${message.created_ts ?? "(unknown)"} • importance: ${message.importance ?? "normal"}`;
    const snippet = document.createElement("div");
    snippet.className = "message-snippet";
    snippet.textContent = message.snippet || "(empty body)";
    item.append(header, meta, snippet);
    messageList.append(item);
  }
}

function applySearch() {
  const term = searchInput.value.trim().toLowerCase();
  if (!term) {
    renderMessages(messages);
    messageMeta.textContent = `${messages.length} message(s) shown.`;
    return;
  }
  const filtered = messages.filter((msg) => {
    return (
      (msg.subject && msg.subject.toLowerCase().includes(term)) ||
      (msg.snippet && msg.snippet.toLowerCase().includes(term))
    );
  });
  renderMessages(filtered);
  messageMeta.textContent = `${filtered.length} message(s) match “${term}”.`;
}

async function bootstrap() {
  try {
    const manifest = await loadJSON("../manifest.json");
    renderManifest(manifest);
    renderBundleInfo(manifest);
    renderProjects(manifest);
    renderAttachments(manifest);
    renderScrub(manifest);

    if (manifest.viewer?.meta) {
      messageMeta.textContent = "Loading message index…";
    }

    try {
      const messageData = await loadJSON("./data/messages.json");
      messages = messageData;
      messageMeta.textContent = `${messages.length} message(s) cached for quick browsing.`;
    } catch (error) {
      messageMeta.textContent = `Unable to load messages.json (${error.message}).`;
    }

    searchInput.addEventListener("input", applySearch);
    renderMessages(messages);
  } catch (error) {
    manifestOutput.textContent = `Viewer initialization failed: ${error}`;
  }
}

bootstrap();
