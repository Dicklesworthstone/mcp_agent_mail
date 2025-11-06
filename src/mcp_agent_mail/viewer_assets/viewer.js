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
  explainMode: false,
};

const ADMIN_SUBJECT_PATTERNS = [
  /^contact request from/i,
  /\bauto-handshake\b/i,
];

const ADMIN_BODY_PATTERNS = [
  /\bauto-handshake\b/i,
];

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

function markdownToPlainText(markdown) {
  if (!markdown) {
    return "";
  }
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/!\[[^\]]*]\([^)]*\)/g, " ")
    .replace(/\[([^\]]+)]\([^)]+\)/g, "$1")
    .replace(/[#>*_~\-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function buildPreviewSnippet(sourceText) {
  const plain = markdownToPlainText(sourceText);
  if (!plain) {
    return "";
  }
  if (plain.length <= 160) {
    return plain;
  }
  return `${plain.slice(0, 157)}...`;
}

function highlightText(text, term) {
  if (!term) {
    return escapeHtml(text);
  }
  const safeTerm = escapeRegExp(term);
  const regex = new RegExp(`(${safeTerm})`, "gi");
  return escapeHtml(text).replace(regex, "<mark>$1</mark>");
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
    try { script.crossOrigin = "anonymous"; } catch (_) {}
    try { script.fetchPriority = "high"; } catch (_) {}
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

// Alpine.js Controllers
// These functions must be defined before Alpine.js loads (we use defer on Alpine script)

/**
 * Dark mode controller for Alpine.js
 * Manages dark mode toggle with localStorage persistence
 */
function darkModeController() {
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
}

/**
 * Main viewer controller for Alpine.js
 * Manages all viewer state and interactions
 */
function viewerController() {
  return {
    // State
    manifest: null,
    isLoading: true,
    viewMode: 'split', // 'split', 'list', or 'threads'
    searchQuery: '',
    filteredMessages: [],
    selectedMessage: null,
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
    recipientsMap: null,
    threadMessageCounts: null,

    // Filters
    showFilters: true,
    filters: {
      project: '',
      sender: '',
      recipient: '',
      importance: '',
      hasThread: '',
      messageKind: 'user'
    },
    uniqueProjects: [],
    uniqueSenders: [],
    uniqueRecipients: [],
    uniqueImportance: [],
    importanceCounts: {},
    threadSearch: '',

    // Bulk Actions
    selectedMessages: [],

    // Refresh Controls
    isRefreshing: false,
    lastRefreshLabel: 'Never',
    autoRefreshEnabled: false,
    refreshError: null,
    refreshInterval: null,

    // Dark mode (moved here so components can reference `darkMode` directly)
    darkMode: false,

    // Responsive flags
    isMobile: false,
    lastScrollY: 0,
    showMobileMessage: false,
    _mobileMedia: null,
    _mobileMediaListener: null,
    _onMobileScroll: null,

    async init() {
      console.info('[Alpine] Initializing viewer controller');
      // Initialize dark mode state
      try {
        const stored = localStorage.getItem('darkMode');
        const prefers = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        this.darkMode = stored === 'true' || (stored === null && prefers);
      } catch (_err) {
        this.darkMode = document.documentElement.classList.contains('dark');
      }
      if (this.darkMode) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
      await this.initViewer();
      this.setupResponsiveHandlers();
      if (typeof this.$watch === 'function') {
        this.$watch('showMobileMessage', (value) => {
          const body = typeof document !== 'undefined' ? document.body : null;
          if (!body) {
            return;
          }
          if (this.isMobile && value) {
            body.classList.add('mobile-modal-open');
          } else {
            body.classList.remove('mobile-modal-open');
          }
        });
      }
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
        this.threadMessageCounts = new Map(
          threads.map((row) => [row.thread_key, Number(row.message_count || 0)])
        );
        this.allThreads = this.buildThreadsForAlpine(threads);

        const messages = this.getAllMessages();
        this.allMessages = messages;

        // Build unique filter arrays
        this.buildUniqueFilters(messages);

        // Apply filters
        this.filterMessages();

        // Initialize virtual list and select the first message
        this.initVirtualList();
        this.selectFirstMessage();

        // Update cache state
        this.cacheState = state.cacheState;

        this.isLoading = false;

        console.info('[Alpine] Viewer initialized', {
          totalMessages: this.totalMessages,
          ftsEnabled: this.ftsEnabled,
          databaseSource: this.databaseSource,
          cacheState: this.cacheState
        });

        // Opportunistic background cache to OPFS after first successful load
        if (CACHE_SUPPORTED && state.cacheKey && state.cacheState !== 'opfs' && state.lastDatabaseBytes) {
          const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 200));
          idle(async () => {
            try {
              const ok = await writeToOpfs(state.cacheKey, state.lastDatabaseBytes);
              if (ok) {
                state.cacheState = 'opfs';
                this.cacheState = 'opfs';
                console.info('[viewer] Cached database to OPFS', { key: state.cacheKey });
              }
            } catch (err) {
              console.debug('[viewer] OPFS cache write skipped', err);
            }
          });
        }

      } catch (error) {
        console.error('[Alpine] Initialization failed', error);
        this.isLoading = false;
        alert(`Failed to initialize viewer: ${error.message}`);
      }
    },
    setupResponsiveHandlers() {
      if (typeof window === 'undefined' || typeof window.matchMedia === 'undefined') {
        return;
      }
      const query = window.matchMedia('(max-width: 768px)');
      const updateMobile = () => {
        const wasMobile = this.isMobile;
        this.isMobile = Boolean(query.matches);
        if (!this.isMobile) {
          this.showFilters = true;
          this.showMobileMessage = false;
          const body = typeof document !== 'undefined' ? document.body : null;
          if (body) {
            body.classList.remove('mobile-modal-open');
          }
        } else if (!wasMobile && this.isMobile) {
          this.showFilters = false;
        }
      };
      this._mobileMedia = query;
      this._mobileMediaListener = updateMobile;
      if (typeof query.addEventListener === 'function') {
        query.addEventListener('change', updateMobile);
      } else if (typeof query.addListener === 'function') {
        query.addListener(updateMobile);
      }
      updateMobile();
      this.lastScrollY = window.scrollY || 0;
      this._onMobileScroll = () => {
        if (!this.isMobile) {
          return;
        }
        const currentY = window.scrollY || 0;
        if (currentY > this.lastScrollY && currentY > 120 && this.showFilters) {
          this.showFilters = false;
        }
        this.lastScrollY = currentY;
      };
      window.addEventListener('scroll', this._onMobileScroll, { passive: true });
    },

    getAllMessages() {
      const results = [];

      // First, get all messages with their basic info
      const stmt = state.db.prepare(`
        SELECT
          mv.id,
          mv.subject,
          mv.created_ts,
          mv.importance,
          mv.thread_id,
          m.project_id,
          CASE WHEN mv.thread_id IS NULL OR mv.thread_id = '' THEN printf('msg:%d', mv.id) ELSE mv.thread_id END AS thread_key,
          mv.body_length,
          mv.latest_snippet,
          mv.recipients,
          mv.sender_name AS sender,
          COALESCE(p.slug, 'unknown') AS project_slug,
          COALESCE(p.human_key, 'Unknown Project') AS project_name
        FROM message_overview_mv mv
        JOIN messages m ON m.id = mv.id
        LEFT JOIN projects p ON p.id = m.project_id
        ORDER BY datetime(mv.created_ts) DESC, mv.id DESC
      `);

      try {
        while (stmt.step()) {
          results.push(stmt.getAsObject());
        }
      } finally {
        stmt.free();
      }

      // Build a recipients map in a single query (MUCH faster than N+1 queries)
      const recipientsMap = this.buildRecipientsMap();
      this.recipientsMap = recipientsMap;

      // Enrich messages with recipients and formatted dates
      return results.map((msg) => {
        const importance = (msg.importance || '').toLowerCase();
        const bodyLength = Number(msg.body_length) || 0;
        const excerpt = msg.latest_snippet || msg.snippet || '';
        const isAdministrative = this.isAdministrativeMessage(msg);
        const threadKey =
          msg.thread_id && msg.thread_id !== ''
            ? msg.thread_id
            : `msg:${msg.id}`;
        const threadCount = this.threadMessageCounts?.get(threadKey) || 1;
        const hasThread = Boolean(msg.thread_id && msg.thread_id !== '') || threadCount > 1;
        const threadReference = hasThread
          ? (msg.thread_id && msg.thread_id !== '' ? msg.thread_id : threadKey)
          : null;

        return {
          ...msg,
          importance,
          body_length: bodyLength,
          recipients: msg.recipients || recipientsMap.get(msg.id) || 'Unknown',
          excerpt,
          created_relative: this.formatTimestamp(msg.created_ts),
          created_full: this.formatTimestampFull(msg.created_ts),
          read: false, // Static viewer doesn't track read state
          isAdministrative,
          message_category: isAdministrative ? 'admin' : 'user',
          thread_count: threadCount,
          thread_reference: threadReference,
          has_thread: hasThread,
        };
      });
    },

    async loadMessageBodyById(id) {
      let body = '';
      const stmt = state.db.prepare(`SELECT COALESCE(body_md, '') AS body_md FROM messages WHERE id = ? LIMIT 1`);
      try {
        stmt.bind([id]);
        if (stmt.step()) {
          const row = stmt.getAsObject();
          body = row.body_md || '';
        }
      } finally {
        stmt.free();
      }
      return body;
    },

    // Search across ALL messages using SQL: FTS when available, otherwise LIKE.
    // Supports parentheses, NOT, quoted phrases, and OR with proper precedence (NOT > AND > OR).
    searchDatabaseIds(query) {
      if (!state.db) return new Set();
      const raw = String(query || '').trim();
      if (!raw) return new Set();

      // 1) Tokenize: terms, quoted phrases, operators, parentheses
      const tokens = [];
      const re = /\s*(\(|\)|"([^"]*)"|AND|OR|NOT|\||[^\s()"]+)\s*/gi;
      let m;
      while ((m = re.exec(raw)) !== null) {
        const full = m[1];
        if (full === '(' || full === ')') {
          tokens.push({ kind: full });
        } else if (/^AND$/i.test(full)) {
          tokens.push({ kind: 'op', value: 'AND' });
        } else if (/^(OR|\|)$/i.test(full)) {
          tokens.push({ kind: 'op', value: 'OR' });
        } else if (/^NOT$/i.test(full)) {
          tokens.push({ kind: 'op', value: 'NOT' });
        } else if (m[2] != null) {
          tokens.push({ kind: 'term', value: m[2] });
        } else if (full && full.trim()) {
          tokens.push({ kind: 'term', value: full.trim() });
        }
      }
      if (tokens.length === 0) return new Set();

      // 2) Shunting-yard → RPN with precedence: NOT(3) > AND(2) > OR(1)
      const prec = { NOT: 3, AND: 2, OR: 1 };
      const rightAssoc = { NOT: true };
      const output = [];
      const ops = [];
      for (const t of tokens) {
        if (t.kind === 'term') {
          output.push(t);
        } else if (t.kind === 'op') {
          while (
            ops.length > 0 && ops[ops.length - 1].kind === 'op' && (
              (rightAssoc[t.value] !== true && prec[ops[ops.length - 1].value] >= prec[t.value]) ||
              (rightAssoc[t.value] === true && prec[ops[ops.length - 1].value] > prec[t.value])
            )
          ) {
            output.push(ops.pop());
          }
          ops.push(t);
        } else if (t.kind === '(') {
          ops.push(t);
        } else if (t.kind === ')') {
          while (ops.length > 0 && ops[ops.length - 1].kind !== '(') {
            output.push(ops.pop());
          }
          if (ops.length > 0 && ops[ops.length - 1].kind === '(') ops.pop();
        }
      }
      while (ops.length > 0) output.push(ops.pop());

      // 3) Build AST from RPN
      function buildAst(rpn) {
        const stack = [];
        for (const t of rpn) {
          if (t.kind === 'term') {
            stack.push({ type: 'term', value: t.value });
          } else if (t.kind === 'op') {
            if (t.value === 'NOT') {
              const a = stack.pop();
              stack.push({ type: 'not', child: a });
            } else {
              const b = stack.pop();
              const a = stack.pop();
              stack.push({ type: t.value.toLowerCase(), left: a, right: b });
            }
          }
        }
        return stack.pop() || null;
      }
      const ast = buildAst(output);
      if (!ast) return new Set();

      const ids = new Set();

      // 4) Try FTS
      const ftsQuote = (s) => `"${String(s).replace(/"/g, '"')}"`;
      function buildFts(node) {
        if (!node) return '';
        switch (node.type) {
          case 'term':
            return /\s/.test(node.value) ? ftsQuote(node.value) : node.value;
          case 'not':
            return `(NOT ${buildFts(node.child)})`;
          case 'and':
            return `(${buildFts(node.left)} AND ${buildFts(node.right)})`;
          case 'or':
            return `(${buildFts(node.left)} OR ${buildFts(node.right)})`;
        }
        return '';
      }

      if (this.ftsEnabled) {
        const ftsExpr = buildFts(ast).trim();
        if (ftsExpr) {
          const sql = `SELECT rowid AS id FROM fts_messages WHERE fts_messages MATCH ?`;
          let stmt;
          try {
            explainQuery(state.db, sql, [ftsExpr], 'searchDatabaseIds (FTS)');
            stmt = state.db.prepare(sql);
            stmt.bind([ftsExpr]);
            while (stmt.step()) {
              const row = stmt.getAsObject();
              if (row.id != null) ids.add(Number(row.id));
            }
          } catch (error) {
            console.warn('[viewer] FTS search failed, falling back to LIKE', error);
          } finally {
            if (stmt) stmt.free();
          }
          if (ids.size > 0) return ids;
        }
      }

      // 5) LIKE fallback
      function buildLike(node, acc) {
        switch (node.type) {
          case 'term': {
            const needle = `%${String(node.value).toLowerCase()}%`;
            acc.sql.push('(subject_lower LIKE ? OR LOWER(COALESCE(body_md, "")) LIKE ?)');
            acc.params.push(needle, needle);
            break;
          }
          case 'not': {
            const sub = { sql: [], params: [] };
            buildLike(node.child, sub);
            acc.sql.push(`NOT (${sub.sql.join(' ')})`);
            acc.params.push(...sub.params);
            break;
          }
          case 'and': {
            const left = { sql: [], params: [] };
            const right = { sql: [], params: [] };
            buildLike(node.left, left);
            buildLike(node.right, right);
            acc.sql.push(`(${left.sql.join(' ')} AND ${right.sql.join(' ')})`);
            acc.params.push(...left.params, ...right.params);
            break;
          }
          case 'or': {
            const left = { sql: [], params: [] };
            const right = { sql: [], params: [] };
            buildLike(node.left, left);
            buildLike(node.right, right);
            acc.sql.push(`(${left.sql.join(' ')} OR ${right.sql.join(' ')})`);
            acc.params.push(...left.params, ...right.params);
            break;
          }
        }
      }

      const acc = { sql: [], params: [] };
      buildLike(ast, acc);
      const likeSql = `SELECT id FROM messages WHERE ${acc.sql.join(' ')}`;
      let likeStmt;
      try {
        explainQuery(state.db, likeSql, acc.params, 'searchDatabaseIds (LIKE)');
        likeStmt = state.db.prepare(likeSql);
        likeStmt.bind(acc.params);
        while (likeStmt.step()) {
          const row = likeStmt.getAsObject();
          if (row.id != null) ids.add(Number(row.id));
        }
      } catch (error) {
        console.error('[viewer] LIKE search failed', error);
      } finally {
        if (likeStmt) likeStmt.free();
      }

      return ids;
    },

    buildRecipientsMap() {
      // Build a map of message_id -> comma-separated recipient names
      // This is done in ONE query instead of N queries!
      const map = new Map();

      const stmt = state.db.prepare(`
        SELECT
          mr.message_id,
          COALESCE(a.name, 'Unknown') AS recipient_name
        FROM message_recipients mr
        LEFT JOIN agents a ON a.id = mr.agent_id
        ORDER BY mr.message_id, recipient_name
      `);

      try {
        let currentMessageId = null;
        let currentRecipients = [];

        while (stmt.step()) {
          const row = stmt.getAsObject();

          if (currentMessageId !== null && currentMessageId !== row.message_id) {
            // Store previous message's recipients
            map.set(currentMessageId, currentRecipients.join(', '));
            currentRecipients = [];
          }

          currentMessageId = row.message_id;
          currentRecipients.push(row.recipient_name);
        }

        // Don't forget the last message
        if (currentMessageId !== null) {
          map.set(currentMessageId, currentRecipients.join(', '));
        }
      } finally {
        stmt.free();
      }

      return map;
    },

    buildThreadsForAlpine(rawThreads) {
      const threads = [];

      for (const thread of rawThreads) {
        // Get all messages in this thread
        const messages = this.getMessagesInThread(thread.thread_key);
        const adminCount = messages.filter((msg) => msg.isAdministrative).length;
        const hasAdministrative = adminCount > 0;
        const hasNonAdministrative = adminCount < messages.length;
        const threadCategory = hasAdministrative
          ? (hasNonAdministrative ? 'mixed' : 'admin')
          : 'user';

        threads.push({
          id: thread.thread_key,
          subject: thread.latest_subject || '(no subject)',
          messages: messages,
          last_created_ts: thread.last_created_ts,
          last_created_relative: this.formatTimestamp(thread.last_created_ts),
          message_count: thread.message_count,
          latest_importance: (thread.latest_importance || '').toLowerCase(),
          latest_snippet: thread.latest_snippet || '',
          hasAdministrative,
          hasNonAdministrative,
          thread_category: threadCategory
        });
      }

      return threads;
    },

    getMessagesInThread(threadKey) {
      const results = [];
      const stmt = state.db.prepare(`
        SELECT
          m.id,
          m.subject,
          m.created_ts,
          m.importance,
          m.body_md,
          COALESCE(ov.latest_snippet, '') AS latest_snippet,
          COALESCE(ov.attachment_count, 0) AS attachment_count,
          COALESCE(ov.recipients, '') AS recipients,
          COALESCE(a.name, 'Unknown') AS sender
        FROM messages m
        LEFT JOIN agents a ON a.id = m.sender_id
        LEFT JOIN message_overview_mv ov ON ov.id = m.id
        WHERE
          (m.thread_id = ?)
          OR (m.thread_id IS NULL AND printf('msg:%d', m.id) = ?)
        ORDER BY datetime(m.created_ts) ASC, m.id ASC
      `);

      try {
        stmt.bind([threadKey, threadKey]);
        while (stmt.step()) {
          results.push(stmt.getAsObject());
        }
      } finally {
        stmt.free();
      }

      return results.map((msg) => {
        const importance = (msg.importance || '').toLowerCase();
        const isAdministrative = this.isAdministrativeMessage({
          subject: msg.subject,
          body_md: msg.body_md,
        });
        const recipientsFromMap = this.recipientsMap && this.recipientsMap.get(msg.id)
          ? this.recipientsMap.get(msg.id)
          : '';
        const recipients = msg.recipients || recipientsFromMap || 'Unknown';
        const previewSource = msg.latest_snippet || msg.body_md || '';
        const preview_plain = buildPreviewSnippet(previewSource);
        const threadReference =
          msg.thread_id && msg.thread_id !== ''
            ? msg.thread_id
            : threadKey;
        return {
          ...msg,
          importance,
          body_length: msg.body_md ? msg.body_md.length : 0,
          recipients,
          isAdministrative,
          preview_plain,
          thread_reference: threadReference,
          has_thread: true,
        };
      });
    },

    buildUniqueFilters(messages) {
      const projects = new Set();
      const senders = new Set();
      const recipients = new Set();
      const importanceMap = new Map();

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

        const importance = (msg.importance || 'normal').toLowerCase();
        importanceMap.set(importance, (importanceMap.get(importance) || 0) + 1);
      });

      this.uniqueProjects = Array.from(projects).sort();
      this.uniqueSenders = Array.from(senders).sort();
      this.uniqueRecipients = Array.from(recipients).sort();

      const order = new Map([
        ['urgent', 0],
        ['high', 1],
        ['normal', 2],
        ['low', 3],
      ]);

      const importanceEntries = Array.from(importanceMap.entries()).sort((a, b) => {
        const rankA = order.has(a[0]) ? order.get(a[0]) : 99;
        const rankB = order.has(b[0]) ? order.get(b[0]) : 99;
        if (rankA !== rankB) return rankA - rankB;
        return a[0].localeCompare(b[0]);
      });

      this.uniqueImportance = importanceEntries.map(([value, count]) => ({ value, count }));
      this.importanceCounts = Object.fromEntries(importanceEntries);

      if (this.filters.importance && !this.importanceCounts[this.filters.importance]) {
        this.filters.importance = '';
      }
    },

    isAdministrativeMessage(message) {
      const subject = message?.subject || '';
      const snippetSource = message?.snippet ?? message?.body_md ?? '';
      if (ADMIN_SUBJECT_PATTERNS.some((pattern) => pattern.test(subject))) {
        return true;
      }
      if (ADMIN_BODY_PATTERNS.some((pattern) => pattern.test(snippetSource))) {
        return true;
      }
      return false;
    },

    isThreadVisible(thread) {
      if (!thread) return false;
      const kind = this.filters.messageKind || 'user';
      if (kind === 'all') {
        return true;
      }
      if (kind === 'admin') {
        return thread.hasAdministrative;
      }
      return thread.hasNonAdministrative;
    },

    filteredThreads() {
      const query = this.threadSearch.trim().toLowerCase();
      return this.allThreads.filter((thread) => {
        if (!this.isThreadVisible(thread)) {
          return false;
        }
        if (!query) {
          return true;
        }
        const subject = (thread.subject || '').toLowerCase();
        const identifier = (thread.id || '').toLowerCase();
        const snippet = (thread.latest_snippet || '').toLowerCase();
        return subject.includes(query) || identifier.includes(query) || snippet.includes(query);
      });
    },

    openThreadById(threadId) {
      if (!threadId) return;
      const normalizedId = String(threadId);
      let thread = this.allThreads.find((t) => t.id === normalizedId);
      if (!thread) {
        const messages = this.getMessagesInThread(normalizedId);
        const hasAdministrative = messages.some((msg) => msg.isAdministrative);
        const hasNonAdministrative = messages.some((msg) => !msg.isAdministrative);
        const latestMessage = messages[messages.length - 1];
        thread = {
          id: normalizedId,
          subject: latestMessage?.subject || '(no subject)',
          messages,
          last_created_ts: latestMessage?.created_ts ?? null,
          last_created_relative: latestMessage ? this.formatTimestamp(latestMessage.created_ts) : '',
          message_count: messages.length,
          latest_importance: latestMessage?.importance || '',
          latest_snippet: latestMessage?.latest_snippet || latestMessage?.preview_plain || '',
          hasAdministrative,
          hasNonAdministrative,
          thread_category: hasAdministrative
            ? (hasNonAdministrative ? 'mixed' : 'admin')
            : 'user',
        };
        this.allThreads.push(thread);
      }
      this.selectThread(thread);
      this.threadSearch = '';
    },

    get filtersActive() {
      return !!(
        this.filters.project ||
        this.filters.sender ||
        this.filters.recipient ||
        this.filters.importance ||
        this.filters.hasThread ||
        this.filters.messageKind !== 'user'
      );
    },

    filterMessages() {
      let filtered = this.allMessages;

      // Apply search query (pass raw; the engine handles case/phrases/operators)
      const query = this.searchQuery.trim();
      if (query) {
        const idSet = this.searchDatabaseIds(query);
        filtered = filtered.filter(msg => idSet.has(msg.id));
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
          if (!msg.recipients) return false;
          // Split recipients and do exact matching to avoid substring false positives
          // e.g., "Alice" shouldn't match "Alicia, Bob"
          const recipientsList = msg.recipients.split(',').map(r => r.trim());
          return recipientsList.includes(this.filters.recipient);
        });
      }

      if (this.filters.importance) {
        const importanceFilter = this.filters.importance.toLowerCase();
        filtered = filtered.filter(msg => (msg.importance || '').toLowerCase() === importanceFilter);
      }

      if (this.filters.hasThread) {
        const hasThread = this.filters.hasThread === 'true';
        filtered = filtered.filter(msg => {
          const msgHasThread = msg.thread_id && msg.thread_id !== '';
          return hasThread ? msgHasThread : !msgHasThread;
        });
      }

      const messageKind = this.filters.messageKind || 'user';
      if (messageKind === 'user') {
        filtered = filtered.filter(msg => !msg.isAdministrative);
      } else if (messageKind === 'admin') {
        filtered = filtered.filter(msg => msg.isAdministrative);
      }

      // Apply sorting
      this.sortMessages(this.sortBy, filtered);

      // Clear any selected messages that are no longer in the filtered list
      const filteredIds = new Set(this.filteredMessages.map(msg => msg.id));
      this.selectedMessages = this.selectedMessages.filter(id => filteredIds.has(id));

      if (this.selectedMessage && !filteredIds.has(this.selectedMessage.id)) {
        this.selectedMessage = null;
        this.showMobileMessage = false;
      }

      if (this.selectedThread && !this.isThreadVisible(this.selectedThread)) {
        this.selectedThread = null;
      }
    },

    clearFilters() {
      this.filters = {
        project: '',
        sender: '',
        recipient: '',
        importance: '',
        hasThread: '',
        messageKind: 'user'
      };
      this.searchQuery = '';
      this.threadSearch = '';
      this.selectedMessages = []; // Clear selections when clearing filters
      this.selectedThread = null;
      this.showMobileMessage = false;
      this.filterMessages();
    },

    async handleMessageClick(msg) {
      if (this.selectedMessage?.id === msg.id) {
        // Deselect if clicking the same message
        this.selectedMessage = null;
        this.showMobileMessage = false;
        this.syncVisibleSelectionHighlight();
        return;
      }
      const fullBody = await this.loadMessageBodyById(msg.id);
      this.selectedMessage = {
        ...msg,
        body_md: fullBody,
      };
      // Switch to split view when selecting a message
      this.viewMode = 'split';
      // Update highlight without rebuilding rows to preserve scroll position
      this.syncVisibleSelectionHighlight();
      if (this.isMobile) {
        this.showMobileMessage = true;
      } else {
        this.showMobileMessage = false;
      }
    },

    selectThread(thread) {
      this.selectedThread = thread;
      this.viewMode = 'threads';
      this.showMobileMessage = false;
      this.$nextTick(() => {
        try {
          const list = this.$refs?.threadList;
          if (!list) {
            return;
          }
          const rawId = thread && thread.id ? String(thread.id) : "";
          if (!rawId) {
            return;
          }
          const escapedId =
            typeof CSS !== "undefined" && typeof CSS.escape === "function"
              ? CSS.escape(rawId)
              : rawId.replace(/"/g, '\\"');
          const button = list.querySelector(
            `[data-thread-id="${escapedId}"]`,
          );
          if (button && typeof button.scrollIntoView === "function") {
            button.scrollIntoView({ block: "nearest", behavior: "smooth" });
          }
        } catch (error) {
          console.debug("[threads] scroll into view skipped", error);
        }
      });
    },

    switchToSplitView() {
      this.viewMode = 'split';
    },

    switchToThreadsView() {
      this.viewMode = 'threads';
      this.showMobileMessage = false;
      if (!this.selectedThread) {
        const firstVisibleThread = this.filteredThreads()[0];
        if (firstVisibleThread) {
          this.selectThread(firstVisibleThread);
        }
      }
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

    formatImportanceLabel(value) {
      const normalized = (value || '').toLowerCase();
      switch (normalized) {
        case 'urgent':
          return 'Urgent';
        case 'high':
          return 'High';
        case 'low':
          return 'Low';
        case 'normal':
        default:
          return 'Normal';
      }
    },

    scrollToTop() {
      if (typeof window !== 'undefined') {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    },

    closeMobileMessage() {
      this.showMobileMessage = false;
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
        case 'longest':
          toSort.sort((a, b) => (b.body_length || 0) - (a.body_length || 0));
          break;
      }

      this.filteredMessages = toSort;
      this.updateVirtualList();
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

    // Debounced search input handler to avoid hammering SQL.js on each keystroke
    onSearchInput() {
      if (this._searchDebounce) clearTimeout(this._searchDebounce);
      this._searchDebounce = setTimeout(() => {
        this.filterMessages();
      }, 140);
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

    toggleDarkMode() {
      this.darkMode = !this.darkMode;
      try {
        localStorage.setItem('darkMode', String(this.darkMode));
      } catch (_err) {
        // ignore storage errors in static viewer
      }
      if (this.darkMode) {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
    },

    // Virtual list (Clusterize.js)
    virtualList: null,
    buildMessageRow(msg, index) {
      const isSelected = this.selectedMessage?.id === msg.id;
      const selectedClasses = isSelected
        ? 'bg-primary-50 dark:bg-primary-900/20 border-l-4 border-l-primary-500'
        : 'border-l-4 border-l-transparent hover:bg-slate-50 dark:hover:bg-slate-900';

      const projectBadge = this.getProjectBadgeClass(msg.project_name || '');

      return (
        `<div class="message-row px-4 py-3 border-b border-slate-100 dark:border-slate-700 cursor-pointer transition-all duration-200 group focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-inset ${selectedClasses}" data-message-id="${msg.id}" tabindex="0" role="button" aria-label="Message from ${escapeHtml(msg.sender || '')}: ${escapeHtml(msg.subject || '')}" style="animation-delay: ${Math.min(index * 0.02, 0.5)}s;">`
        + `<div class="flex items-start gap-3">`
        + `<input type="checkbox" class="mt-1 w-4 h-4 text-primary-600 bg-white dark:bg-slate-700 border-slate-300 dark:border-slate-600 rounded focus:ring-2 focus:ring-primary-500 transition-all duration-200 cursor-pointer opacity-0 group-hover:opacity-100" aria-hidden="true">`
        + `<div class="flex-1 min-w-0">`
        + `<div class="flex items-center gap-2 mb-1">`
        + `<span class="text-sm font-semibold text-slate-900 dark:text-white truncate">${escapeHtml(msg.sender || '')}</span>`
        + `<i data-lucide="arrow-right" class="w-3 h-3 text-slate-400 flex-shrink-0"></i>`
        + `<span class="text-sm text-slate-600 dark:text-slate-400 truncate">${escapeHtml(msg.recipients || '')}</span>`
        + `</div>`
        + `<div class="flex items-center gap-2 mb-1.5 flex-wrap">`
        + `<span class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full ${projectBadge}" title="${escapeHtml(msg.project_name || '')}">`
        + `<i data-lucide="folder" class="w-3 h-3"></i>`
        + `<span class="truncate max-w-[100px]">${escapeHtml(msg.project_name || '')}</span>`
        + `</span>`
        + (msg.importance === 'urgent'
          ? `<span class=\"inline-flex items-center gap-1 px-2 py-0.5 bg-danger-100 dark:bg-danger-900/30 text-danger-700 dark:text-danger-300 text-xs font-bold rounded-full\"><i data-lucide=\"alert-circle\" class=\"w-3 h-3\"></i>Urgent</span>`
          : msg.importance === 'high'
            ? `<span class=\"inline-flex items-center gap-1 px-2 py-0.5 bg-warning-100 dark:bg-warning-900/30 text-warning-700 dark:text-warning-300 text-xs font-semibold rounded-full\"><i data-lucide=\"alert-triangle\" class=\"w-3 h-3\"></i>High</span>`
            : '')
        + `</div>`
        + `<div class="text-sm mb-1 text-slate-900 dark:text-white truncate">${escapeHtml(msg.subject || '')}</div>`
        + `<div class="text-xs text-slate-600 dark:text-slate-400 line-clamp-2">${escapeHtml(msg.excerpt || '')}</div>`
        + `<div class="text-xs text-slate-500 dark:text-slate-500 mt-1">${escapeHtml(msg.created_relative || '')}</div>`
        + `</div>`
        + `</div>`
        + `</div>`
      );
    },
    buildRowsFromMessages(messages) {
      const rows = [];
      for (let i = 0; i < messages.length; i += 1) {
        rows.push(this.buildMessageRow(messages[i], i));
      }
      return rows;
    },
    initVirtualList() {
      const scrollElem = document.getElementById('virtual-message-list');
      const contentElem = document.getElementById('virtual-message-content');
      if (!scrollElem || !contentElem) {
        return;
      }

      const setHeightFromViewport = () => {
        try {
          const rect = scrollElem.getBoundingClientRect();
          const vh = window.innerHeight || document.documentElement.clientHeight || 800;
          const h = Math.max(240, Math.floor(vh - rect.top - 12));
          scrollElem.style.height = `${h}px`;
        } catch (_) {}
      };

      setHeightFromViewport();

      const virtualState = {
        scrollElem,
        contentElem,
        estimatedRowHeight: 156,
        overscan: 6,
        renderRaf: null,
        heightRaf: null
      };

      const scheduleRender = () => {
        if (virtualState.renderRaf) cancelAnimationFrame(virtualState.renderRaf);
        virtualState.renderRaf = requestAnimationFrame(() => {
          virtualState.renderRaf = null;
          this.renderVirtualSlice();
        });
      };

      const measureRowHeight = () => {
        if (!this.virtualList) return;
        const rows = Array.from(this.virtualList.contentElem.querySelectorAll('.message-row'));
        if (!rows.length) return;
        const total = rows.reduce((acc, el) => acc + el.getBoundingClientRect().height, 0);
        const avg = total / rows.length;
        if (Number.isFinite(avg) && avg > 32) {
          this.virtualList.estimatedRowHeight = (this.virtualList.estimatedRowHeight * 0.6) + (avg * 0.4);
        }
      };

      const onScroll = () => {
        scheduleRender();
      };

      const onResize = () => {
        setHeightFromViewport();
        measureRowHeight();
        scheduleRender();
      };

      if (!this._onRowClick) {
        this._onRowClick = (event) => {
          const row = event.target.closest('[data-message-id]');
          if (!row) return;
          const id = Number(row.getAttribute('data-message-id'));
          const msg = this.filteredMessages.find(m => m.id === id);
          if (msg) {
            this.handleMessageClick(msg);
          }
        };
      }

      this._onVirtualScroll = onScroll;
      scrollElem.addEventListener('scroll', this._onVirtualScroll);
      scrollElem.addEventListener('click', this._onRowClick);
      this._onResize = onResize;
      window.addEventListener('resize', this._onResize);

      this.virtualList = { ...virtualState, scheduleRender, measureRowHeight };

      // Wait for fonts to load before first measurement to avoid layout jumps
      try {
        if (document.fonts && document.fonts.ready) {
          document.fonts.ready.then(() => {
            if (this.virtualList) {
              this.virtualList.measureRowHeight();
              this.renderVirtualSlice(true);
            }
          }).catch(() => {
            this.renderVirtualSlice(true);
          });
        } else {
          this.renderVirtualSlice(true);
        }
      } catch (_) {
        this.renderVirtualSlice(true);
      }
    },
    updateVirtualList() {
      if (!this.virtualList) {
        this.initVirtualList();
        return;
      }
      this.renderVirtualSlice(true);
    },
    renderVirtualSlice(forceRebuild = false) {
      if (!this.virtualList) return;
      const { scrollElem, contentElem, estimatedRowHeight, overscan } = this.virtualList;
      const total = this.filteredMessages.length;

      if (total === 0) {
        scrollElem.scrollTop = 0;
        contentElem.innerHTML = '<div class="py-20 text-center text-slate-500 dark:text-slate-400">No messages found</div>';
        return;
      }

      const viewportHeight = scrollElem.clientHeight || 1;
      const scrollTop = scrollElem.scrollTop || 0;
      const estRow = Math.max(estimatedRowHeight, 56);
      const startIndex = Math.max(0, Math.floor(scrollTop / estRow) - overscan);
      const visibleCount = Math.ceil(viewportHeight / estRow) + overscan * 2;
      const endIndex = Math.min(total, startIndex + visibleCount);

      const beforeHeight = startIndex * estRow;
      const afterHeight = Math.max(0, (total - endIndex) * estRow);

      // Build rows for current window
      const rows = [];
      for (let idx = startIndex; idx < endIndex; idx += 1) {
        rows.push(this.buildMessageRow(this.filteredMessages[idx], idx));
      }

      const spacerBefore = `<div class="virtual-spacer" style="height:${beforeHeight}px"></div>`;
      const spacerAfter = `<div class="virtual-spacer" style="height:${afterHeight}px"></div>`;

      const nextMarkup = spacerBefore + rows.join('') + spacerAfter;
      if (!forceRebuild && contentElem.innerHTML === nextMarkup) {
        return;
      }
      contentElem.innerHTML = nextMarkup;

      this.virtualList.measureRowHeight();

      try {
        if (typeof lucide !== 'undefined') {
          lucide.createIcons();
        }
      } catch (_) {}

      this.syncVisibleSelectionHighlight();
    },

    // Update selected-row styling for visible rows only, without touching Clusterize data
    syncVisibleSelectionHighlight() {
      try {
        const container = document.getElementById('virtual-message-list');
        if (!container) return;
        // Clear any existing highlight
        container.querySelectorAll('.message-row').forEach(el => {
          el.classList.remove('bg-primary-50', 'dark:bg-primary-900/20', 'border-l-4', 'border-l-primary-500');
        });
        if (!this.selectedMessage) return;
        const sel = container.querySelector(`.message-row[data-message-id="${this.selectedMessage.id}"]`);
        if (sel) {
          sel.classList.add('bg-primary-50', 'dark:bg-primary-900/20', 'border-l-4', 'border-l-primary-500');
        }
      } catch (_) {}
    },
    selectFirstMessage() {
      if (this.filteredMessages.length > 0 && !this.selectedMessage) {
        this.handleMessageClick(this.filteredMessages[0]);
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

    // Cleanup when component is destroyed
    destroy() {
      // Clear auto-refresh interval to prevent memory leaks
      if (this.refreshInterval) {
        clearInterval(this.refreshInterval);
        this.refreshInterval = null;
        console.info('[Alpine] Cleaned up auto-refresh interval');
      }
      if (typeof window !== 'undefined' && this._onMobileScroll) {
        window.removeEventListener('scroll', this._onMobileScroll);
        this._onMobileScroll = null;
      }
      if (this._mobileMedia && this._mobileMediaListener) {
        if (typeof this._mobileMedia.removeEventListener === 'function') {
          this._mobileMedia.removeEventListener('change', this._mobileMediaListener);
        } else if (typeof this._mobileMedia.removeListener === 'function') {
          this._mobileMedia.removeListener(this._mobileMediaListener);
        }
      }
      this._mobileMedia = null;
      this._mobileMediaListener = null;
      if (typeof document !== 'undefined' && document.body) {
        document.body.classList.remove('mobile-modal-open');
      }
      if (this.virtualList && this.virtualList.scrollElem) {
        try {
          this.virtualList.scrollElem.removeEventListener('scroll', this._onVirtualScroll);
          this.virtualList.scrollElem.removeEventListener('click', this._onRowClick);
        } catch (_) {}
      }
      if (this._onVirtualScroll) {
        this._onVirtualScroll = null;
      }
      if (this._onRowClick) {
        this._onRowClick = null;
      }
      this.virtualList = null;
      if (this._onResize) {
        try { window.removeEventListener('resize', this._onResize); } catch (_) {}
        this._onResize = null;
      }
    },
  };
}

// Expose controllers on window so x-data can call them directly
if (typeof window !== 'undefined') {
  window.darkModeController = darkModeController;
  window.viewerController = viewerController;
}

const registerAlpineControllers = () => {
  if (!window.Alpine) {
    return;
  }
  // Also register with Alpine (not strictly required when using window.viewerController())
  window.Alpine.data('viewerController', viewerController);
};

if (window.Alpine) {
  registerAlpineControllers();
} else {
  document.addEventListener('alpine:init', registerAlpineControllers, { once: true });
}

// If Alpine deferred startup for us, start it now after controllers are in place
if (typeof window !== 'undefined' && typeof window.__alpineStart === 'function') {
  try {
    window.__alpineStart();
  } finally {
    window.__alpineStart = null;
  }
}

// Alpine.js controllers are now the ONLY way to initialize the viewer
// No backwards compatibility - we only support the Alpine.js version that matches the Python webui
