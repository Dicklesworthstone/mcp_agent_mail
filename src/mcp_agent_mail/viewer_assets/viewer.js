const manifestOutput = document.getElementById("manifest-json");
const messageList = document.getElementById("message-list");
const searchInput = document.getElementById("search-input");
const messageMeta = document.getElementById("message-meta");

let messages = [];
let databaseMessagesAvailable = false;
const bootstrapStart = performance.now();

async function loadJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to fetch ${path} (${response.status})`);
  }
  return response.json();
}

async function loadBinary(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to fetch ${path} (${response.status})`);
  }
  return new Uint8Array(await response.arrayBuffer());
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
  const preset = manifest.scrub?.preset ? ` • Scrub preset ${manifest.scrub.preset}` : "";
  paragraph.textContent = `Generated ${generated} • Schema ${schema} • Exporter ${exporter}${preset}`;
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
    const note = databaseMessagesAvailable ? "querying sqlite snapshot" : "cached messages.json";
    messageMeta.textContent = `${messages.length} message(s) shown (${note}).`;
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

function formatChunkPath(pattern, index) {
  return pattern.replace(/\{index(?::0?(\d+)d)?\}/, (_match, width) => {
    const targetWidth = width ? Number(width) : 0;
    return index.toString().padStart(targetWidth, "0");
  });
}

async function loadDatabaseBytes(manifest) {
  const dbInfo = manifest.database ?? {};
  const dbPath = dbInfo.path ?? "mailbox.sqlite3";
  const chunkManifest = dbInfo.chunk_manifest;

  if (!chunkManifest) {
    return { bytes: await loadBinary(`../${dbPath}`), source: dbPath };
  }

  const buffers = [];
  let total = 0;
  for (let index = 0; index < chunkManifest.chunk_count; index += 1) {
    const relativeChunk = formatChunkPath(chunkManifest.pattern, index);
    const chunkBytes = await loadBinary(`../${relativeChunk}`);
    buffers.push(chunkBytes);
    total += chunkBytes.length;
  }

  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of buffers) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return { bytes: merged, source: `${chunkManifest.pattern} (${chunkManifest.chunk_count} chunks)` };
}

const sqlJsConfig = {
  locateFile(file) {
    return `./vendor/${file}`;
  },
};

async function ensureSqlJsLoaded() {
  if (window.initSqlJs) {
    return window.initSqlJs(sqlJsConfig);
  }
  await new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-sqljs="true"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", (event) => reject(new Error(`Failed to load sql-wasm.js: ${event.message}`)), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = "./vendor/sql-wasm.js";
    script.async = true;
    script.dataset.sqljs = "true";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Failed to load sql-wasm.js"));
    document.head.append(script);
  });
  if (!window.initSqlJs) {
    throw new Error("sql.js failed to initialise (initSqlJs missing)");
  }
  return window.initSqlJs(sqlJsConfig);
}

function queryMessagesFromDatabase(db, limit = 500) {
  const results = [];
  const statement = db.prepare(
    `SELECT id, subject, created_ts, importance, substr(coalesce(body_md, ''), 1, 280) AS snippet
     FROM messages
     ORDER BY created_ts DESC
     LIMIT ?`
  );
  try {
    statement.bind([limit]);
    while (statement.step()) {
      const row = statement.getAsObject();
      results.push({ ...row });
    }
  } finally {
    statement.free();
  }
  return results;
}

async function tryLoadMessagesFromDatabase(manifest) {
  try {
    const SQL = await ensureSqlJsLoaded();
    messageMeta.textContent = "Loading SQLite snapshot…";
    const { bytes, source } = await loadDatabaseBytes(manifest);
    const db = new SQL.Database(bytes);
    try {
      const rows = queryMessagesFromDatabase(db, 500);
      databaseMessagesAvailable = true;
      messageMeta.textContent = `${rows.length} message(s) queried directly from ${source}.`;
      return rows;
    } finally {
      db.close();
    }
  } catch (error) {
    console.warn("Falling back to cached messages.json:", error);
    databaseMessagesAvailable = false;
    return null;
  }
}

async function bootstrap() {
  try {
    const manifest = await loadJSON("../manifest.json");
    renderManifest(manifest);
    renderBundleInfo(manifest);
    renderProjects(manifest);
    renderAttachments(manifest);
    renderScrub(manifest);

    let hydrated = await tryLoadMessagesFromDatabase(manifest);
    if (!hydrated) {
      try {
        const messageData = await loadJSON("./data/messages.json");
        hydrated = messageData;
        messageMeta.textContent = `${hydrated.length} message(s) loaded from cached messages.json.`;
      } catch (error) {
        messageMeta.textContent = `Unable to load messages.json (${error.message}).`;
        hydrated = [];
      }
    }

    messages = hydrated;
    searchInput.addEventListener("input", applySearch);
    applySearch();
    const durationMs = Math.round(performance.now() - bootstrapStart);
    console.info("[viewer] bootstrap complete", {
      durationMs,
      databaseMessagesAvailable,
      messageCount: messages.length,
    });
  } catch (error) {
    manifestOutput.textContent = `Viewer initialization failed: ${error}`;
  }
}

bootstrap();
