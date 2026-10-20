"""Unified-inbox conversation navigation at the Jinja/JavaScript boundary.

Execute the production component and its rendered link expressions in Node.
These are not full HTTP-service or browser/Alpine integration tests.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "mcp_agent_mail" / "templates"
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Page(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self._stack: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)
        self.close()
        assert not self._stack, "Unclosed elements in the production template"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"a", "button"}:
            assert not any(
                parent in {"a", "button"} or values.get("role") == "button"
                for parent, values in self._stack
            ), "Navigation must not nest inside another link or button"
        self.elements.append({"tag": tag, "attrs": attributes, "ancestors": list(self._stack)})
        if tag == "script":
            self.scripts.append("")
        if tag not in VOID_ELEMENTS:
            self._stack.append((tag, attributes))

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID_ELEMENTS:
            assert self._stack and self._stack[-1][0] == tag, f"Mismatched closing tag: {tag}"
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][0] == "script":
            self.scripts[-1] += data

    @property
    def links(self) -> list[dict[str, Any]]:
        return [element for element in self.elements if element["tag"] == "a"]


def _message(mid: int = 100, thread_id: str | None = "release", project: str = "alpha") -> dict[str, Any]:
    return {
        "id": mid,
        "thread_id": thread_id,
        "project_slug": project,
        "project_name": project.title(),
        "sender_project_slug": "unrelated-sender-project",
        "sender": "Remote Agent",
        "subject": "Status update",
        "recipients": "BlueLake, RedStone",
        "importance": "normal",
        "created_ts": f"2026-01-01T00:00:{mid % 60:02d}Z",
        "body_md": "",
        "excerpt": "",
    }


def _render(messages: list[dict[str, Any]]) -> _Page:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=True,
    )
    template = env.get_template("mail_unified_inbox.html")

    async def render_content() -> str:
        context = template.new_context({"projects": [], "messages": messages})
        chunks = cast(AsyncIterator[str], template.blocks["content"](context))
        return "".join([chunk async for chunk in chunks])

    return _Page(asyncio.run(render_content()))


@pytest.fixture
def node() -> str:
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js is required to execute the production inbox JavaScript")
    return executable


# The only substitutes are browser APIs and explicitly supplied refresh responses.
# The manager, URL builders, filter logic, and link expressions are production code.
NODE_RUNNER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = vm.createContext({
  assert, payload: data.payload, links: data.links, elements: data.elements, URL,
  window: {location: {origin: 'http://test', href: '/mail'}},
  document: {querySelector: () => null, getElementById: () => null},
  navigator: {}, console: {error: () => {}, warn: () => {}},
  setInterval: () => 1, clearInterval: () => {}, setTimeout: () => 1
});
vm.runInContext(data.script, context, {timeout: 5000});
vm.runInContext(`
  const app = unifiedInboxManager();
  app.$nextTick = (fn) => fn();
  const href = (link, msg) => new Function('msg',
    'with (this) { return (' + link.attrs[':href'] + '); }'
  ).call(app, msg);
`, context, {timeout: 5000});
Promise.resolve(vm.runInContext('(async () => {\n' + data.check + '\n})()', context, {timeout: 5000}))
  .then(() => process.stdout.write('ok\n'))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""


def _check(node: str, page: _Page, check: str, **payload: Any) -> None:
    assert len(page.scripts) == 1
    result = subprocess.run(
        [node, "-e", NODE_RUNNER],
        input=json.dumps({
            "script": page.scripts[0], "links": page.links,
            "elements": page.elements, "check": check, "payload": payload,
        }),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


@pytest.mark.parametrize("thread_id", [None, "", "   ", "0", "00100", "release ?#&", 'literal%2F"', " padded ", "\t", "\u00a0", "計画"])
@pytest.mark.parametrize("project", ["alpha", "beta"])
def test_rendered_thread_links_match_sqlite_identity_and_message_project(node: str, thread_id: str | None, project: str) -> None:
    message = _message(thread_id=thread_id, project=project)
    with sqlite3.connect(":memory:") as connection:
        key = connection.execute(
            "SELECT CASE WHEN ? IS NOT NULL AND TRIM(?) != '' THEN ? ELSE CAST(? AS TEXT) END",
            (thread_id, thread_id, thread_id, message["id"]),
        ).fetchone()[0]
    page = _render([message])
    encoded_key = quote(key, safe="~()*!.'")
    _check(node, page, r"""
        const msg = app.allMessages[0];
        app.selectedMessage = msg;
        const threadLinks = links.filter(link => /^threadUrl\(/.test(link.attrs[':href'] || ''));
        assert.equal(threadLinks.length, 3, 'list, metadata and selected-message action must all link to the conversation');
        for (const link of threadLinks) {
            assert.equal(href(link, msg), payload.expected);
            assert.ok(!link.ancestors.some(([, attrs]) => /thread_id/.test(attrs['x-if'] || '')),
              'standalone starters must not lose the thread link');
        }
        assert.equal(app.conversationId(msg), payload.key);
        const browse = links.find(link => link.attrs[':href'] === 'threadsUrl(selectedMessage)');
        assert.ok(browse, 'the selected message must expose its project thread browser');
        assert.equal(href(browse, msg), '/mail/' + msg.project_slug + '/threads');
    """, key=key, expected=f"/mail/{project}/thread/{encoded_key}")


def test_navigation_controls_do_not_hijack_message_selection(node: str) -> None:
    page = _render([_message()])
    rows = [e for e in page.elements if ":data-message-id" in e["attrs"]]
    assert len(rows) == 1
    assert rows[0]["attrs"]["role"] == "group"
    assert not any(key.startswith("@keydown") for key in rows[0]["attrs"])
    for expression in ("threadUrl(msg)", "projectUrl(msg)"):
        link = next(e for e in page.links if e["attrs"].get(":href") == expression)
        assert "@click.stop" in link["attrs"]
        assert "@click.prevent" not in link["attrs"]  # Preserve native/new-tab navigation.
    buttons = [e for e in page.elements if e["tag"] == "button" and e["attrs"].get("@click.stop") == "handleMessageClick(msg)"]
    assert len(buttons) == 1
    assert buttons[0]["attrs"]["type"] == "button"
    _check(node, page, """
        const msg = app.allMessages[0];
        const button = elements.find(e => e.tag === 'button' && e.attrs['@click.stop'] === 'handleMessageClick(msg)');
        const activate = () => new Function('msg', 'with (this) {' + button.attrs['@click.stop'] + '}').call(app, msg);
        app.selectedMessages = [msg.id];
        activate();
        assert.equal(app.selectedMessage, msg);
        assert.equal(window.location.href, '/mail');
        app.threadUrl(msg);
        assert.equal(app.selectedMessages[0], msg.id);
        app.viewMode = 'list';
        activate();
        assert.equal(window.location.href, '/mail/alpha/message/100');
    """)


def test_thread_identifier_search_composes_with_existing_filters_and_sort(node: str) -> None:
    messages = [_message(100, "FEAT-277"), _message(101, "FEAT-277", "beta"), _message(102, ""), _message(103, "   ")]
    page = _render(messages)
    _check(node, page, """
        const ids = () => JSON.stringify(app.filteredMessages.map(m => m.id));
        app.searchQuery = 'feat-277';
        app.filterMessages();
        assert.equal(ids(), '[101,100]');
        app.filters.project = 'Alpha';
        app.filters.sender = 'Remote Agent';
        app.filters.recipient = 'BlueLake';
        app.filters.importance = 'normal';
        app.filters.hasThread = 'true';
        app.filterMessages();
        assert.equal(ids(), '[100]');
        app.searchQuery = 'status update';
        app.filters.hasThread = 'false';
        app.sortBy = 'oldest';
        app.filterMessages();
        assert.equal(ids(), '[102,103]');
        app.searchQuery = '102';
        app.filterMessages();
        assert.equal(ids(), '[102]');
        app.searchQuery = '';
        app.clearFilters();
        assert.equal(app.filteredMessages.length, 4);
    """)


def test_thread_destinations_follow_refreshed_message_without_losing_selection(node: str) -> None:
    page = _render([_message(100, "old"), _message(101, "shared", "beta")])
    _check(node, page, """
        app.autoRefreshEnabled = false;
        app.selectedMessage = app.allMessages[0];
        app.selectedMessages = [100];
        app.filters.project = 'Alpha';
        globalThis.fetch = async (url) => {
            assert.equal(url, '/mail/api/unified-inbox?limit=500&include_projects=false');
            return {ok: true, json: async () => ({messages: payload.incoming})};
        };
        await app.fetchLatestMessages();
        assert.equal(app.selectedMessage.id, 100);
        assert.equal(JSON.stringify(app.selectedMessages), '[100]');
        assert.equal(app.filteredMessages.length, 1);
        const link = links.find(link => link.attrs[':href'] === 'threadUrl(selectedMessage)');
        assert.equal(href(link, app.selectedMessage), '/mail/alpha/thread/100');
        assert.equal(app.threadUrl(app.allMessages[1]), '/mail/beta/thread/100');
        assert.equal(app.refreshError, null);
        globalThis.fetch = async () => ({ok: false, status: 503});
        await app.fetchLatestMessages();
        assert.equal(app.selectedMessage.id, 100);
        assert.equal(href(link, app.selectedMessage), '/mail/alpha/thread/100');
        assert.equal(app.isRefreshing, false);
        assert.ok(app.refreshError);
    """, incoming=[_message(100, None), _message(101, "100", "beta")])


def test_message_links_copy_and_project_links_share_encoded_message_scope(node: str) -> None:
    page = _render([_message(project="workspace ?#&")])
    _check(node, page, r"""
        const msg = app.allMessages[0];
        app.selectedMessage = msg;
        const projectLinks = links.filter(link => /^projectUrl\(/.test(link.attrs[':href'] || ''));
        assert.equal(projectLinks.length, 2);
        for (const link of projectLinks) assert.equal(href(link, msg), '/mail/workspace%20%3F%23%26');
        const open = links.find(link => link.attrs[':href'] === 'messageUrl(selectedMessage)');
        assert.equal(href(open, msg), '/mail/workspace%20%3F%23%26/message/100');
        window.copyToClipboard = (value) => { globalThis.copied = value; };
        app.copyMessageLink(msg);
        assert.equal(copied, 'http://test' + href(open, msg));
        assert.equal(app.projectUrl({sender_project_slug: 'beta'}), null);
        for (const value of [null, undefined, {}]) {
            assert.equal(app.threadUrl(value), null);
            assert.equal(app.threadsUrl(value), null);
            assert.equal(app.messageUrl(value), null);
        }
    """)


def test_empty_inbox_renders_and_does_not_invent_conversation_links(node: str) -> None:
    page = _render([])
    _check(node, page, """
        app.filterMessages();
        assert.equal(app.filteredMessages.length, 0);
        assert.equal(app.selectedMessage, null);
        assert.equal(app.threadUrl(app.selectedMessage), null);
        assert.equal(app.threadsUrl(app.selectedMessage), null);
    """)


def test_untrusted_message_data_remains_data_in_production_script(node: str) -> None:
    message = _message(thread_id='literal"?#&')
    message["subject"] = '</script><script>throw new Error("injected")</script>'
    page = _render([message])
    _check(node, page, """
        assert.equal(app.allMessages[0].subject, payload.subject);
        assert.equal(app.threadUrl(app.allMessages[0]), '/mail/alpha/thread/literal%22%3F%23%26');
        assert.ok(!elements.some(e => e.attrs.onerror || e.attrs.onload));
    """, subject=message["subject"])
