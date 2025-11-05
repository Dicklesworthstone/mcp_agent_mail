const manifestOutput = document.getElementById("manifest-json");
const projectsList = document.getElementById("projects-list");
const attachmentStatsList = document.getElementById("attachment-stats");
const scrubStatsList = document.getElementById("scrub-stats");
const bundleInfo = document.getElementById("bundle-info");
const threadListEl = document.getElementById("thread-list");
const threadScrollEl = document.getElementById("thread-scroll");
const threadFilterInput = document.getElementById("thread-filter");
const messageListEl = document.getElementById("message-list");
const messageScrollEl = document.getElementById("message-scroll");
const messageMetaEl = document.getElementById("message-meta");
const messageDetailEl = document.getElementById("message-detail");
const searchInput = document.getElementById("search-input");
const cacheToggle = document.getElementById("cache-toggle");
const engineStatus = document.getElementById("engine-status");
const clearDetailButton = document.getElementById("clear-detail");

const bootstrapStart = performance.now();
const CACHE_SUPPORTED = typeof navigator.storage?.getDirectory === "function";
const CACHE_PREFIX = "mailbox-snapshot";
const VIRTUAL_SCROLL_THRESHOLD = 1000; // Use virtual scrolling when items > this

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
  explainMode: false,
  threadClusterize: null,
  messageClusterize: null,
};

// Trusted Types Policy for secure Markdown rendering
// See plan document lines 190-205 for security requirements
let trustedTypesPolicy;
let trustedScriptURLPolicy;
try {
  if (window.trustedTypes) {
    trustedTypesPolicy = trustedTypes.createPolicy("mailViewerDOMPurify", {
      createHTML: (dirty) => {
        // DOMPurify will be loaded from vendor/dompurify.min.js
        if (typeof DOMPurify !== "undefined") {
          return DOMPurify.sanitize(dirty, { RETURN_TRUSTED_TYPE: true });
        }
        // Fallback to basic escaping if DOMPurify not loaded
        console.warn("DOMPurify not available, falling back to basic escaping");
        return escapeHtml(dirty);
      },
      createScriptURL: (url) => {
        // Only allow loading scripts from our vendor directory
        if (url.startsWith("./vendor/") || url.startsWith("/vendor/")) {
          return url;
        }
        throw new Error(`Untrusted script URL: ${url}`);
      },
    });

    trustedScriptURLPolicy = trustedTypesPolicy;

    // Default policy for Clusterize.js compatibility
    // Clusterize uses innerHTML but doesn't understand Trusted Types
    // This policy passes through HTML that we've already escaped in createThreadHTML/createMessageHTML
    trustedTypes.createPolicy("default", {
      createHTML: (dirty) => {
        // For Clusterize.js: HTML is already escaped via escapeHtml() in our rendering functions
        // We verify this is safe because:
        // 1. All user content (subjects, snippets, thread keys) goes through escapeHtml()
        // 2. highlightText() calls escapeHtml() before regex replacement
        // 3. Timestamps are escaped via escapeHtml(formatTimestamp())
        // 4. Numbers (message counts, IDs) are not user-controllable
        // 5. Only static HTML tags (<li>, <h3>, <div>, <span>) and escaped content
        return dirty;
      },
      createScriptURL: (url) => {
        // Only allow loading scripts from our vendor directory
        if (url.startsWith("./vendor/") || url.startsWith("/vendor/")) {
          return url;
        }
        throw new Error(`Untrusted script URL: ${url}`);
      },
    });
  }
} catch (error) {
  console.warn("Trusted Types not supported or policy creation failed:", error);
}

function escapeHtml(value) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Create TrustedHTML from escaped HTML string.
 * @param {string} html - HTML string with escaped entities
 * @returns {string|TrustedHTML} - Trusted HTML ready for innerHTML
 */
function createTrustedHTML(html) {
  if (trustedTypesPolicy) {
    // For simple escaped HTML, we can trust it directly
    // The policy will receive already-escaped HTML, so DOMPurify will pass it through
    return trustedTypesPolicy.createHTML(html);
  }
  // Fallback for browsers without Trusted Types
  return html;
}

/**
 * Render Markdown safely using Marked + DOMPurify + Trusted Types.
 * @param {string} markdown - Raw markdown text
 * @returns {string|TrustedHTML} - Sanitized HTML ready for innerHTML
 */
function renderMarkdownSafe(markdown) {
  if (!markdown) {
    return trustedTypesPolicy ? trustedTypesPolicy.createHTML("") : "";
  }

  // If Marked.js is available, parse Markdown
  let html;
  if (typeof marked !== "undefined") {
    try {
      html = marked.parse(markdown, {
        breaks: true, // GFM line breaks
        gfm: true, // GitHub Flavored Markdown
        headerIds: false, // Disable auto-generated IDs for security
        mangle: false, // Don't mangle email addresses
      });
    } catch (error) {
      console.error("Marked parsing error:", error);
      html = escapeHtml(markdown);
    }
  } else {
    // Fallback: treat as plain text
    html = escapeHtml(markdown).replace(/\n/g, "<br>");
  }

  // Sanitize with DOMPurify + Trusted Types
  if (trustedTypesPolicy) {
    return trustedTypesPolicy.createHTML(html);
  }

  // Fallback for browsers without Trusted Types
  if (typeof DOMPurify !== "undefined") {
    return DOMPurify.sanitize(html);
  }

  // Last resort: return escaped HTML
  console.warn("No sanitization available, returning escaped text");
  return escapeHtml(markdown);
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
    li.innerHTML = createTrustedHTML(`<strong>${escapeHtml(entry.slug)}</strong> <span>${escapeHtml(entry.human_key)}</span>`);
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
    // Write the database file
    const handle = await root.getFileHandle(`${CACHE_PREFIX}-${key}.sqlite3`, { create: true });
    const writable = await handle.createWritable();
    await writable.write(bytes);
    await writable.close();

    // Write version metadata for cache invalidation
    const metaHandle = await root.getFileHandle(`${CACHE_PREFIX}-${key}.meta.json`, { create: true });
    const metaWritable = await metaHandle.createWritable();
    const metadata = {
      cacheKey: key,
      cachedAt: new Date().toISOString(),
      version: 1,
    };
    await metaWritable.write(JSON.stringify(metadata));
    await metaWritable.close();

    return true;
  } catch (error) {
    console.warn("Failed to write OPFS cache", error);
    return false;
  }
}

async function readOpfsMetadata(key) {
  const root = await getOpfsRoot();
  if (!root) {
    return null;
  }
  try {
    const handle = await root.getFileHandle(`${CACHE_PREFIX}-${key}.meta.json`);
    const file = await handle.getFile();
    const text = await file.text();
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function removeFromOpfs(key) {
  const root = await getOpfsRoot();
  if (!root) {
    return;
  }
  try {
    await root.removeEntry(`${CACHE_PREFIX}-${key}.sqlite3`);
    await root.removeEntry(`${CACHE_PREFIX}-${key}.meta.json`);
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
      // Check cache version to ensure it matches current manifest
      const metadata = await readOpfsMetadata(state.cacheKey);
      if (metadata && metadata.cacheKey === state.cacheKey) {
        console.info("[viewer] Using OPFS cache", { key: state.cacheKey, cachedAt: metadata.cachedAt });
        state.cacheState = "opfs";
        state.lastDatabaseBytes = cached;
        state.databaseSource = "opfs cache";
        return { bytes: cached, source: "OPFS cache" };
      } else {
        // Stale cache detected - invalidate and fetch fresh
        console.warn("[viewer] Stale OPFS cache detected, invalidating", {
          cached: metadata?.cacheKey,
          current: state.cacheKey
        });
        await removeFromOpfs(state.cacheKey);
        if (metadata?.cacheKey) {
          await removeFromOpfs(metadata.cacheKey);
        }
      }
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
    const scriptURL = "./vendor/sql-wasm.js";
    // Use Trusted Types policy if available
    script.src = trustedScriptURLPolicy
      ? trustedScriptURLPolicy.createScriptURL(scriptURL)
      : scriptURL;
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

/**
 * Execute EXPLAIN QUERY PLAN for a SQL query and log the results.
 * @param {Database} db - sql.js database instance
 * @param {string} sql - The SQL query to explain
 * @param {Array} params - Query parameters
 * @param {string} label - Label for console output
 */
function explainQuery(db, sql, params = [], label = "Query") {
  if (!state.explainMode || !db) {
    return;
  }

  let statement;
  try {
    const explainSql = `EXPLAIN QUERY PLAN ${sql}`;
    statement = db.prepare(explainSql);
    statement.bind(params);

    const plan = [];
    while (statement.step()) {
      const row = statement.getAsObject();
      plan.push(row);
    }

    if (plan.length > 0) {
      console.group(`[EXPLAIN] ${label}`);
      console.log("Query:", sql.substring(0, 200) + (sql.length > 200 ? "..." : ""));
      if (params.length > 0) {
        console.log("Params:", params);
      }
      console.table(plan);
      console.groupEnd();
    }
  } catch (error) {
    console.warn(`[EXPLAIN] Failed to explain query: ${label}`, error);
  } finally {
    if (statement) {
      statement.free();
    }
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

function buildThreadList(db, limit = 50000) {
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

  explainQuery(db, sql, [limit], "buildThreadList");

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
  // Helper to create HTML string for a thread item
  const createThreadHTML = (thread) => {
    const isActive = thread.thread_key === state.selectedThread;
    const activeClass = isActive ? ' active' : '';
    const subject = thread.thread_key === "all"
      ? "All messages"
      : escapeHtml(thread.latest_subject || "(no subject)");
    const timestamp = thread.last_created_ts ? escapeHtml(formatTimestamp(thread.last_created_ts)) : "";
    const snippet = highlightText(thread.latest_snippet || "", state.searchTerm);

    return `<li class="thread-item${activeClass}" tabindex="0" data-thread-key="${escapeHtml(thread.thread_key)}">
      <h3>${subject}</h3>
      <div class="thread-meta">
        <span class="pill">${thread.message_count} msg</span>
        <span>${timestamp}</span>
      </div>
      <div class="thread-preview">${snippet}</div>
    </li>`;
  };

  // Build all threads array (including "All messages" entry)
  const allThreads = [
    {
      thread_key: "all",
      message_count: state.totalMessages,
      last_created_ts: threads[0]?.last_created_ts || null,
      latest_subject: "All messages",
      latest_snippet: ""
    },
    ...threads
  ];

  const totalCount = allThreads.length;

  // Use virtual scrolling if items exceed threshold
  if (totalCount > VIRTUAL_SCROLL_THRESHOLD) {
    // Show scroll container, hide skeleton
    threadScrollEl.classList.remove("hidden");
    document.getElementById("thread-skeleton").classList.add("hidden");

    const rows = allThreads.map(thread => createThreadHTML(thread));

    if (state.threadClusterize) {
      // Update existing Clusterize instance
      state.threadClusterize.update(rows);
    } else {
      // Initialize new Clusterize instance
      state.threadClusterize = new Clusterize({
        rows: rows,
        scrollElem: threadScrollEl,
        contentElem: threadListEl,
        rows_in_block: 20,
        blocks_in_cluster: 2,
        tag: "li"  // Use <li> for spacing rows to maintain valid HTML structure
      });
    }
  } else {
    // Direct DOM rendering for small lists
    threadScrollEl.classList.remove("hidden");
    document.getElementById("thread-skeleton").classList.add("hidden");

    // Destroy Clusterize if it exists
    if (state.threadClusterize) {
      state.threadClusterize.destroy(true);
      state.threadClusterize = null;
    }

    threadListEl.innerHTML = "";
    for (const thread of allThreads) {
      const li = document.createElement("li");
      li.className = "thread-item";
      li.tabIndex = 0;
      li.dataset.threadKey = thread.thread_key;
      if (thread.thread_key === state.selectedThread) {
        li.classList.add("active");
      }

      const title = document.createElement("h3");
      title.innerHTML = createTrustedHTML(thread.thread_key === "all"
        ? "All messages"
        : escapeHtml(thread.latest_subject || "(no subject)"));

      const meta = document.createElement("div");
      meta.className = "thread-meta";
      meta.innerHTML = createTrustedHTML(`
        <span class="pill">${thread.message_count} msg</span>
        <span>${thread.last_created_ts ? escapeHtml(formatTimestamp(thread.last_created_ts)) : ""}</span>
      `);

      const preview = document.createElement("div");
      preview.className = "thread-preview";
      preview.innerHTML = createTrustedHTML(highlightText(thread.latest_snippet || "", state.searchTerm));

      li.append(title, meta, preview);
      threadListEl.append(li);
    }
  }
}

function getThreadMessages(threadKey, limit = 50000) {
  const results = [];
  let statement;
  let sql;
  let params;

  if (threadKey === "all") {
    sql = `SELECT id, subject, created_ts, importance,
              CASE WHEN thread_id IS NULL OR thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
              substr(COALESCE(body_md, ''), 1, 280) AS snippet
       FROM messages
       ORDER BY datetime(created_ts) DESC, id DESC
       LIMIT ?`;
    params = [limit];
    explainQuery(state.db, sql, params, "getThreadMessages (all)");
    statement = state.db.prepare(sql);
    statement.bind(params);
  } else {
    sql = `SELECT id, subject, created_ts, importance,
              CASE WHEN thread_id IS NULL OR thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
              substr(COALESCE(body_md, ''), 1, 280) AS snippet
       FROM messages
       WHERE (thread_id = ?) OR (thread_id IS NULL AND printf('msg:%d', id) = ?)
       ORDER BY datetime(created_ts) ASC, id ASC`;
    params = [threadKey, threadKey];
    explainQuery(state.db, sql, params, "getThreadMessages (specific)");
    statement = state.db.prepare(sql);
    statement.bind(params);
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
  if (!list.length) {
    // Destroy Clusterize if it exists
    if (state.messageClusterize) {
      state.messageClusterize.destroy(true);
      state.messageClusterize = null;
    }

    messageScrollEl.classList.remove("hidden");
    document.getElementById("message-skeleton").classList.add("hidden");
    messageListEl.innerHTML = "";

    const empty = document.createElement("li");
    empty.textContent = context === "search" ? "No messages match your query." : "No messages available.";
    messageListEl.append(empty);
    return;
  }

  // Helper to create HTML string for a message item
  const createMessageHTML = (message) => {
    const isActive = Number(state.selectedMessageId) === Number(message.id);
    const activeClass = isActive ? ' active' : '';
    const subject = highlightText(message.subject || "(no subject)", term);
    const timestamp = escapeHtml(formatTimestamp(message.created_ts));
    const importance = escapeHtml(message.importance || "normal");
    const snippet = highlightText(message.snippet || "", term);

    return `<li class="${activeClass}" data-id="${escapeHtml(String(message.id))}" data-thread-key="${escapeHtml(message.thread_key)}">
      <h3>${subject}</h3>
      <div class="message-meta-line">${timestamp} • importance: ${importance}</div>
      <div class="message-snippet">${snippet}</div>
    </li>`;
  };

  const totalCount = list.length;

  // Use virtual scrolling if items exceed threshold
  if (totalCount > VIRTUAL_SCROLL_THRESHOLD) {
    // Show scroll container, hide skeleton
    messageScrollEl.classList.remove("hidden");
    document.getElementById("message-skeleton").classList.add("hidden");

    const rows = list.map(message => createMessageHTML(message));

    if (state.messageClusterize) {
      // Update existing Clusterize instance
      state.messageClusterize.update(rows);
    } else {
      // Initialize new Clusterize instance
      state.messageClusterize = new Clusterize({
        rows: rows,
        scrollElem: messageScrollEl,
        contentElem: messageListEl,
        rows_in_block: 20,
        blocks_in_cluster: 2,
        tag: "li"  // Use <li> for spacing rows to maintain valid HTML structure
      });
    }
  } else {
    // Direct DOM rendering for small lists
    messageScrollEl.classList.remove("hidden");
    document.getElementById("message-skeleton").classList.add("hidden");

    // Destroy Clusterize if it exists
    if (state.messageClusterize) {
      state.messageClusterize.destroy(true);
      state.messageClusterize = null;
    }

    messageListEl.innerHTML = "";
    for (const message of list) {
      const item = document.createElement("li");
      item.dataset.id = String(message.id);
      item.dataset.threadKey = message.thread_key;
      if (Number(state.selectedMessageId) === Number(message.id)) {
        item.classList.add("active");
      }

      const title = document.createElement("h3");
      title.innerHTML = createTrustedHTML(highlightText(message.subject || "(no subject)", term));

      const meta = document.createElement("div");
      meta.className = "message-meta-line";
      meta.innerHTML = createTrustedHTML(`${escapeHtml(formatTimestamp(message.created_ts))} • importance: ${escapeHtml(message.importance || "normal")}`);

      const snippet = document.createElement("div");
      snippet.className = "message-snippet";
      snippet.innerHTML = createTrustedHTML(highlightText(message.snippet || "", term));

      item.append(title, meta, snippet);
      messageListEl.append(item);
    }
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
      const ftsSql = `SELECT messages.id, messages.subject, messages.created_ts, messages.importance,
                CASE WHEN messages.thread_id IS NULL OR messages.thread_id = '' THEN printf('msg:%d', messages.id) ELSE messages.thread_id END AS thread_key,
                COALESCE(snippet(fts_messages, 1, '<mark>', '</mark>', '…', 32), substr(messages.body_md, 1, 280)) AS snippet
         FROM fts_messages
         JOIN messages ON messages.id = fts_messages.rowid
         WHERE fts_messages MATCH ?
         ORDER BY datetime(messages.created_ts) DESC
         LIMIT 10000`;
      explainQuery(state.db, ftsSql, [query], "performSearch (FTS)");
      const stmt = state.db.prepare(ftsSql);
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
    const likeSql = `SELECT id, subject, created_ts, importance,
              CASE WHEN thread_id IS NULL OR thread_id = '' THEN printf('msg:%d', id) ELSE thread_id END AS thread_key,
              substr(COALESCE(body_md, ''), 1, 280) AS snippet
       FROM messages
       WHERE subject LIKE ? OR body_md LIKE ?
       ORDER BY datetime(created_ts) DESC
       LIMIT 10000`;
    const pattern = `%${query}%`;
    explainQuery(state.db, likeSql, [pattern, pattern], "performSearch (LIKE)");
    const stmt = state.db.prepare(likeSql);
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
  messageDetailEl.innerHTML = createTrustedHTML("<p class='meta-line'>Select a message to inspect subject, body, and attachments.</p>");
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
  header.innerHTML = createTrustedHTML(`
    <h3>${escapeHtml(message.subject || "(no subject)")}</h3>
    <div class="meta-line">${escapeHtml(formatTimestamp(message.created_ts))} • importance: ${escapeHtml(message.importance || "normal")} • project: ${escapeHtml(message.project_slug || "-")}</div>
  `);

  const body = document.createElement("div");
  body.className = "message-snippet";
  // Render markdown safely using DOMPurify + Marked.js + Trusted Types
  // Note: Search highlighting on rendered HTML is complex (requires text-node-only highlighting)
  // For now, we prioritize secure markdown rendering over search highlighting in detail view
  body.innerHTML = renderMarkdownSafe(message.body_md || "(empty body)");

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

          // Build complete HTML string, then create TrustedHTML once
          let html = `<strong>${escapeHtml(mode)}</strong> – ${escapeHtml(label)}`;
          if (entry.sha256) {
            html += `<br /><span class="meta-line">sha256: ${escapeHtml(entry.sha256)}</span>`;
          }
          if (entry.path) {
            html += `<br /><span class="meta-line">Path: ${escapeHtml(entry.path)}</span>`;
          }
          if (entry.original_path) {
            html += `<br /><span class="meta-line">Original: ${escapeHtml(entry.original_path)}</span>`;
          }
          li.innerHTML = createTrustedHTML(html);
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

/**
 * Show skeleton loading screens while data loads.
 */
function showSkeletons() {
  const threadSkeleton = document.getElementById('thread-skeleton');
  const messageSkeleton = document.getElementById('message-skeleton');
  const threadList = document.getElementById('thread-list');
  const messageList = document.getElementById('message-list');

  if (threadSkeleton) threadSkeleton.classList.remove('hidden');
  if (messageSkeleton) messageSkeleton.classList.remove('hidden');
  if (threadList) threadList.classList.add('hidden');
  if (messageList) messageList.classList.add('hidden');
}

/**
 * Hide skeleton loading screens and show actual content.
 */
function hideSkeletons() {
  const threadSkeleton = document.getElementById('thread-skeleton');
  const messageSkeleton = document.getElementById('message-skeleton');
  const threadList = document.getElementById('thread-list');
  const messageList = document.getElementById('message-list');

  if (threadSkeleton) threadSkeleton.classList.add('hidden');
  if (messageSkeleton) messageSkeleton.classList.add('hidden');
  if (threadList) threadList.classList.remove('hidden');
  if (messageList) messageList.classList.remove('hidden');
}

/**
 * Check if the page is running in a cross-origin isolated context.
 * Display a warning banner if isolation is not available.
 */
function checkCrossOriginIsolation() {
  const isIsolated = window.crossOriginIsolated === true;

  if (!isIsolated) {
    showIsolationWarning();
  }

  return isIsolated;
}

/**
 * Display a warning banner about missing cross-origin isolation.
 */
function showIsolationWarning() {
  const header = document.querySelector('header.banner');
  if (!header) return;

  const warningBanner = document.createElement('div');
  warningBanner.id = 'isolation-warning';
  warningBanner.className = 'warning-banner';
  warningBanner.innerHTML = createTrustedHTML(`
    <strong>⚠️ Cross-Origin Isolation Not Enabled</strong>
    <p>This viewer requires Cross-Origin-Opener-Policy (COOP) and Cross-Origin-Embedder-Policy (COEP) headers for optimal performance.</p>
    <details>
      <summary>How to fix this</summary>
      <ul>
        <li><strong>Cloudflare Pages / Netlify:</strong> The included <code>_headers</code> file should be automatically applied.</li>
        <li><strong>GitHub Pages:</strong> Uncomment the <code>&lt;script src="./coi-serviceworker.js"&gt;&lt;/script&gt;</code> line in <code>index.html</code> and redeploy.</li>
        <li><strong>Other hosts:</strong> Configure your server to send COOP and COEP headers. See <code>HOW_TO_DEPLOY.md</code> for details.</li>
      </ul>
      <p>Without isolation, advanced features like OPFS caching and SharedArrayBuffer may be unavailable.</p>
    </details>
  `);

  header.parentNode.insertBefore(warningBanner, header.nextSibling);
}

async function bootstrap() {
  // Show skeleton loading screens
  showSkeletons();

  // Check for cross-origin isolation and show warning if needed
  checkCrossOriginIsolation();

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

    // Hide skeleton screens and show actual content
    hideSkeletons();

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

// Diagnostics Panel
const diagnosticsPanel = document.getElementById("diagnostics-panel");
const diagnosticsToggle = document.getElementById("diagnostics-toggle");
const closeDiagnostics = document.getElementById("close-diagnostics");
const clearAllCachesButton = document.getElementById("clear-all-caches");
const toggleExplainButton = document.getElementById("toggle-explain");

function updateDiagnostics() {
  // Cross-Origin Isolation
  const isIsolated = window.crossOriginIsolated === true;
  document.getElementById("diag-isolation-status").textContent = isIsolated ? "✅ Enabled" : "❌ Disabled";
  document.getElementById("diag-sab-status").textContent = typeof SharedArrayBuffer !== "undefined" ? "✅ Available" : "❌ Unavailable";
  document.getElementById("diag-opfs-status").textContent = CACHE_SUPPORTED ? "✅ Available" : "❌ Unavailable";

  // Database Engine
  document.getElementById("diag-db-source").textContent = state.databaseSource || "-";
  document.getElementById("diag-db-engine").textContent = "sql.js (WASM)";
  document.getElementById("diag-fts-status").textContent = state.ftsEnabled ? "✅ Enabled" : "❌ Disabled";
  document.getElementById("diag-explain-status").textContent = state.explainMode ? "✅ Enabled (check console)" : "❌ Disabled";

  // Cache Status
  const cacheStateMap = {
    opfs: "OPFS (persistent)",
    memory: "Memory (session only)",
    none: "No cache",
    unsupported: "Unsupported"
  };
  document.getElementById("diag-cache-state").textContent = cacheStateMap[state.cacheState] || state.cacheState;
  document.getElementById("diag-cache-key").textContent = state.cacheKey || "-";
  const cacheLocation = state.cacheState === "opfs" ? "OPFS (origin-private file system)"
    : state.cacheState === "memory" ? "Browser memory"
    : "None";
  document.getElementById("diag-cache-location").textContent = cacheLocation;

  // Performance
  const bootstrapMs = Math.round(performance.now() - bootstrapStart);
  document.getElementById("diag-bootstrap-time").textContent = `${bootstrapMs}ms`;
  document.getElementById("diag-total-messages").textContent = state.totalMessages || "-";
}

diagnosticsToggle.addEventListener("click", () => {
  diagnosticsPanel.classList.remove("hidden");
  updateDiagnostics();
});

closeDiagnostics.addEventListener("click", () => {
  diagnosticsPanel.classList.add("hidden");
});

diagnosticsPanel.addEventListener("click", (event) => {
  if (event.target === diagnosticsPanel) {
    diagnosticsPanel.classList.add("hidden");
  }
});

toggleExplainButton.addEventListener("click", () => {
  state.explainMode = !state.explainMode;
  updateDiagnostics();
  const message = state.explainMode
    ? "EXPLAIN mode enabled. Query plans will be logged to the console."
    : "EXPLAIN mode disabled.";
  console.info(`[viewer] ${message}`);
  alert(message);
});

clearAllCachesButton.addEventListener("click", async () => {
  if (!confirm("Clear all caches? This will remove all offline data and require re-downloading the database.")) {
    return;
  }
  clearAllCachesButton.disabled = true;
  try {
    if (state.cacheKey) {
      await removeFromOpfs(state.cacheKey);
    }
    // Try to clear any other cached files
    const root = await getOpfsRoot();
    if (root) {
      for await (const [name, handle] of root.entries()) {
        if (name.startsWith(CACHE_PREFIX)) {
          try {
            await root.removeEntry(name);
            console.info("[viewer] Removed cached file:", name);
          } catch (err) {
            console.warn("[viewer] Failed to remove:", name, err);
          }
        }
      }
    }
    state.cacheState = CACHE_SUPPORTED ? "memory" : "none";
    updateCacheToggle();
    updateDiagnostics();
    alert("All caches cleared successfully. Refresh the page to reload from network.");
  } catch (error) {
    console.error("[viewer] Failed to clear caches:", error);
    alert(`Failed to clear caches: ${error.message}`);
  } finally {
    clearAllCachesButton.disabled = false;
  }
});

// Alpine.js Controllers
// These functions must be defined before Alpine.js loads (we use defer on Alpine script)

/**
 * Dark mode controller for Alpine.js
 * Manages dark mode toggle with localStorage persistence
 */
window.darkModeController = function() {
  return {
    darkMode: false,

    init() {
      // Initialize from localStorage or system preference
      try {
        const stored = localStorage.getItem('darkMode');
        const prefers = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        this.darkMode = stored === 'true' || (stored === null && prefers);
      } catch (error) {
        console.warn('Failed to read darkMode from localStorage', error);
        this.darkMode = false;
      }
    },

    toggleDarkMode() {
      this.darkMode = !this.darkMode;
      try {
        localStorage.setItem('darkMode', String(this.darkMode));
      } catch (error) {
        console.warn('Failed to persist darkMode to localStorage', error);
      }

      // Update document class
      if (this.darkMode) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
    }
  };
};

/**
 * Main viewer controller for Alpine.js
 * Manages all viewer state and interactions
 */
window.viewerController = function() {
  return {
    // State
    manifest: null,
    isLoading: true,
    viewMode: 'split', // 'split', 'list', or 'threads'
    searchQuery: '',
    filteredMessages: [],
    selectedMessage: null,
    splitView: true,
    sortBy: 'newest',
    isFullscreen: false,
    showDiagnostics: false,
    cacheState: 'none',
    cacheSupported: CACHE_SUPPORTED,
    totalMessages: 0,
    ftsEnabled: false,
    databaseSource: 'network',
    selectedThread: null,
    allMessages: [],
    allThreads: [],

    // Filters
    showFilters: false,
    filters: {
      project: '',
      sender: '',
      recipient: '',
      importance: '',
      hasThread: ''
    },
    uniqueProjects: [],
    uniqueSenders: [],
    uniqueRecipients: [],

    // Bulk Actions
    selectedMessages: [],

    // Refresh Controls
    isRefreshing: false,
    lastRefreshLabel: 'Never',
    autoRefreshEnabled: false,
    refreshError: null,
    refreshInterval: null,

    async init() {
      console.info('[Alpine] Initializing viewer controller');
      await this.initViewer();
    },

    async initViewer() {
      this.isLoading = true;

      try {
        // Load manifest
        this.manifest = await loadJSON("../manifest.json");
        state.manifest = this.manifest;

        // Load database
        const { bytes, source } = await loadDatabaseBytes(this.manifest);
        this.databaseSource = source;
        state.databaseSource = source;

        // Initialize SQL.js
        state.SQL = await ensureSqlJsLoaded();
        state.db = new state.SQL.Database(bytes);

        // Detect FTS
        this.ftsEnabled = Boolean(this.manifest.database?.fts_enabled) && detectFts(state.db);
        state.ftsEnabled = this.ftsEnabled;

        // Load data
        this.totalMessages = Number(getScalar(state.db, "SELECT COUNT(*) FROM messages") || 0);
        state.totalMessages = this.totalMessages;
        loadProjectMap(state.db);

        // Build threads and messages
        const threads = buildThreadList(state.db);
        this.allThreads = this.buildThreadsForAlpine(threads);

        const messages = this.getAllMessages();
        this.allMessages = messages;

        // Build unique filter arrays
        this.buildUniqueFilters(messages);

        // Apply filters
        this.filterMessages();

        // Update cache state
        this.cacheState = state.cacheState;

        this.isLoading = false;

        console.info('[Alpine] Viewer initialized', {
          totalMessages: this.totalMessages,
          ftsEnabled: this.ftsEnabled,
          databaseSource: this.databaseSource,
          cacheState: this.cacheState
        });

      } catch (error) {
        console.error('[Alpine] Initialization failed', error);
        this.isLoading = false;
        alert(`Failed to initialize viewer: ${error.message}`);
      }
    },

    getAllMessages() {
      const results = [];
      const stmt = state.db.prepare(`
        SELECT
          m.id,
          m.subject,
          m.created_ts,
          m.importance,
          m.thread_id,
          m.project_id,
          CASE WHEN m.thread_id IS NULL OR m.thread_id = '' THEN printf('msg:%d', m.id) ELSE m.thread_id END AS thread_key,
          substr(COALESCE(m.body_md, ''), 1, 280) AS snippet,
          m.body_md,
          COALESCE(
            (SELECT name FROM agents WHERE id = (SELECT from_agent_id FROM message_senders WHERE message_id = m.id LIMIT 1)),
            'Unknown'
          ) AS sender,
          COALESCE(p.slug, 'unknown') AS project_slug,
          COALESCE(p.human_key, 'Unknown Project') AS project_name
        FROM messages m
        LEFT JOIN projects p ON p.id = m.project_id
        ORDER BY datetime(m.created_ts) DESC, m.id DESC
      `);

      try {
        while (stmt.step()) {
          const row = stmt.getAsObject();

          // Get recipients for this message
          const recipients = this.getMessageRecipients(row.id);

          // Enrich message with additional fields
          results.push({
            ...row,
            recipients: recipients,
            excerpt: row.snippet || '',
            created_relative: this.formatTimestamp(row.created_ts),
            created_full: this.formatTimestampFull(row.created_ts),
            read: false // Static viewer doesn't track read state
          });
        }
      } finally {
        stmt.free();
      }

      return results;
    },

    getMessageRecipients(messageId) {
      const stmt = state.db.prepare(`
        SELECT COALESCE(a.name, 'Unknown') AS recipient_name
        FROM message_recipients mr
        LEFT JOIN agents a ON a.id = mr.to_agent_id
        WHERE mr.message_id = ?
        ORDER BY recipient_name
      `);

      const recipients = [];
      try {
        stmt.bind([messageId]);
        while (stmt.step()) {
          const row = stmt.getAsObject();
          recipients.push(row.recipient_name);
        }
      } finally {
        stmt.free();
      }

      return recipients.length > 0 ? recipients.join(', ') : 'Unknown';
    },

    buildThreadsForAlpine(rawThreads) {
      const threads = [];

      for (const thread of rawThreads) {
        // Get all messages in this thread
        const messages = this.getMessagesInThread(thread.thread_key);

        threads.push({
          id: thread.thread_key,
          subject: thread.latest_subject || '(no subject)',
          messages: messages,
          last_created_ts: thread.last_created_ts,
          message_count: thread.message_count
        });
      }

      return threads;
    },

    getMessagesInThread(threadKey) {
      const results = [];
      const stmt = state.db.prepare(`
        SELECT
          id,
          subject,
          created_ts,
          importance,
          body_md,
          COALESCE(
            (SELECT name FROM agents WHERE id = (SELECT from_agent_id FROM message_senders WHERE message_id = messages.id LIMIT 1)),
            'Unknown'
          ) AS sender
        FROM messages
        WHERE
          (thread_id = ?)
          OR (thread_id IS NULL AND printf('msg:%d', id) = ?)
        ORDER BY datetime(created_ts) ASC, id ASC
      `);

      try {
        stmt.bind([threadKey, threadKey]);
        while (stmt.step()) {
          results.push(stmt.getAsObject());
        }
      } finally {
        stmt.free();
      }

      return results;
    },

    buildUniqueFilters(messages) {
      const projects = new Set();
      const senders = new Set();
      const recipients = new Set();

      messages.forEach(msg => {
        if (msg.project_name) projects.add(msg.project_name);
        if (msg.sender) senders.add(msg.sender);
        if (msg.recipients) {
          // Recipients is a comma-separated string, split it
          msg.recipients.split(',').forEach(r => {
            const trimmed = r.trim();
            if (trimmed) recipients.add(trimmed);
          });
        }
      });

      this.uniqueProjects = Array.from(projects).sort();
      this.uniqueSenders = Array.from(senders).sort();
      this.uniqueRecipients = Array.from(recipients).sort();
    },

    get filtersActive() {
      return !!(
        this.filters.project ||
        this.filters.sender ||
        this.filters.recipient ||
        this.filters.importance ||
        this.filters.hasThread
      );
    },

    filterMessages() {
      let filtered = this.allMessages;

      // Apply search query
      const query = this.searchQuery.trim().toLowerCase();
      if (query) {
        filtered = filtered.filter(msg => {
          return (
            (msg.subject && msg.subject.toLowerCase().includes(query)) ||
            (msg.body_md && msg.body_md.toLowerCase().includes(query)) ||
            (msg.sender && msg.sender.toLowerCase().includes(query)) ||
            (msg.recipients && msg.recipients.toLowerCase().includes(query))
          );
        });
      }

      // Apply filters
      if (this.filters.project) {
        filtered = filtered.filter(msg => msg.project_name === this.filters.project);
      }

      if (this.filters.sender) {
        filtered = filtered.filter(msg => msg.sender === this.filters.sender);
      }

      if (this.filters.recipient) {
        filtered = filtered.filter(msg => {
          return msg.recipients && msg.recipients.includes(this.filters.recipient);
        });
      }

      if (this.filters.importance) {
        filtered = filtered.filter(msg => msg.importance === this.filters.importance);
      }

      if (this.filters.hasThread) {
        const hasThread = this.filters.hasThread === 'true';
        filtered = filtered.filter(msg => {
          const msgHasThread = msg.thread_id && msg.thread_id !== '';
          return hasThread ? msgHasThread : !msgHasThread;
        });
      }

      // Apply sorting
      this.sortMessages(this.sortBy, filtered);
    },

    clearFilters() {
      this.filters = {
        project: '',
        sender: '',
        recipient: '',
        importance: '',
        hasThread: ''
      };
      this.searchQuery = '';
      this.filterMessages();
    },

    handleMessageClick(msg) {
      if (this.selectedMessage?.id === msg.id) {
        // Deselect if clicking the same message
        this.selectedMessage = null;
      } else {
        this.selectedMessage = msg;
        this.splitView = true;
      }
    },

    selectThread(thread) {
      this.selectedThread = thread;
      this.viewMode = 'threads';
    },

    switchToSplitView() {
      this.viewMode = 'split';
      this.splitView = true;
    },

    renderMarkdown(markdown) {
      if (!markdown) {
        return '';
      }

      // Use the existing renderMarkdownSafe function
      return renderMarkdownSafe(markdown);
    },

    formatTimestamp(timestamp) {
      if (!timestamp) {
        return '';
      }

      try {
        const date = new Date(timestamp);
        if (Number.isNaN(date.getTime())) {
          return timestamp;
        }

        const now = new Date();
        const diffMs = now.getTime() - date.getTime();
        const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

        if (diffDays === 0) {
          return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } else if (diffDays === 1) {
          return 'Yesterday';
        } else if (diffDays < 7) {
          return date.toLocaleDateString([], { weekday: 'short' });
        } else {
          return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
        }
      } catch {
        return timestamp;
      }
    },

    async toggleCache() {
      if (!CACHE_SUPPORTED || !state.cacheKey) {
        alert('Caching is not supported in this browser.');
        return;
      }

      try {
        if (state.cacheState === 'opfs') {
          await removeFromOpfs(state.cacheKey);
          state.cacheState = state.lastDatabaseBytes ? 'memory' : 'none';
        } else if (state.lastDatabaseBytes) {
          const success = await writeToOpfs(state.cacheKey, state.lastDatabaseBytes);
          if (success) {
            state.cacheState = 'opfs';
          }
        }

        this.cacheState = state.cacheState;
      } catch (error) {
        console.error('[Alpine] Cache toggle failed', error);
        alert(`Failed to toggle cache: ${error.message}`);
      }
    },

    sortMessages(sortBy, messages = null) {
      this.sortBy = sortBy;

      const toSort = messages || [...this.filteredMessages];

      switch (sortBy) {
        case 'newest':
          toSort.sort((a, b) => new Date(b.created_ts) - new Date(a.created_ts));
          break;
        case 'oldest':
          toSort.sort((a, b) => new Date(a.created_ts) - new Date(b.created_ts));
          break;
        case 'subject':
          toSort.sort((a, b) => (a.subject || '').localeCompare(b.subject || ''));
          break;
        case 'sender':
          toSort.sort((a, b) => (a.sender || '').localeCompare(b.sender || ''));
          break;
      }

      this.filteredMessages = toSort;
    },

    // Bulk Actions
    toggleSelectAll() {
      if (this.selectedMessages.length === this.filteredMessages.length) {
        // Deselect all
        this.selectedMessages = [];
      } else {
        // Select all filtered messages
        this.selectedMessages = this.filteredMessages.map(msg => msg.id);
      }
    },

    toggleMessageSelection(id) {
      const index = this.selectedMessages.indexOf(id);
      if (index > -1) {
        this.selectedMessages.splice(index, 1);
      } else {
        this.selectedMessages.push(id);
      }
    },

    markSelectedAsRead() {
      // In static viewer, we can't actually mark as read in database
      // But we can update the local state
      this.allMessages.forEach(msg => {
        if (this.selectedMessages.includes(msg.id)) {
          msg.read = true;
        }
      });

      // Clear selection after marking as read
      this.selectedMessages = [];

      // Re-filter to update UI
      this.filterMessages();
    },

    // Refresh Controls
    async fetchLatestMessages() {
      // In static viewer, we can't actually fetch new messages
      // But we can simulate a refresh for UI feedback
      this.isRefreshing = true;
      this.refreshError = null;

      try {
        // Simulate network delay
        await new Promise(resolve => setTimeout(resolve, 500));

        // Update timestamp
        this.lastRefreshLabel = 'Just now';

        console.info('[Alpine] Refreshed messages (static viewer - no new data)');
      } catch (error) {
        console.error('[Alpine] Refresh error', error);
        this.refreshError = 'Failed to refresh';
      } finally {
        this.isRefreshing = false;
      }
    },

    handleAutoRefreshToggle() {
      if (this.autoRefreshEnabled) {
        // Start auto-refresh (every 30 seconds)
        this.refreshInterval = setInterval(() => {
          this.fetchLatestMessages();
        }, 30000);
        console.info('[Alpine] Auto-refresh enabled');
      } else {
        // Stop auto-refresh
        if (this.refreshInterval) {
          clearInterval(this.refreshInterval);
          this.refreshInterval = null;
        }
        console.info('[Alpine] Auto-refresh disabled');
      }
    },

    // Helper Functions
    getProjectBadgeClass(projectName) {
      // Return Tailwind classes for project badge based on project name
      // Use a hash to get consistent colors for same project
      const hash = projectName.split('').reduce((acc, char) => {
        return char.charCodeAt(0) + ((acc << 5) - acc);
      }, 0);

      const colors = [
        'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
        'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300',
        'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300',
        'bg-pink-100 dark:bg-pink-900/30 text-pink-700 dark:text-pink-300',
        'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300',
        'bg-indigo-100 dark:bg-indigo-900/30 text-indigo-700 dark:text-indigo-300',
      ];

      return colors[Math.abs(hash) % colors.length];
    },

    formatTimestampFull(timestamp) {
      if (!timestamp) {
        return 'Unknown';
      }

      try {
        const date = new Date(timestamp);
        if (Number.isNaN(date.getTime())) {
          return timestamp;
        }

        return date.toLocaleDateString([], {
          year: 'numeric',
          month: 'long',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit'
        });
      } catch {
        return timestamp;
      }
    },
  };
};

// Alpine.js controllers are now the ONLY way to initialize the viewer
// No backwards compatibility - we only support the Alpine.js version that matches the Python webui
