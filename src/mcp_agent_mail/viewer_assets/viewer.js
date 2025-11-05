const manifestOutput = document.getElementById("manifest-json");
const projectsList = document.getElementById("projects-list");
const attachmentStatsList = document.getElementById("attachment-stats");
const scrubStatsList = document.getElementById("scrub-stats");
const bundleInfo = document.getElementById("bundle-info");
const threadListEl = document.getElementById("thread-list");
const threadFilterInput = document.getElementById("thread-filter");
const messageListEl = document.getElementById("message-list");
const messageMetaEl = document.getElementById("message-meta");
const messageDetailEl = document.getElementById("message-detail");
const searchInput = document.getElementById("search-input");
const cacheToggle = document.getElementById("cache-toggle");
const engineStatus = document.getElementById("engine-status");
const clearDetailButton = document.getElementById("clear-detail");

const bootstrapStart = performance.now();
const CACHE_SUPPORTED = typeof navigator.storage?.getDirectory === "function";
const CACHE_PREFIX = "mailbox-snapshot";

const state = {
  manifest: null,
  SQL: null,
  db: null,
  threads: [],
  filteredThreads: [],
  selectedThread: "all",
  messages: [],
  messagesContext: "inbox",
  searchTerm: "",
  ftsEnabled: false,
  totalMessages: 0,
  projectMap: new Map(),
  cacheKey: null,
  cacheState: CACHE_SUPPORTED ? "none" : "unsupported",
  lastDatabaseBytes: null,
  databaseSource: "network",
  selectedMessageId: undefined,
};

function escapeHtml(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlightText(text, term) {
  if (!term) {
    return escapeHtml(text);
  }
  const safeTerm = escapeRegExp(term);
  const regex = new RegExp(`(${safeTerm})`, "gi");
  return escapeHtml(text).replace(regex, "<mark>$1</mark>");
}

function renderProjects(manifest) {
  projectsList.innerHTML = "";
  const entries = manifest.project_scope?.included ?? [];
  if (!entries.length) {
    const li = document.createElement("li");
    li.textContent = "All projects";
    projectsList.append(li);
    return;
  }
  for (const entry of entries) {
    const li = document.createElement("li");
    li.innerHTML = `<strong>${escapeHtml(entry.slug)}</strong> <span>${escapeHtml(entry.human_key)}</span>`;
    projectsList.append(li);
  }
}

function renderScrub(manifest) {
  const scrub = manifest.scrub ?? {};
  scrubStatsList.innerHTML = "";
  for (const [key, value] of Object.entries(scrub)) {
    const dt = document.createElement("dt");
    dt.textContent = key.replace(/_/g, " ");
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    scrubStatsList.append(dt, dd);
  }
}

function renderAttachments(manifest) {
  const stats = manifest.attachments?.stats ?? {};
  const config = manifest.attachments?.config ?? {};
  attachmentStatsList.innerHTML = "";
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
    attachmentStatsList.append(dt, dd);
  }
}

function renderBundleInfo(manifest) {
  const generated = manifest.generated_at ?? "";
  const schema = manifest.schema_version ?? "";
  const exporter = manifest.exporter_version ?? "";
  const scrubPreset = manifest.scrub?.preset ? ` • Scrub preset ${manifest.scrub.preset}` : "";
  bundleInfo.textContent = `Generated ${generated} • Schema ${schema} • Exporter ${exporter}${scrubPreset}`;
}

function renderManifest(manifest) {
  manifestOutput.textContent = JSON.stringify(manifest, null, 2);
}

function updateEngineStatus() {
  const parts = ["Engine: sql.js", state.ftsEnabled ? "FTS5 enabled" : "LIKE fallback"];
  if (CACHE_SUPPORTED) {
    const cacheLabel = state.cacheState === "opfs" ? "OPFS" : state.cacheState === "memory" ? "session" : "none";
    parts.push(`Cache: ${cacheLabel}`);
  } else {
    parts.push("Cache: unsupported");
  }
  engineStatus.textContent = parts.join(" • ");
}

function updateCacheToggle() {
  if (!CACHE_SUPPORTED) {
    cacheToggle.disabled = true;
    cacheToggle.textContent = "Offline caching unavailable";
    return;
  }
  cacheToggle.disabled = !state.cacheKey;
  cacheToggle.textContent = state.cacheState === "opfs" ? "Remove local cache" : "Cache for offline use";
}

async function getOpfsRoot() {
  if (!CACHE_SUPPORTED) {
    return null;
  }
  try {
    return await navigator.storage.getDirectory();
  } catch (error) {
    console.warn("OPFS not accessible", error);
    state.cacheState = "unsupported";
    return null;
  }
}

async function readFromOpfs(key) {
  const root = await getOpfsRoot();
  if (!root) {
    return null;
  }
  try {
    const handle = await root.getFileHandle(`${CACHE_PREFIX}-${key}.sqlite3`);
    const file = await handle.getFile();
    const buffer = await file.arrayBuffer();
    return new Uint8Array(buffer);
  } catch {
    return null;
  }
}

async function writeToOpfs(key, bytes) {
  const root = await getOpfsRoot();
  if (!root) {
    return false;
  }
  try {
    await navigator.storage?.persist?.();
  } catch (error) {
    console.debug("Unable to request persistent storage", error);
  }
  try {
    const handle = await root.getFileHandle(`${CACHE_PREFIX}-${key}.sqlite3`, { create: true });
    const writable = await handle.createWritable();
    await writable.write(bytes);
    await writable.close();
    return true;
  } catch (error) {
    console.warn("Failed to write OPFS cache", error);
    return false;
  }
}

async function removeFromOpfs(key) {
  const root = await getOpfsRoot();
  if (!root) {
    return;
  }
  try {
    await root.removeEntry(`${CACHE_PREFIX}-${key}.sqlite3`);
  } catch (error) {
    console.debug("No cached file to remove", error);
  }
}

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

function formatChunkPath(pattern, index) {
  return pattern.replace(/\{index(?::0?(\d+)d)?\}/, (_match, width) => {
    const targetWidth = width ? Number(width) : 0;
    return String(index).padStart(targetWidth, "0");
  });
}

async function fetchDatabaseFromNetwork(manifest) {
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

async function loadDatabaseBytes(manifest) {
  const sha = manifest.database?.sha256;
  const fallbackKey = manifest.database?.path && manifest.database?.size_bytes
    ? `${manifest.database.path}:${manifest.database.size_bytes}`
    : null;
  state.cacheKey = sha || fallbackKey;

  if (CACHE_SUPPORTED && state.cacheKey) {
    const cached = await readFromOpfs(state.cacheKey);
    if (cached) {
      state.cacheState = "opfs";
      state.lastDatabaseBytes = cached;
      state.databaseSource = "opfs cache";
      return { bytes: cached, source: "OPFS cache" };
    }
  }

  const network = await fetchDatabaseFromNetwork(manifest);
  state.lastDatabaseBytes = network.bytes;
  state.databaseSource = network.source;
  if (state.cacheState !== "opfs") {
    state.cacheState = CACHE_SUPPORTED ? "memory" : "none";
  }
  return network;
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

function getScalar(db, sql, params = []) {
  const statement = db.prepare(sql);
  try {
    statement.bind(params);
    if (statement.step()) {
      const row = statement.get();
      return Array.isArray(row) ? row[0] : Object.values(row)[0];
    }
    return null;
  } finally {
    statement.free();
  }
}

function detectFts(db) {
  try {
    const statement = db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='fts_messages'");
    try {
      const hasTable = statement.step();
      return hasTable;
    } finally {
      statement.free();
    }
  } catch (error) {
    console.warn("FTS detection failed", error);
    return false;
  }
}

function loadProjectMap(db) {
  const statement = db.prepare("SELECT id, slug, human_key FROM projects");
  try {
    while (statement.step()) {
      const row = statement.getAsObject();
      state.projectMap.set(row.id, {
        slug: row.slug,
        human_key: row.human_key,
      });
    }
  } finally {
    statement.free();
  }
}

function buildThreadList(db, limit = 200) {
  const threads = [];
  const sql = `
    WITH normalized AS (
      SELECT
        id,
        subject,
        COALESCE(body_md, '') AS body_md,
        COALESCE(thread_id, '') AS thread_id,
        created_ts,
        importance,
        project_id
      FROM messages
    ),
    keyed AS (
      SELECT
        CASE WHEN thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
        *
      FROM normalized
    )
    SELECT
      thread_key,
      COUNT(*) AS message_count,
      MAX(created_ts) AS last_created_ts,
      (
        SELECT subject FROM keyed k2
        WHERE k2.thread_key = k.thread_key
        ORDER BY datetime(k2.created_ts) DESC, k2.id DESC
        LIMIT 1
      ) AS latest_subject,
      (
        SELECT importance FROM keyed k2
        WHERE k2.thread_key = k.thread_key
        ORDER BY datetime(k2.created_ts) DESC, k2.id DESC
        LIMIT 1
      ) AS latest_importance,
      (
        SELECT substr(body_md, 1, 160) FROM keyed k2
        WHERE k2.thread_key = k.thread_key
        ORDER BY datetime(k2.created_ts) DESC, k2.id DESC
        LIMIT 1
      ) AS latest_snippet
    FROM keyed k
    GROUP BY thread_key
    ORDER BY datetime(last_created_ts) DESC
    LIMIT ?;
  `;

  const statement = db.prepare(sql);
  try {
    statement.bind([limit]);
    while (statement.step()) {
      threads.push(statement.getAsObject());
    }
  } finally {
    statement.free();
  }
  return threads;
}

function formatTimestamp(value) {
  if (!value) {
    return "(unknown)";
  }
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return `${date.toLocaleDateString()} ${date.toLocaleTimeString()}`;
  } catch {
    return value;
  }
}

function renderThreads(threads) {
  threadListEl.innerHTML = "";

  const renderEntry = (thread) => {
    const li = document.createElement("li");
    li.className = "thread-item";
    li.tabIndex = 0;
    li.dataset.threadKey = thread.thread_key;
    if (thread.thread_key === state.selectedThread) {
      li.classList.add("active");
    }

    const title = document.createElement("h3");
    title.innerHTML = thread.thread_key === "all"
      ? "All messages"
      : escapeHtml(thread.latest_subject || "(no subject)");

    const meta = document.createElement("div");
    meta.className = "thread-meta";
    meta.innerHTML = `
      <span class="pill">${thread.message_count} msg</span>
      <span>${thread.last_created_ts ? formatTimestamp(thread.last_created_ts) : ""}</span>
    `;

    const preview = document.createElement("div");
    preview.className = "thread-preview";
    preview.innerHTML = highlightText(thread.latest_snippet || "", state.searchTerm);

    li.append(title, meta, preview);
    threadListEl.append(li);
  };

  renderEntry({ thread_key: "all", message_count: state.totalMessages, last_created_ts: threads[0]?.last_created_ts || null, latest_subject: "All messages", latest_snippet: "" });
  for (const thread of threads) {
    renderEntry(thread);
  }
}

function getThreadMessages(threadKey, limit = 200) {
  const results = [];
  let statement;
  if (threadKey === "all") {
    statement = state.db.prepare(
      `SELECT id, subject, created_ts, importance,
              CASE WHEN thread_id IS NULL OR thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
              substr(COALESCE(body_md, ''), 1, 280) AS snippet
       FROM messages
       ORDER BY datetime(created_ts) DESC, id DESC
       LIMIT ?`
    );
    statement.bind([limit]);
  } else {
    statement = state.db.prepare(
      `SELECT id, subject, created_ts, importance,
              CASE WHEN thread_id IS NULL OR thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
              substr(COALESCE(body_md, ''), 1, 280) AS snippet
       FROM messages
       WHERE (thread_id = ?) OR (thread_id IS NULL AND printf('msg:%d', id) = ?)
       ORDER BY datetime(created_ts) ASC, id ASC`
    );
    statement.bind([threadKey, threadKey]);
  }

  try {
    while (statement.step()) {
      results.push(statement.getAsObject());
    }
  } finally {
    statement.free();
  }
  return results;
}

function renderMessages(list, { context, term }) {
  messageListEl.innerHTML = "";

  if (!list.length) {
    const empty = document.createElement("li");
    empty.textContent = context === "search" ? "No messages match your query." : "No messages available.";
    messageListEl.append(empty);
    return;
  }

  for (const message of list) {
    const item = document.createElement("li");
    item.dataset.id = String(message.id);
    item.dataset.threadKey = message.thread_key;
    if (Number(state.selectedMessageId) === Number(message.id)) {
      item.classList.add("active");
    }

    const title = document.createElement("h3");
    title.innerHTML = highlightText(message.subject || "(no subject)", term);

    const meta = document.createElement("div");
    meta.className = "message-meta-line";
    meta.innerHTML = `${formatTimestamp(message.created_ts)} • importance: ${escapeHtml(message.importance || "normal")}`;

    const snippet = document.createElement("div");
    snippet.className = "message-snippet";
    snippet.innerHTML = highlightText(message.snippet || "", term);

    item.append(title, meta, snippet);
    messageListEl.append(item);
  }
}

function updateMessageMeta({ context, term }) {
  if (context === "search") {
    messageMetaEl.textContent = `${state.messages.length} result(s) for “${term}” (${state.ftsEnabled ? "FTS" : "LIKE"} search)`;
  } else if (context === "thread" && state.selectedThread !== "all") {
    messageMetaEl.textContent = `${state.messages.length} message(s) in thread ${state.selectedThread}`;
  } else {
    const note = state.ftsEnabled ? "FTS ready" : "LIKE fallback";
    messageMetaEl.textContent = `${state.messages.length} message(s) shown (${note}).`;
  }
}

function performSearch(term) {
  const query = term.trim();
  state.searchTerm = query;
  if (query.length < 2) {
    state.messagesContext = state.selectedThread === "all" ? "inbox" : "thread";
    state.messages = getThreadMessages(state.selectedThread);
    renderMessages(state.messages, { context: state.messagesContext, term: "" });
    updateMessageMeta({ context: state.messagesContext, term: "" });
    return;
  }

  let results = [];
  if (state.ftsEnabled) {
    try {
      const stmt = state.db.prepare(
        `SELECT messages.id, messages.subject, messages.created_ts, messages.importance,
                CASE WHEN messages.thread_id IS NULL OR messages.thread_id = '' THEN printf('msg:%d', messages.id) ELSE messages.thread_id END AS thread_key,
                COALESCE(snippet(fts_messages, 1, '<mark>', '</mark>', '…', 32), substr(messages.body_md, 1, 280)) AS snippet
         FROM fts_messages
         JOIN messages ON messages.id = fts_messages.rowid
         WHERE fts_messages MATCH ?
         ORDER BY datetime(messages.created_ts) DESC
         LIMIT 100`
      );
      stmt.bind([query]);
      while (stmt.step()) {
        const row = stmt.getAsObject();
        results.push({
          ...row,
          snippet: row.snippet?.replace(/<mark>/g, "").replace(/<\/mark>/g, "") ?? "",
        });
      }
      stmt.free();
    } catch (error) {
      console.warn("FTS query failed, falling back to LIKE", error);
      state.ftsEnabled = false;
      results = [];
    }
  }

  if (!state.ftsEnabled) {
    const stmt = state.db.prepare(
      `SELECT id, subject, created_ts, importance,
              CASE WHEN thread_id IS NULL OR thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
              substr(COALESCE(body_md, ''), 1, 280) AS snippet
       FROM messages
       WHERE subject LIKE ? OR body_md LIKE ?
       ORDER BY datetime(created_ts) DESC
       LIMIT 100`
    );
    const pattern = `%${query}%`;
    stmt.bind([pattern, pattern]);
    while (stmt.step()) {
      results.push(stmt.getAsObject());
    }
    stmt.free();
  }

  state.messagesContext = "search";
  state.messages = results;
  renderMessages(state.messages, { context: "search", term: query });
  updateMessageMeta({ context: "search", term: query });
}

function selectThread(threadKey) {
  state.selectedThread = threadKey;
  state.searchTerm = "";
  state.messagesContext = threadKey === "all" ? "inbox" : "thread";
  searchInput.value = "";
  state.messages = getThreadMessages(threadKey);
  state.selectedMessageId = undefined;
  renderThreads(state.filteredThreads.length ? state.filteredThreads : state.threads);
  renderMessages(state.messages, { context: state.messagesContext, term: "" });
  updateMessageMeta({ context: state.messagesContext, term: "" });
  clearMessageDetail();
}

function clearMessageDetail() {
  state.selectedMessageId = undefined;
  messageDetailEl.innerHTML = "<p class='meta-line'>Select a message to inspect subject, body, and attachments.</p>";
  const active = messageListEl.querySelector("li.active");
  if (active) {
    active.classList.remove("active");
  }
}

function getMessageDetail(id) {
  const stmt = state.db.prepare(
    `SELECT m.id, m.subject, m.body_md, m.created_ts, m.importance, m.thread_id, m.project_id,
            m.attachments,
            COALESCE(p.slug, '') AS project_slug,
            COALESCE(p.human_key, '') AS project_name
     FROM messages m
     LEFT JOIN projects p ON p.id = m.project_id
     WHERE m.id = ?`
  );
  try {
    stmt.bind([id]);
    if (stmt.step()) {
      return stmt.getAsObject();
    }
    return null;
  } finally {
    stmt.free();
  }
}

function renderMessageDetail(message) {
  if (!message) {
    clearMessageDetail();
    return;
  }

  messageDetailEl.innerHTML = "";

  const header = document.createElement("header");
  header.innerHTML = `
    <h3>${escapeHtml(message.subject || "(no subject)")}</h3>
    <div class="meta-line">${formatTimestamp(message.created_ts)} • importance: ${escapeHtml(message.importance || "normal")} • project: ${escapeHtml(message.project_slug || "-")}</div>
  `;

  const body = document.createElement("div");
  body.className = "message-snippet";
  body.innerHTML = highlightText(message.body_md || "(empty body)", state.searchTerm);

  messageDetailEl.append(header, body);

  if (message.attachments) {
    try {
      const data = typeof message.attachments === "string" ? JSON.parse(message.attachments || "[]") : message.attachments;
      if (Array.isArray(data) && data.length) {
        const list = document.createElement("ul");
        list.className = "attachment-list";
        for (const entry of data) {
          const li = document.createElement("li");
          const mode = entry.type || entry.mode || "file";
          const label = entry.media_type || "application/octet-stream";
          li.innerHTML = `<strong>${escapeHtml(mode)}</strong> – ${escapeHtml(label)}`;
          if (entry.sha256) {
            li.innerHTML += `<br /><span class="meta-line">sha256: ${escapeHtml(entry.sha256)}</span>`;
          }
          if (entry.path) {
            li.innerHTML += `<br /><span class="meta-line">Path: ${escapeHtml(entry.path)}</span>`;
          }
          if (entry.original_path) {
            li.innerHTML += `<br /><span class="meta-line">Original: ${escapeHtml(entry.original_path)}</span>`;
          }
          list.append(li);
        }
        const attachmentsHeader = document.createElement("h4");
        attachmentsHeader.textContent = "Attachments";
        messageDetailEl.append(attachmentsHeader, list);
      }
    } catch (error) {
      console.warn("Failed to parse attachments", error);
    }
  }
}

function handleMessageSelection(event) {
  const item = event.target.closest("li[data-id]");
  if (!item) {
    return;
  }
  const id = Number(item.dataset.id);
  state.selectedMessageId = id;
  messageListEl.querySelectorAll("li.active").forEach((el) => el.classList.remove("active"));
  item.classList.add("active");
  const detail = getMessageDetail(id);
  renderMessageDetail(detail);
}

function filterThreads(term) {
  const value = term.trim().toLowerCase();
  if (!value) {
    state.filteredThreads = state.threads;
  } else {
    state.filteredThreads = state.threads.filter((thread) => {
      if (thread.thread_key === "all") {
        return true;
      }
      return (
        (thread.latest_subject && thread.latest_subject.toLowerCase().includes(value)) ||
        (thread.latest_snippet && thread.latest_snippet.toLowerCase().includes(value)) ||
        thread.thread_key.toLowerCase().includes(value)
      );
    });
  }
  renderThreads(state.filteredThreads);
}

async function bootstrap() {
  try {
    const manifest = await loadJSON("../manifest.json");
    state.manifest = manifest;
    renderManifest(manifest);
    renderProjects(manifest);
    renderScrub(manifest);
    renderAttachments(manifest);
    renderBundleInfo(manifest);

    const { bytes, source } = await loadDatabaseBytes(manifest);
    state.databaseSource = source;
    updateCacheToggle();

    state.SQL = await ensureSqlJsLoaded();
    state.db = new state.SQL.Database(bytes);
    state.ftsEnabled = Boolean(manifest.database?.fts_enabled) && detectFts(state.db);
    updateEngineStatus();

    state.totalMessages = Number(getScalar(state.db, "SELECT COUNT(*) FROM messages") || 0);
    loadProjectMap(state.db);
    state.threads = buildThreadList(state.db);
    state.filteredThreads = state.threads;
    renderThreads(state.filteredThreads);

    state.messages = getThreadMessages("all");
    state.messagesContext = "inbox";
    renderMessages(state.messages, { context: "inbox", term: "" });
    updateMessageMeta({ context: "inbox", term: "" });

    clearMessageDetail();

    const durationMs = Math.round(performance.now() - bootstrapStart);
    console.info("[viewer] bootstrap complete", {
      durationMs,
      ftsEnabled: state.ftsEnabled,
      totalMessages: state.totalMessages,
      databaseSource: state.databaseSource,
      cacheState: state.cacheState,
    });
  } catch (error) {
    console.error(error);
    manifestOutput.textContent = `Viewer initialization failed: ${error}`;
  }
}

threadListEl.addEventListener("click", (event) => {
  const item = event.target.closest("li.thread-item");
  if (!item) {
    return;
  }
  const threadKey = item.dataset.threadKey;
  if (threadKey) {
    state.threads = state.filteredThreads;
    selectThread(threadKey);
  }
});

threadListEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    const item = event.target.closest("li.thread-item");
    if (item?.dataset.threadKey) {
      selectThread(item.dataset.threadKey);
      event.preventDefault();
    }
  }
});

messageListEl.addEventListener("click", handleMessageSelection);

searchInput.addEventListener("input", (event) => {
  performSearch(event.target.value);
});

threadFilterInput.addEventListener("input", (event) => {
  filterThreads(event.target.value);
});

cacheToggle.addEventListener("click", async () => {
  if (!CACHE_SUPPORTED || !state.cacheKey) {
    return;
  }
  cacheToggle.disabled = true;
  try {
    if (state.cacheState === "opfs") {
      await removeFromOpfs(state.cacheKey);
      state.cacheState = state.lastDatabaseBytes ? "memory" : "none";
    } else if (state.lastDatabaseBytes) {
      const success = await writeToOpfs(state.cacheKey, state.lastDatabaseBytes);
      if (success) {
        state.cacheState = "opfs";
      }
    }
  } finally {
    updateCacheToggle();
    updateEngineStatus();
    cacheToggle.disabled = false;
  }
});

clearDetailButton.addEventListener("click", () => {
  clearMessageDetail();
});

bootstrap();
