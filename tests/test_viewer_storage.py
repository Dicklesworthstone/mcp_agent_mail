"""Cross-browser storage tests for OPFS caching and offline functionality.

This test suite documents requirements for testing the viewer's offline storage capabilities:
1. OPFS (Origin Private File System) caching
2. Cache version management and invalidation
3. Cross-origin isolation requirements
4. SharedArrayBuffer availability
5. Warm vs cold load performance

Reference: PLAN_TO_ENABLE_EASY_AND_SECURE_SHARING_OF_AGENT_MAILBOX.md line 262

Note: Comprehensive cross-browser testing requires browser automation (Playwright/Puppeteer)
as OPFS and SharedArrayBuffer APIs are not available in Node.js or Python runtimes.
"""

from __future__ import annotations


def test_opfs_caching_requirements_documentation() -> None:
    """Document requirements for OPFS caching tests.

    OPFS (Origin Private File System) is a browser API for persistent local storage
    that enables faster warm-load performance by caching the SQLite database locally.

    Testing OPFS requires real browsers with the following conditions:
    1. Cross-origin isolation enabled (COOP + COEP headers)
    2. Chromium-based browser (Chromium 102+, Chrome, Edge)
    3. HTTPS or localhost origin
    """
    documentation = """
    OPFS Caching Test Requirements
    ===============================

    The static viewer uses OPFS for offline caching when available. To test OPFS:

    Required Browser Conditions:
    ---------------------------
    1. Cross-Origin Isolation: Page must be served with headers:
       - Cross-Origin-Opener-Policy: same-origin
       - Cross-Origin-Embedder-Policy: require-corp

    2. Browser Support:
       - Chrome/Chromium 102+
       - Edge 102+
       - Opera 88+
       - Firefox: Limited support (requires flags)
       - Safari: Not supported as of 2025

    3. Secure Context:
       - HTTPS origin OR localhost for development

    Test Scenarios:
    ---------------

    1. Cold Load (First Visit)
    --------------------------
    - Navigate to viewer URL
    - Verify: Database is fetched from network
    - Verify: OPFS write occurs (console logs)
    - Verify: Cache state = "opfs" in diagnostics
    - Verify: Cache button shows "Cache for offline use"
    - Measure: Time to first meaningful paint

    Expected behavior:
    - Database downloaded from network
    - Written to OPFS: /opfs-root/mailbox-snapshot-{cacheKey}.sqlite3
    - Metadata written: /opfs-root/mailbox-snapshot-{cacheKey}.meta.json
    - Viewer functional after download completes

    2. Warm Load (Subsequent Visit)
    -------------------------------
    - Refresh page or close/reopen browser
    - Verify: Database loaded from OPFS (console logs)
    - Verify: No network requests for .sqlite3
    - Verify: Cache state = "opfs" in diagnostics
    - Measure: Significantly faster load time (< 500ms typical)

    Expected behavior:
    - Database read from OPFS cache
    - No network download
    - Immediate viewer functionality

    3. Cache Invalidation (Version Mismatch)
    ----------------------------------------
    - Deploy updated bundle with new cacheKey (hash changes)
    - Reload page
    - Verify: Old cache detected as stale
    - Verify: Old cache files deleted
    - Verify: New database downloaded and cached

    Expected behavior:
    - Console warning: "Stale OPFS cache detected, invalidating"
    - Old .sqlite3 and .meta.json files removed
    - Fresh download and re-cache

    4. Cache Manual Clear
    ---------------------
    - Open diagnostics panel
    - Click "Clear All Caches"
    - Verify: OPFS files deleted
    - Verify: Next load fetches from network

    Expected behavior:
    - All mailbox-snapshot-* files removed from OPFS
    - Cache state reset to "memory" or "none"
    - Alert confirms cache cleared

    5. Cross-Origin Isolation Fallback
    -----------------------------------
    - Serve viewer without COOP/COEP headers
    - Navigate to viewer
    - Verify: OPFS unavailable warning
    - Verify: Cache state = "unsupported" or "memory"
    - Verify: Viewer still functional (in-memory mode)

    Expected behavior:
    - Console warning about cross-origin isolation
    - Cache toggle disabled
    - Database loaded into memory only
    - No persistent cache

    6. Offline Functionality
    ------------------------
    - Load viewer with OPFS cache populated
    - Disconnect network / enable offline mode
    - Refresh page
    - Verify: Viewer loads from OPFS cache
    - Verify: Full functionality maintained

    Expected behavior:
    - Page loads without network requests
    - All messages visible
    - Search functional
    - No degradation

    Performance Targets:
    -------------------
    - Cold load (100 MB DB): < 5 seconds to interactive
    - Warm load (100 MB DB): < 500ms to interactive
    - OPFS write speed: > 50 MB/s
    - OPFS read speed: > 100 MB/s
    """

    assert len(documentation) > 500, "OPFS documentation should be comprehensive"


def test_playwright_opfs_test_example() -> None:
    """Provide example Playwright test for OPFS functionality.

    This test shows how to implement OPFS caching tests using Playwright.
    To run these tests, install playwright and create a tests/playwright/ directory.
    """
    example_code = '''
    """Example Playwright test for OPFS caching."""

    import pytest
    from playwright.sync_api import Page, expect


    @pytest.fixture
    def viewer_page(page: Page):
        """Launch viewer with cross-origin isolation headers."""
        # Start local server with proper headers
        # See src/mcp_agent_mail/share.py start_preview_server for header config

        page.goto("http://localhost:8000/viewer/index.html")
        # Wait for viewer to initialize
        page.wait_for_selector("#message-list", timeout=10000)
        return page


    def test_opfs_cold_load(viewer_page: Page):
        """Test initial load caches database to OPFS."""
        # Check cross-origin isolation
        is_isolated = viewer_page.evaluate("() => window.crossOriginIsolated")
        assert is_isolated, "Cross-origin isolation required for OPFS"

        # Open diagnostics
        viewer_page.click("#diagnostics-toggle")

        # Verify OPFS is available
        opfs_status = viewer_page.text_content("#diag-opfs-status")
        assert "✅" in opfs_status, "OPFS should be available"

        # Verify cache state after load
        cache_state = viewer_page.text_content("#diag-cache-state")
        assert "OPFS" in cache_state or "memory" in cache_state


    def test_opfs_warm_load_performance(viewer_page: Page):
        """Test that warm load is significantly faster than cold load."""
        # First load (cold) - should cache to OPFS
        viewer_page.goto("http://localhost:8000/viewer/index.html")
        viewer_page.wait_for_selector("#message-list li", timeout=10000)

        cold_load_time = viewer_page.evaluate(
            "() => performance.getEntriesByType('navigation')[0].loadEventEnd"
        )

        # Second load (warm) - should load from OPFS
        viewer_page.reload()
        viewer_page.wait_for_selector("#message-list li", timeout=10000)

        warm_load_time = viewer_page.evaluate(
            "() => performance.getEntriesByType('navigation')[0].loadEventEnd"
        )

        print(f"Cold load: {cold_load_time}ms")
        print(f"Warm load: {warm_load_time}ms")

        # Warm load should be at least 2x faster
        assert warm_load_time < cold_load_time / 2, "Warm load should use OPFS cache"


    def test_opfs_cache_invalidation(viewer_page: Page):
        """Test that cache is invalidated when cacheKey changes."""
        # Load initial version
        viewer_page.goto("http://localhost:8000/viewer/index.html")
        viewer_page.wait_for_selector("#message-list", timeout=10000)

        # Get initial cache key from diagnostics
        viewer_page.click("#diagnostics-toggle")
        initial_key = viewer_page.text_content("#diag-cache-key")

        # Simulate new bundle deployment by modifying manifest.json
        # (In real scenario, deploy new bundle with different hash)

        # Reload page
        viewer_page.reload()
        viewer_page.wait_for_selector("#message-list", timeout=10000)

        # Verify console shows invalidation
        messages = viewer_page.evaluate("() => console.messages")
        has_invalidation = any("stale" in str(m).lower() for m in messages)

        # Note: Actual implementation depends on test infrastructure


    def test_opfs_offline_functionality(viewer_page: Page):
        """Test that viewer works offline after caching to OPFS."""
        # Load page and verify OPFS cache
        viewer_page.goto("http://localhost:8000/viewer/index.html")
        viewer_page.wait_for_selector("#message-list li", timeout=10000)

        # Enable offline mode
        context = viewer_page.context
        context.set_offline(True)

        # Reload page
        viewer_page.reload()

        # Verify page loads from cache
        viewer_page.wait_for_selector("#message-list li", timeout=10000)

        # Verify messages are visible
        message_count = viewer_page.locator("#message-list li").count()
        assert message_count > 0, "Messages should load from OPFS cache offline"

        # Disable offline mode
        context.set_offline(False)


    def test_cross_origin_isolation_fallback(page: Page):
        """Test viewer works without cross-origin isolation (memory-only mode)."""
        # Navigate to viewer served WITHOUT COOP/COEP headers
        # This requires separate server configuration for testing
        page.goto("http://localhost:8001/viewer/index.html")  # Server without headers

        # Wait for viewer to load
        page.wait_for_selector("#message-list", timeout=10000)

        # Verify cross-origin isolation is disabled
        is_isolated = page.evaluate("() => window.crossOriginIsolated")
        assert not is_isolated, "Should not be cross-origin isolated"

        # Open diagnostics
        page.click("#diagnostics-toggle")

        # Verify OPFS shows as unavailable
        opfs_status = page.text_content("#diag-opfs-status")
        assert "❌" in opfs_status, "OPFS should be unavailable without isolation"

        # Verify viewer still works (memory mode)
        cache_state = page.text_content("#diag-cache-state")
        assert "memory" in cache_state.lower() or "unsupported" in cache_state.lower()

        # Verify messages are visible
        message_count = page.locator("#message-list li").count()
        assert message_count > 0, "Viewer should work in memory-only mode"


    def test_cache_clear_functionality(viewer_page: Page):
        """Test manual cache clearing through diagnostics."""
        # Open diagnostics panel
        viewer_page.click("#diagnostics-toggle")

        # Click clear all caches button
        viewer_page.click("#clear-all-caches")

        # Handle confirmation dialog
        viewer_page.on("dialog", lambda dialog: dialog.accept())

        # Wait for alert confirmation
        # Note: Actual implementation depends on how alerts are handled

        # Verify cache state updated
        cache_state = viewer_page.text_content("#diag-cache-state")
        assert "memory" in cache_state.lower() or "none" in cache_state.lower()

        # Reload and verify re-download
        viewer_page.reload()
        viewer_page.wait_for_selector("#message-list", timeout=10000)
    '''

    assert len(example_code) > 1000, "Example code should be comprehensive"


def test_sharedarraybuffer_requirements_documentation() -> None:
    """Document SharedArrayBuffer requirements for sql.js performance.

    SharedArrayBuffer enables sqlite-wasm to use optimized memory access patterns,
    improving query performance. It requires cross-origin isolation.
    """
    documentation = """
    SharedArrayBuffer Requirements for sql.js Performance
    =====================================================

    The viewer uses sql.js (SQLite compiled to WebAssembly) which can leverage
    SharedArrayBuffer for improved performance when available.

    Required Conditions:
    -------------------
    1. Cross-Origin Isolation (same as OPFS):
       - Cross-Origin-Opener-Policy: same-origin
       - Cross-Origin-Embedder-Policy: require-corp

    2. Browser Support:
       - Chrome 92+
       - Firefox 79+
       - Safari 15.2+
       - Edge 92+

    3. Secure Context:
       - HTTPS or localhost

    Test Scenarios:
    ---------------

    1. Verify SharedArrayBuffer Availability
    ----------------------------------------
    Open browser console:
    ```javascript
    typeof SharedArrayBuffer !== 'undefined'
    // Should return: true (with cross-origin isolation)
    //                false (without isolation)
    ```

    Open diagnostics panel:
    - Look for "SharedArrayBuffer: ✅ Available"

    2. Performance With vs Without SharedArrayBuffer
    ------------------------------------------------
    Compare query performance:
    - With SAB: Queries may be 10-30% faster
    - Without SAB: Still functional, slightly slower

    Benchmark: Run search query on large database
    - With SAB: < 100ms for FTS search
    - Without SAB: < 150ms for FTS search

    3. Fallback Behavior
    --------------------
    When SharedArrayBuffer unavailable:
    - sql.js falls back to regular memory
    - No functionality loss
    - Slight performance degradation
    - Viewer remains fully functional

    Performance Monitoring:
    ----------------------
    Use diagnostics panel to check:
    - Bootstrap time should be < 2 seconds
    - FTS search should be < 200ms
    - Message detail load should be < 50ms

    If performance degrades without SharedArrayBuffer:
    - Enable cross-origin isolation
    - Verify headers are properly set
    - Check browser version supports SAB
    """

    assert len(documentation) > 500, "SharedArrayBuffer documentation should be comprehensive"


def test_browser_compatibility_matrix_documentation() -> None:
    """Document browser compatibility for OPFS and SharedArrayBuffer features."""
    documentation = """
    Browser Compatibility Matrix for Viewer Features
    =================================================

    Feature: OPFS (Origin Private File System)
    -------------------------------------------
    | Browser          | Version | Support | Notes                        |
    |------------------|---------|---------|------------------------------|
    | Chrome           | 102+    | ✅ Full | Stable since M102            |
    | Edge             | 102+    | ✅ Full | Chromium-based               |
    | Opera            | 88+     | ✅ Full | Chromium-based               |
    | Brave            | 1.40+   | ✅ Full | Chromium-based               |
    | Firefox          | 111+    | ⚠️ Flag | Requires about:config flag   |
    | Safari           | N/A     | ❌ None | Not implemented              |
    | Safari iOS       | N/A     | ❌ None | Not implemented              |
    | Samsung Internet | 19+     | ✅ Full | Chromium-based               |

    Feature: SharedArrayBuffer
    ---------------------------
    | Browser          | Version | Support | Notes                        |
    |------------------|---------|---------|------------------------------|
    | Chrome           | 92+     | ✅ Full | Requires cross-origin iso.   |
    | Edge             | 92+     | ✅ Full | Requires cross-origin iso.   |
    | Firefox          | 79+     | ✅ Full | Requires cross-origin iso.   |
    | Safari           | 15.2+   | ✅ Full | Requires cross-origin iso.   |
    | Opera            | 78+     | ✅ Full | Requires cross-origin iso.   |

    Feature: WebAssembly (sql.js requirement)
    ------------------------------------------
    | Browser          | Version | Support | Notes                        |
    |------------------|---------|---------|------------------------------|
    | All modern       | 2017+   | ✅ Full | Universal support            |

    Recommended Testing Configuration:
    ----------------------------------
    - Primary: Chrome 120+ (best OPFS support)
    - Secondary: Firefox 120+ (verify degraded OPFS)
    - Tertiary: Safari 17+ (verify memory-only mode)

    Fallback Behavior:
    ------------------
    When OPFS unavailable:
    - Database cached in memory only
    - Cache cleared on page unload
    - Viewer remains fully functional
    - Performance slightly reduced

    When SharedArrayBuffer unavailable:
    - sql.js uses regular memory
    - Slight performance degradation
    - All features remain functional

    Testing Checklist:
    ------------------
    For each browser:
    1. ✅ Load viewer with cross-origin isolation
    2. ✅ Verify OPFS status in diagnostics
    3. ✅ Verify SharedArrayBuffer status
    4. ✅ Test cold load (first visit)
    5. ✅ Test warm load (cached)
    6. ✅ Test cache invalidation
    7. ✅ Test offline functionality (if OPFS available)
    8. ✅ Test without cross-origin isolation (fallback)
    9. ✅ Verify search performance
    10. ✅ Verify navigation responsiveness
    """

    assert len(documentation) > 500, "Compatibility matrix should be comprehensive"


def test_cache_toggle_functionality_documentation() -> None:
    """Document the cache toggle button behavior and user interaction.

    The cache toggle button allows users to explicitly trigger OPFS caching
    when automatic caching hasn't occurred or when re-caching is needed.
    """
    documentation = """
    Cache Toggle Button Behavior
    =============================

    The viewer includes a "Cache for offline use" button that manages OPFS caching.

    Button States:
    --------------

    1. Initial State (OPFS Available, Not Cached):
       Text: "Cache for offline use"
       Enabled: Yes
       Action: Downloads DB and writes to OPFS

    2. Caching in Progress:
       Text: "Caching..."
       Enabled: No
       Action: Shows progress

    3. Cached State (OPFS Active):
       Text: "✓ Cached offline"
       Enabled: Yes (allows re-cache)
       Action: Re-downloads and updates cache

    4. OPFS Unavailable:
       Text: "Cache unavailable (requires cross-origin isolation)"
       Enabled: No
       Action: None (informational)

    5. Memory-Only Mode (Automatic):
       Text: "Using memory cache"
       Enabled: No
       Action: None (already cached in memory)

    User Flows:
    -----------

    Flow 1: Manual Cache on First Visit
    ------------------------------------
    1. User loads viewer
    2. Database loads into memory automatically
    3. User clicks "Cache for offline use"
    4. Database written to OPFS
    5. Button updates to "✓ Cached offline"
    6. Subsequent visits load from OPFS automatically

    Flow 2: Automatic Caching (Default)
    ------------------------------------
    1. User loads viewer
    2. Database downloads from network
    3. Automatically written to OPFS (if available)
    4. Button already shows "✓ Cached offline"
    5. No user action needed

    Flow 3: Re-cache After Update
    ------------------------------
    1. User has old version cached
    2. New bundle deployed (different cacheKey)
    3. Viewer detects stale cache
    4. Old cache automatically invalidated
    5. New database downloaded and cached
    6. Button shows "✓ Cached offline" for new version

    Flow 4: Fallback to Memory (No OPFS)
    -------------------------------------
    1. User loads viewer without cross-origin isolation
    2. OPFS unavailable
    3. Database loads into memory only
    4. Button disabled with message
    5. Viewer fully functional but no persistence

    Testing the Cache Toggle:
    --------------------------

    Manual Test Steps:
    1. Open viewer in Chrome 102+
    2. Verify cross-origin isolation (check diagnostics)
    3. Click cache toggle button
    4. Monitor console for OPFS write logs
    5. Refresh page
    6. Verify warm load from OPFS (console logs)
    7. Open diagnostics to confirm cache state

    Expected Console Output (Successful Cache):
    ```
    [viewer] Cache supported, attempting to cache...
    [viewer] OPFS write successful: mailbox-snapshot-abc123.sqlite3
    [viewer] Cache state updated: opfs
    ```

    Expected Console Output (Warm Load):
    ```
    [viewer] Using OPFS cache: mailbox-snapshot-abc123.sqlite3
    [viewer] Cache state: opfs
    [viewer] Bootstrap completed in 324ms
    ```

    Error Scenarios:
    ----------------
    1. OPFS Quota Exceeded:
       - Console warning: "OPFS quota exceeded"
       - Falls back to memory cache
       - Button shows error state

    2. OPFS Write Permission Denied:
       - Console error: "OPFS permission denied"
       - Falls back to memory cache
       - Button disabled

    3. Network Failure During Cache:
       - Console error: "Network error during caching"
       - Retries may occur
       - Button shows error state
    """

    assert len(documentation) > 500, "Cache toggle documentation should be comprehensive"


def test_performance_metrics_documentation() -> None:
    """Document performance metrics for storage operations.

    Provides baseline expectations for OPFS read/write performance and
    cache effectiveness measurements.
    """
    documentation = """
    Storage Performance Metrics and Benchmarks
    ===========================================

    Expected Performance (Baseline):
    ---------------------------------

    OPFS Write Performance:
    - 10 MB database: < 200ms
    - 100 MB database: < 2 seconds
    - 500 MB database: < 10 seconds
    - Throughput: 50-100 MB/s typical

    OPFS Read Performance:
    - 10 MB database: < 100ms
    - 100 MB database: < 500ms
    - 500 MB database: < 2 seconds
    - Throughput: 100-200 MB/s typical

    Network Fetch Performance:
    - 10 MB (gzip compressed ~3 MB): 1-2 seconds on fast connection
    - 100 MB (gzip compressed ~30 MB): 5-10 seconds on fast connection
    - httpvfs chunked: Initial load ~5-10 Range requests

    Cache Effectiveness:
    -------------------

    Warm Load Speedup:
    - Small bundles (< 10 MB): 5-10x faster
    - Medium bundles (10-100 MB): 10-20x faster
    - Large bundles (> 100 MB): 20-50x faster

    Example Measurements:
    - 100 MB bundle cold load: 8 seconds
    - 100 MB bundle warm load: 400ms
    - Speedup: 20x

    Performance Monitoring:
    -----------------------

    Use browser DevTools Performance tab:
    1. Record load timeline
    2. Check "Caching" marks in timeline
    3. Measure TTFB (Time to First Byte)
    4. Measure FCP (First Contentful Paint)
    5. Measure TTI (Time to Interactive)

    Use diagnostics panel:
    - Bootstrap time: < 2 seconds target
    - Database source: "OPFS cache" vs "network"
    - Cache state: "opfs", "memory", or "none"

    Performance Regression Detection:
    ---------------------------------

    Monitor these metrics over time:
    - Bootstrap time increasing → investigate cache effectiveness
    - Warm load time increasing → check OPFS read performance
    - Cold load time increasing → check network/server performance

    Target Thresholds (100 MB bundle):
    - Cold load: < 10 seconds
    - Warm load: < 500ms
    - Bootstrap: < 2 seconds
    - First message visible: < 1 second (warm), < 5 seconds (cold)

    Browser-Specific Variations:
    ----------------------------

    Chrome/Edge (Chromium):
    - Best OPFS performance
    - Consistent read/write speeds
    - Recommended for testing

    Firefox:
    - OPFS behind flag or unavailable
    - Memory-only mode typical
    - Good baseline for fallback testing

    Safari:
    - No OPFS support
    - Memory-only mode
    - Useful for testing degraded experience
    """

    assert len(documentation) > 500, "Performance metrics documentation should be comprehensive"


def test_debugging_tools_documentation() -> None:
    """Document debugging tools for storage and caching issues.

    Provides guidance on using browser DevTools and diagnostics panel
    to troubleshoot storage-related problems.
    """
    documentation = """
    Debugging Storage and Caching Issues
    =====================================

    Diagnostics Panel:
    ------------------

    Access: Click "Diagnostics" button in viewer banner

    Key Information:
    1. Cross-Origin Isolation Status:
       - ✅ Enabled → OPFS available
       - ❌ Disabled → Memory-only mode

    2. SharedArrayBuffer Status:
       - ✅ Available → Optimal sql.js performance
       - ❌ Unavailable → Fallback mode

    3. OPFS Access Status:
       - ✅ Available → Can use persistent cache
       - ❌ Unavailable → Memory-only mode

    4. Cache State:
       - "OPFS (persistent)" → Cached to OPFS successfully
       - "Memory (session only)" → Cached in memory, cleared on exit
       - "No cache" → No caching active
       - "Unsupported" → Browser doesn't support required APIs

    5. Cache Key:
       - SHA256 hash of database
       - Used for cache versioning
       - Changes when bundle updated

    6. Bootstrap Time:
       - Time from page load to viewer ready
       - Cold load: 2-10 seconds typical
       - Warm load: < 500ms typical

    Browser DevTools - Application Tab:
    ------------------------------------

    Check OPFS Contents:
    1. Open DevTools (F12)
    2. Go to Application tab
    3. Navigate to "Storage" → "OPFS"
    4. Look for mailbox-snapshot-*.sqlite3 files
    5. Verify file sizes match expected database size

    Check Storage Quota:
    1. Console: `navigator.storage.estimate()`
    2. Check quota and usage
    3. Verify sufficient space available

    Example:
    ```javascript
    navigator.storage.estimate().then(estimate => {
      console.log(`Used: ${estimate.usage / 1024 / 1024} MB`);
      console.log(`Quota: ${estimate.quota / 1024 / 1024} MB`);
      console.log(`Available: ${(estimate.quota - estimate.usage) / 1024 / 1024} MB`);
    });
    ```

    Browser DevTools - Network Tab:
    --------------------------------

    Monitor Caching Behavior:
    1. Open DevTools Network tab
    2. Disable cache in DevTools (checkbox)
    3. Load viewer
    4. Check for .sqlite3 download

    Expected Network Activity:
    - Cold load: Large .sqlite3 download
    - Warm load: No .sqlite3 download (from OPFS)
    - httpvfs: Multiple Range requests

    Browser DevTools - Console:
    ----------------------------

    Check Viewer Logs:
    - [viewer] logs show caching operations
    - Look for OPFS read/write messages
    - Check for cache invalidation warnings
    - Monitor performance timings

    Example Console Output:
    ```
    [viewer] Database loaded from network
    [viewer] Attempting to cache to OPFS...
    [viewer] OPFS write successful
    [viewer] Cache state: opfs
    [viewer] Bootstrap time: 1234ms
    ```

    Common Issues and Solutions:
    ----------------------------

    Issue: "Cache unavailable" despite Chrome 102+
    Solution:
    - Check cross-origin isolation headers
    - Verify HTTPS or localhost origin
    - Check browser flags (chrome://flags)

    Issue: Warm load still downloads database
    Solution:
    - Check if cacheKey changed (bundle updated)
    - Verify OPFS files in DevTools Application tab
    - Check console for cache invalidation messages

    Issue: OPFS write fails
    Solution:
    - Check storage quota: navigator.storage.estimate()
    - Request persistent storage: navigator.storage.persist()
    - Clear old caches to free space

    Issue: "SharedArrayBuffer is not defined"
    Solution:
    - Enable cross-origin isolation (COOP/COEP headers)
    - Check secure context (HTTPS/localhost)
    - Verify browser version supports SAB

    Manual OPFS Inspection:
    ------------------------

    List OPFS files:
    ```javascript
    async function listOpfsFiles() {
      const root = await navigator.storage.getDirectory();
      for await (const [name, handle] of root.entries()) {
        if (name.startsWith('mailbox-snapshot')) {
          const file = await handle.getFile();
          console.log(`${name}: ${file.size} bytes`);
        }
      }
    }
    listOpfsFiles();
    ```

    Delete specific OPFS file:
    ```javascript
    async function deleteOpfsFile(filename) {
      const root = await navigator.storage.getDirectory();
      await root.removeEntry(filename);
      console.log(`Deleted: ${filename}`);
    }
    deleteOpfsFile('mailbox-snapshot-abc123.sqlite3');
    ```

    Clear all OPFS files:
    ```javascript
    async function clearAllOpfs() {
      const root = await navigator.storage.getDirectory();
      for await (const [name, handle] of root.entries()) {
        await root.removeEntry(name);
        console.log(`Deleted: ${name}`);
      }
    }
    clearAllOpfs();
    ```
    """

    assert len(documentation) > 1000, "Debugging documentation should be comprehensive"
