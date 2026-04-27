# Plan to Enable Easy and Secure Sharing of Agent Mailbox

## Objectives
- Allow maintainers to publish a curated, read‑only snapshot of one or more MCP Agent Mail projects as static assets (suitable for GitHub Pages, Cloudflare Pages, etc.).
- Preserve the rich browsing and search UX of the existing Web UI without requiring a live FastMCP server.
- Guarantee data minimization, integrity, and optional secrecy controls so that exported mailboxes cannot be modified silently or leak unintended content.

## Ease-of-Use Principles
- Provide a single happy-path command (`uv run python -m mcp_agent_mail.cli share export --output ./out/mailbox-share`) that emits a ready-to-host bundle with smart defaults (all projects, chunking when needed, manifest signing, ZIP archive).
- Offer an interactive `--interactive` wizard that prompts for project selection, redaction presets, encryption, and attachment policies so users never need to memorize flags.
- ✅ *(2025-11-04)* Wizard now collects project filters, attachment thresholds, and ZIP opt-in before export.
- ✅ *(2025-11-04)* Built-in preview server available via `share preview`, with optional browser launch for frictionless validation.
- ✅ *(2025-11-04)* Minimal `viewer/` scaffold (HTML/CSS/JS + diagnostics) ships in every bundle to surface manifest and hosting details.
- ✅ *(2025-11-04)* Export pipeline auto-detects GitHub Pages / Cloudflare Pages / Netlify / S3 signals and records tailored instructions in manifest + HOW_TO_DEPLOY.
- ✅ *(2025-11-04)* Each bundle now renders `HOW_TO_DEPLOY.md` with host-specific copy/paste deployment steps automatically generated.
- ✅ *(2025-11-04)* Advanced options (Ed25519 signing, age encryption, attachment thresholds) remain opt-in flags or wizard prompts; defaults stay minimal.
- ✅ *(2025-11-05)* Share wizard now presents scrub presets (`standard`, `strict`) with inline guidance so operators pick the right redaction level without reading docs.

## Constraints & Assumptions
- Exports must never mutate the live SQLite database; they operate on a read-only snapshot.
- All configuration continues to be sourced through `python-decouple` and the existing `.env`.
- Host environments are static file hosts with no ability to set custom HTTP headers unless explicitly supported (GitHub Pages, Cloudflare Pages, Netlify, etc.).
- Attachments are predominantly WebP images, but other binary formats may appear.
- Default target runtime: modern evergreen browsers (Chrome 129+, Firefox 129+, Safari 18+) with WASM and Web Workers enabled.

## High-Level Architecture
1. **Export Pipeline (CLI):**
   - New command group: `uv run python -m mcp_agent_mail.cli share export [...]`.
   - Steps: scope selection → snapshot → scrub → package.
2. **Static Bundle Layout:**
   ```
   mailbox-share/
     README.txt
     manifest.json
     mailbox.sqlite3            # small/medium bundles
     mailbox.sqlite3.config.json # httpvfs chunk map (only when chunked)
     chunks/                    # optional chunked database blobs
     attachments/
     viewer/
       index.html
       assets/
         mail-viewer.js
         mail-viewer.css
         wasm/       # JS/WASM runtimes
   ```
3. **Client-Side Runtime:**
   - Engines (auto-selected at viewer boot):
     1) `@sqlite.org/sqlite-wasm` with OPFS-based caching via cross-origin isolation or the SAH-Pool VFS path when available—fastest warm load, no re-downloads.[1][2]
     2) `sql.js-httpvfs` for zero-header static hosting with HTTP Range streaming (no persistent cache required).[3]
     3) Optional `absurd-sql` IndexedDB VFS as persistence when COOP/COEP is unavailable but local caching is desired.[4]
   - Fallback: in-memory `sql.js` driver if none of the above succeed, with an explicit warning about reduced search performance.
   - Ship prebuilt WASM artifacts compiled with `-DSQLITE_ENABLE_FTS5` (sql.js) and full-text support enabled (sqlite-wasm). Abort viewer initialization if FTS5 capabilities are absent.[5]
4. **Viewer UI:**
   - Reuse existing Jinja templates as design reference but ship a pre-built static SPA (Vite build) that mirrors the navigation, search, and message detail flows.
   - All data access occurs via in-browser SQL queries against the bundled database.
5. **Security Layer:**
   - Scrubbing rules (PII redaction, deterministic pseudonyms, optional thread filtering).
   - Integrity manifest with SHA-256 hashes, Subresource Integrity for viewer assets, and Ed25519 signatures for tamper evidence.[8][13]
   - Optional whole-bundle encryption using `age` (passphrase or public-key recipients) with in-browser decryption prior to mounting.[9]

## Export Pipeline Details
### Command Workflow
```
uv run python -m mcp_agent_mail.cli share export \
  --output ./out/mailbox-share \
  --project backend --project frontend \
  --since 2025-06-01 \
  --include-attachments inline,external \
  --redact-agent-names pseudonym \
  --encrypt passphrase
```
Running the command with only `--output` (no extra flags) exports all projects, auto-chunks large databases, signs the manifest, produces a zipped archive, and drops `HOW_TO_DEPLOY.md` alongside the static bundle so the default workflow stays copy/paste simple.

| Stage                | Description |
|----------------------|-------------|
| Scope resolution     | Match project slugs or human keys via `--project`; default is all projects. Persist included list + removed count in manifest. |
| Consistent snapshot  | Use SQLite `backup` API (preferred) or `VACUUM ... INTO` to materialize the live WAL database into a single `.sqlite3` file before further processing. |
| Scrubbing            | Clear ack metadata, purge file reservations/agent links, and redact common token patterns (API keys, tokens) across subjects, bodies, and attachment metadata. Agent names are retained as-is (already meaningless pseudonyms like "BlueMountain"). |
| Attachment handling  | Inline ≤64 KB assets as data URIs; larger files stored under `attachments/<xx>/<sha256>.<ext>` (content-addressed). Detach >25 MiB files to external object storage when necessary, retaining hashes/URLs in manifest.[3][11] |
| Manifest build       | Record export metadata, schema versions, compile options snapshot, hashed artifacts, applied redactions, CLI version, build timestamp (UTC), optional Ed25519 signing key. |
| Bundle assembly      | Emit viewer assets (pre-built) + data files; automatically compress into `.zip`, generate `HOW_TO_DEPLOY.md`, and include optional `deploy/` helper scripts. |

### Data Minimization
- Redact fields not needed for read-only viewing (internal IDs, read/ack markers, file reservation metadata).
- Replace absolute project paths with friendly slugs and optional display names supplied via CLI.
- Agent names (BlueMountain, GreenCastle, etc.) are already meaningless pseudonyms by design and are retained as-is for readability.
- Strip bearer tokens, JWTs, and common secret formats (e.g., GitHub, Slack tokens) before writing frontmatter or manifest entries.
- Remove contact policies and policy histories; retain only data required for rendering.
- Provide pluggable redaction hooks so teams can mask sensitive phrases prior to export.
- ✅ *(2025-11-04)* CLI export scrubs ack/read markers, deletes file reservations and agent links, and redacts common token patterns (API keys, secrets) before manifest generation. Agent names are retained for readability.

## Export SQL Hygiene
Execute before packaging each bundle:
```
PRAGMA journal_mode=DELETE;
PRAGMA page_size=1024;
INSERT INTO fts_messages(fts_messages) VALUES('optimize');
VACUUM;
PRAGMA optimize;
```
Repeat the `INSERT ... VALUES('optimize')` step for every FTS table. This sequence improves httpvfs locality, reduces download size, and guarantees FTS5 compile-time options are exercised.[3][5]

- Never remove `storage.sqlite3-wal` or `storage.sqlite3-shm` directly; WAL content is required to avoid corruption.[15]
- Preferred approach: use the SQLite **Online Backup API** (available via `sqlite3` CLI `.backup` or Python’s `sqlite3.Connection.backup`) to copy the live database into a clean snapshot file. The API automatically includes pending WAL frames and yields a consistent single-file image.[16]
- Alternate approach: run `VACUUM main INTO 'snapshot.sqlite3'` from the sqlite3 shell. This produces a compact file with all WAL changes applied and leaves the source untouched. Ensure the destination file does not already exist and allow for additional disk space during the vacuum.[14]
- Before snapshotting, make sure no other connections are writing; optionally execute `PRAGMA wal_checkpoint(TRUNCATE);` to flush frames, then perform the backup.
- Export pipeline uses the snapshot file (`storage.snapshot.sqlite3`) as the input for redaction and packaging, keeping the original live database and its WAL files intact.
- **Progress – 2025-11-04:** Implemented `share export` CLI command that creates a clean `mailbox.sqlite3` snapshot using the Online Backup API.


## Static Viewer Design
### Core Modules
1. **Database Loader**
   - Detects COOP/COEP + SAH-Pool availability, then loads the preferred engine (sqlite-wasm → httpvfs → absurd-sql → sql.js fallback).
   - Fetches either `mailbox.sqlite3` or chunk manifests based on `mailbox.sqlite3.config.json`, streaming pages with httpvfs when present.[3]
   - Provides instrumentation hooks (timing, strategy chosen) for troubleshooting large bundles.
2. **Data Access Layer**
   - Parameterized queries aligned with existing API view-models; avoid `SELECT *`.
   - Export-time materialized views with covering indexes:
     - `message_overview_mv` (subject, created_ts, thread_id, sender, recipients, snippet, importance).
     - `attachments_by_message_mv`.
     - `fts_search_overview_mv` (rowid, subject, highlights) for search result rendering.
   - Viewer surfaces EXPLAIN PLAN metrics (optional debug toggle) to validate sequential access patterns under httpvfs.[3]
3. **Search**
   - Use external-content FTS5 tables; during export run:
     - `INSERT INTO fts_messages(fts_messages) VALUES('optimize');`
     - `PRAGMA optimize;`
     - `VACUUM; PRAGMA page_size=1024;`[3]
   - Viewer boot validates FTS5 availability via `PRAGMA compile_options`; fail fast with remediation guidance when missing.[5]
   - UI advertises Boolean operators and phrase queries only when FTS5 path is active; fallback to LIKE search hides those affordances to avoid confusion.
4. **UI Layer**
   - Tailwind + Alpine.js compiled into self-hosted bundles (no CDN fonts/scripts) to maintain CSP simplicity.
   - Hash-based routing only; no external fonts or analytics.
   - All actions read-only; hide composer forms, file reservation panels, overseer controls, and contact workflows.
5. **Progressive Loading**
   - Show skeleton screens while runtime + initial pages stream.
   - Default to httpvfs streaming for first load when DB > configurable threshold; expose "Cache for offline use" action:
     - COOP/COEP available → copy into OPFS via sqlite-wasm (SAH-Pool).
     - Otherwise → persist via absurd-sql (IndexedDB).
   - Version caches using manifest hash and sqlite `user_version` to invalidate stale snapshots automatically.[4]

### Hosting Compatibility
- **GitHub Pages / static hosts without custom headers:**
  - Prefer `sql.js-httpvfs` (Range requests supported); use chunked DB output when bundle exceeds per-file limits.[3][10]
  - Provide `static.json` or `.nojekyll` to ensure `.wasm` served with `application/wasm`.
  - Export places a ready-to-commit `docs/` layout and instructions in `HOW_TO_DEPLOY.md` (copy `viewer/` + DB assets, push to main).
  - Recommend optional age-encrypted bundles committed to repo to keep sensitive data encrypted at rest.
- **Cloudflare Pages:**
  - 25 MiB per-file limit—generate chunked DB and/or offload large attachments to R2 with hashed URLs.[11]
  - Supply `_headers` file setting `Cross-Origin-Opener-Policy` and `Cross-Origin-Embedder-Policy` for sqlite-wasm fast path.[1]
- **Netlify / S3 + CDN:**
  - Document header configuration equivalents; verify Range and `application/wasm` support.
- Provide automation examples (GitHub Actions, Cloudflare wrangler) covering chunk upload, manifest signing, and cache invalidation.

## Cross-Origin Isolation Options
- Preferred: configure headers (Cloudflare `_headers`, Netlify `_headers`, S3 metadata) with:
  ```
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
  ```
  enabling sqlite-wasm OPFS caching and SharedArrayBuffer features when needed.[1]
- GitHub Pages fallback: ship `coi-serviceworker.js` in `viewer/` and register it before runtime boot; if isolation fails, fall back automatically to httpvfs streaming.
- Highlight SAH-Pool VFS usage for OPFS persistence without SharedArrayBuffer requirements when threads are disabled.[2]
- Viewer diagnostics panel should expose isolation status, selected engine, cache location, and ability to clear persisted data.

## COOP/COEP Recipes
- **Cloudflare Pages `_headers`:**
  ```
  /*
    Cross-Origin-Opener-Policy: same-origin
    Cross-Origin-Embedder-Policy: require-corp
  ```
- **Netlify `_headers`:** same values under `/*`.
- **GitHub Pages:** add `viewer/coi-serviceworker.js` from https://github.com/gzuidhof/coi-serviceworker and register before app bootstrap to emulate isolation.
- Verification checklist: confirm `self.crossOriginIsolated`, `navigator.storage.getDirectory()` access, sqlite-wasm OPFS initialization, and fallback messaging when unavailable.

## Security, Privacy & Integrity
- **Threat model:** shared bundle is world-readable; adversaries may tamper or attempt to infer redacted data.
- **Mitigations:**
  - Hash manifest (SHA-256), include Subresource Integrity attributes for viewer assets, and sign `manifest.json` with Ed25519 (verify client-side via libsodium.js or tweetnacl).[8][13]
  - Optional whole-bundle encryption using age (passphrase or public-key). Viewer decrypts to memory/OPFS before mounting; no SQLCipher/SEE builds required.[9]
  - CLI auto-scrubs secrets: remove bearer tokens, strip ack-required flags, convert agent identities to deterministic pseudonyms.
  - Sanitize rendered Markdown with DOMPurify, enforce CSP + Trusted Types, and keep assets self-hosted.[6][7][12]
  - Support selective publish: default exclude messages marked `importance = 'urgent'` or flagged as private.
  - Provide `--dry-run` report summarizing included agents, threads, attachments, and estimated bundle size.
- **Compliance:**
  - Document consent requirements (agents must allow publication) and encourage review of attachments for licensed material.

## Viewer Security Profile
- Markdown parsing via Marked followed by DOMPurify sanitization with `RETURN_TRUSTED_TYPE` to integrate Trusted Types.[6][7][12]
- Recommended CSP:
  ```
  default-src 'self';
  script-src 'self';
  style-src 'self';
  img-src 'self' data:;
  object-src 'none';
  base-uri 'none';
  frame-ancestors 'none';
  require-trusted-types-for 'script';
  trusted-types mailViewerDOMPurify;
  ```
- Trusted Types policy: `createPolicy('mailViewerDOMPurify', { createHTML: (s) => DOMPurify.sanitize(s, {RETURN_TRUSTED_TYPE: true}) })`.
- Assets are self-hosted with Subresource Integrity hashes; no inline scripts/styles beyond hashed `<style nonce>` allowances.[8]
- Third-party scripts disabled by default; any opt-in requires manifest re-signing and updated CSP guidance.

## Implementation Roadmap (Rough)
0. **Prework (shared infra):**
   - Produce reproducible builds for:
     - `sql.js` compiled with `-DSQLITE_ENABLE_FTS5`.
     - `@sqlite.org/sqlite-wasm` pinned release with SAH-Pool VFS enabled.
   - Create integration test DB covering FTS MATCH, `snippet()`, `bm25`, LIKE fallback, and httpvfs streaming.
    - ✅ *(2025-11-04)* Added SQLite snapshot helper and initial `share export` CLI command (snapshot stage only).
1. **Foundation (1 week):**
   - Research final WASM packaging strategy.
   - ✅ *(2025-11-04)* Bundled sql.js (wasm) + loader so the static viewer queries the exported snapshot directly.
   - Prototype `sql.js` viewer hitting a sample export.
2. **Export CLI (2 weeks):**
   - Snapshot + scrub modules.
   - Attachment bundling + manifest generation.
   - Automated tests for filter accuracy and redaction coverage.
   - ✅ *(2025-11-04)* Interactive wizard now covers project filters, redaction presets, attachment thresholds, chunking, and ZIP opt-in with inline guidance.
   - ✅ *(2025-11-04)* Wizard collects project filters, attachment thresholds, and ZIP packaging choice before export.
   - ✅ *(2025-11-04)* Preview server exposes `/__preview__/status`; viewer polls and hot-reloads bundle changes automatically.
   - ✅ *(2025-11-04)* Prototype export now emits `manifest.json`, `README.txt`, and `HOW_TO_DEPLOY.md` scaffolding alongside the snapshot.
   - ✅ *(2025-11-04)* Default export now creates a deterministic `.zip` archive in addition to the snapshot directory.
   - ✅ *(2025-11-04)* `--project` filters limit exports to selected slugs/human keys and manifest records included scope + removed counts.
   - ✅ *(2025-11-04)* Scrubber pseudonymizes agents, clears ack/read markers, removes file reservations/agent links, and redacts common secret tokens before manifest generation.
   - ✅ *(2025-11-04)* Added scrub presets (`standard`, `strict`) available via CLI flag or wizard, captured in manifest summaries.
   - ✅ *(2025-11-05)* `--dry-run` mode prints a security checklist, attachment breakdown, and search readiness without writing artifacts.
   - ✅ *(2025-11-04)* Attachment bundler hashes assets into `attachments/<sha>/` paths, inlines ≤64 KiB files as data URIs, marks >25 MiB artifacts for external hosting, and records per-message bundle metadata in the manifest.
   - ✅ *(2025-11-04)* `share preview` command serves bundle directories via a local threaded HTTP server with optional browser launch.
   - ✅ *(2025-11-04)* Export now copies shipped viewer scaffold (`viewer/index.html`, `viewer.js`, `styles.css`) so bundles render manifest diagnostics out of the box.
   - ✅ *(2025-11-04)* End-to-end integration test exercises `share export` CLI, validates manifest/ZIP outputs, and emits rich diagnostics for traceability.
3. **Viewer (3 weeks):**
   - ✅ *(2025-11-05)* Built SPA viewer shell with inbox/thread/detail flows, FTS-backed search, and thread navigation.
   - ✅ *(2025-11-04)* Viewer loads bundled SQLite via sql.js (with chunk reassembly fallback) and hydrates message list without server APIs.
   - ✅ *(2025-11-05)* Added OPFS-aware caching toggle plus analytics-free bootstrap telemetry (console diagnostics only).
4. **Security & Hardening (1 week):**
   - Hash/signing pipeline.
   - Optional encryption module.
   - Documentation and sample CI workflow.
   - ✅ *(2025-11-04)* CLI supports Ed25519 manifest signing (`--signing-key`) and optional age encryption for ZIP bundles (`--age-recipient`).
5. **Beta & Feedback (1 week):**
   - Publish sample bundle in repo `docs/share-demo/`.
   - Gather feedback, tune performance (chunking, query optimization).

### Deployment Automation Samples
- ✅ *(2025-11-05)* Added GitHub Actions workflow example (`docs/deployment_samples/github-actions-share.yml`).
- ✅ *(2025-11-05)* Added Cloudflare Pages `wrangler.toml` example (`docs/deployment_samples/cloudflare-pages-example.toml`).

## Testing Strategy
- Unit tests for scrubbing rules, pseudonym determinism, and secret redaction.
  - ✅ *(2025-11-04)* Added unit coverage for scrubbing + attachment bundling (`tests/test_share_export.py`).
- Integration tests that exercise the CLI end-to-end with rich logging to validate manifest contents, attachment packaging, and ZIP bundles.
  - ✅ *(2025-11-04)* Added `tests/integration/test_mailbox_share_integration.py` covering export + preview server workflow.
- Snapshot tests for manifest integrity, schema_version, compile_options, and Ed25519 signatures.
  - ✅ *(2025-11-05)* `test_manifest_snapshot_structure` validates manifest schema/version/summary fields after export.
- Playwright E2E tests against static viewer (local http server): verify engine auto-selection, search results, attachments, and CSP enforcement.
  - ✅ *(2025-11-05)* `test_viewer_playwright_smoke` runs Chromium to ensure viewer loads, blocks XSS, and surfaces metadata.
- Performance benchmarks: measure first meaningful paint on 10 MB, 100 MB, 500 MB bundles under httpvfs streaming; confirm warm-load OPFS cache hits.
- Query locality validation: run `EXPLAIN QUERY PLAN` + `dbstat` checks to ensure covering indexes minimize random seeks.[3]
- Cross-browser storage tests: Chrome, Firefox, Safari covering OPFS (with SAH-Pool) and IndexedDB limits.
- XSS test corpus (malicious Markdown) to validate DOMPurify + Trusted Types; run security regression suite on each release.[6][7][12]
- Encryption tests: age-encrypted bundles (passphrase + public key) with manifest signature verification and decryption failure handling.[9]

## Open Questions
- Should we offer per-message share toggles within the main app to mark sensitive threads as non-exportable?
- Does MVP need both passphrase and public-key age support, or can we ship one mode first?
- Do we need localization support in the viewer at launch?
- How do we handle extremely large attachments (>250 MB) while keeping static hosting feasible (e.g., mandatory external object storage)?
- Should we build a bundle-diff tool that signs before/after manifests and publishes only changed chunks + attachments?
- Would a lightweight GUI/desktop wrapper for the export wizard materially improve usability, or would it complicate maintenance relative to the CLI experience?

## References
1. SQLite.org, "SQLite Wasm: accessing database files via OPFS and COOP/COEP requirements," accessed November 4, 2025.
2. SQLite.org, "Shared-Access Handles (SAH) Pool VFS" reference, accessed November 4, 2025.
3. phiresky, "sql.js-httpvfs" GitHub README (Range-based SQLite VFS), accessed November 4, 2025.
4. James Long et al., "absurd-sql" documentation (IndexedDB-backed SQLite), accessed November 4, 2025.
5. sql.js project docs, "Compiling with custom SQLite options" (including -DSQLITE_ENABLE_FTS5), accessed November 4, 2025.
6. DOMPurify documentation and API reference, accessed November 4, 2025.
7. Marked.js project, "Security" guidance (sanitization warning), accessed November 4, 2025.
8. MDN Web Docs, "Subresource Integrity (SRI)", accessed November 4, 2025.
9. Filippo Valsorda, "age" encryption specification and tooling guides, accessed November 4, 2025.
10. GitHub Docs, "About GitHub Pages" (repository/file size limits and MIME guidance), accessed November 4, 2025.
11. Cloudflare Pages documentation, "Limits and usage quotas" (25 MiB per-file limit, _headers), accessed November 4, 2025.
12. Google web.dev, "Trusted Types" overview, accessed November 4, 2025.
13. libsodium.js and TweetNaCl.js documentation (Ed25519 signature verification for browsers), accessed November 4, 2025.
14. SQLite.org documentation, "VACUUM" (including `VACUUM INTO` usage and precautions), accessed November 4, 2025.
15. SQLite.org, "Write-Ahead Logging" (WAL) documentation, accessed November 4, 2025.
16. SQLite.org, "Online Backup API" reference, accessed November 4, 2025.
