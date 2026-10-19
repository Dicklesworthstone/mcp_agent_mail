"""Browser regression for the web UI icon pipeline (GH #276).

``lucide.createIcons()`` replaces every ``[data-lucide]`` node, including ``<svg>``
icons it already rendered. ``base.html`` used to re-run it after *any* DOM mutation,
so its own swaps re-triggered the observer forever: every icon was rebuilt about ten
times per second, and the refresh spinner restarted its animation on each swap and
kept spinning after the refresh finished (the copied ``animate-spin`` class was no
longer owned by Alpine).

Gated like the DOMPurify sink test in ``test_xss_corpus.py``: skipped (not failed)
when Playwright, a Chromium build, or the CDN-hosted Alpine/Lucide scripts that the
page loads are unavailable.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.http import build_http_app

REFRESH_BUTTON = "button[aria-label='Refresh inbox']"

# Hold the unified-inbox refresh request open so the spinner is observable, then
# record every swapped-out Lucide <svg> and every element that spins in the button.
_INSTRUMENT_JS = """(selector) => {
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init) => String(input).includes('/mail/api/unified-inbox')
    ? new Promise((resolve) => setTimeout(resolve, 600)).then(() => realFetch(input, init))
    : realFetch(input, init);

  const probe = { swaps: 0, spinSamples: 0, spinNodes: new Set() };
  window.__iconProbe = probe;
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.removedNodes) {
        if (node instanceof SVGElement && node.hasAttribute('data-lucide')) probe.swaps += 1;
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
  probe.timer = setInterval(() => {
    const spinning = document.querySelector(`${selector} .animate-spin`);
    if (spinning) {
      probe.spinSamples += 1;
      probe.spinNodes.add(spinning);
    }
  }, 16);
}"""


@pytest.fixture
def live_ui(isolated_env) -> Iterator[str]:
    """Serve the real HTTP app (isolated DB and storage) on an ephemeral localhost port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_http_app(get_settings()), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail("Web UI server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


def test_refresh_spinner_is_stable_and_icon_rendering_settles(live_ui: str) -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")
    try:
        pw_cm = playwright_sync.sync_playwright()
        playwright = pw_cm.__enter__()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Playwright runtime unavailable: {exc}")

    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"Chromium not installed for Playwright: {exc}")

        try:
            context = browser.new_context()
            # Keep the first-visit tutorial overlay from covering the toolbar.
            context.add_init_script(
                "try { localStorage.setItem('agentmail_tutorial_completed', 'true'); } catch (e) {}"
            )
            page = context.new_page()
            try:
                page.goto(f"{live_ui}/mail", wait_until="load")
                page.wait_for_function(
                    "(selector) => window.Alpine && window.lucide && document.querySelector(`${selector} svg`)",
                    arg=REFRESH_BUTTON,
                    timeout=20_000,
                )
            except playwright_sync.TimeoutError:  # pragma: no cover - offline CDN
                pytest.skip("Alpine/Lucide CDN scripts unavailable")

            page.evaluate(_INSTRUMENT_JS, REFRESH_BUTTON)
            page.click(REFRESH_BUTTON)
            page.wait_for_function(
                "(selector) => Alpine.$data(document.querySelector(selector)).isRefreshing === false",
                arg=REFRESH_BUTTON,
                timeout=10_000,
            )
            # Let any queued icon pass (100 ms debounce, $nextTick renders) finish.
            page.wait_for_timeout(1_000)
            spin = page.evaluate(
                """(selector) => {
                  clearInterval(window.__iconProbe.timer);
                  return {
                    samples: window.__iconProbe.spinSamples,
                    nodes: window.__iconProbe.spinNodes.size,
                    stillSpinning: Boolean(document.querySelector(`${selector} .animate-spin`)),
                  };
                }""",
                REFRESH_BUTTON,
            )
            page.evaluate("() => { window.__iconProbe.swaps = 0; }")
            page.wait_for_timeout(1_000)
            idle_swaps = page.evaluate("() => window.__iconProbe.swaps")
        finally:
            browser.close()
    finally:
        pw_cm.__exit__(None, None, None)

    assert spin["samples"] > 0, "spinner never showed while the refresh request was in flight"
    assert spin["nodes"] == 1, f"spinner element was replaced {spin['nodes'] - 1} times mid-animation"
    assert not spin["stillSpinning"], "spinner kept spinning after the refresh finished"
    assert idle_swaps == 0, f"Lucide re-rendered {idle_swaps} icons on an idle page"
