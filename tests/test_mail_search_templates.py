"""Render the real search content blocks: conversation links and untrusted FTS text.

These tests isolate the template boundary, not HTTP routing or the full service.
No substitute base template is used: Jinja renders each production content block
with the same async/autoescape settings as the mail viewer.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup


TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "mcp_agent_mail" / "templates"
SEARCH_TEMPLATES = ("mail_project.html", "mail_search.html")


class _HTML(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.links: list[dict[str, str]] = []
        self.text: list[str] = []
        self._link: dict[str, str] | None = None
        self.feed(html)
        self.close()
        assert self._link is None, "Unclosed anchor in rendered search results"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "a":
            assert self._link is None, "Nested anchors in rendered search results"
            self._link = {"href": attributes.get("href") or "", "text": ""}
            self.links.append(self._link)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._link = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._link is not None:
            self._link["text"] += data


@pytest.fixture
def template_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=True,
    )


def _result(thread_id: str | None = "release", snippet: str = "Preview") -> dict[str, Any]:
    return {
        "id": 42,
        "thread_id": thread_id,
        "subject": "Searchable subject",
        "from": "Remote sender",
        "sender": "Remote sender",
        # The sender's workspace must never determine the navigation target.
        "sender_project_slug": "other-project",
        "created": "2026-01-01 00:00:00",
        "snippet": snippet,
        "hits": 1,
    }


def _render(
    env: Environment,
    name: str,
    *,
    block: str = "content",
    project_slug: str = "alpha",
    **overrides: Any,
) -> str:
    template = env.get_template(name)
    values: dict[str, Any] = {
        "project": {"id": 1, "slug": project_slug, "human_key": "Workspace Alpha", "archived_at": None},
        "agents": [],
        "active_threads": [],
        "results": [_result()],
        "q": "Searchable",
        "scope": "subject",
        "order": "time",
        "boost": True,
        "tokens": [],
    }
    values.update(overrides)

    async def render_block() -> str:
        # Jinja's block annotation describes sync generators; enable_async=True
        # makes these async generators at runtime.
        parts = cast(AsyncIterator[str], template.blocks[block](template.new_context(values)))
        return "".join([part async for part in parts])

    return asyncio.run(render_block())


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
@pytest.mark.parametrize("project_slug", ["alpha", "beta"])
@pytest.mark.parametrize("thread_id", [None, "", "   ", "release", "123", "release ?#&", 'literal%2F"', "  padded  "])
def test_search_results_offer_independent_project_scoped_thread_links(
    template_env: Environment, name: str, project_slug: str, thread_id: str | None,
) -> None:
    page = _HTML(_render(template_env, name, project_slug=project_slug, results=[_result(thread_id)]))
    conversation_id = thread_id if thread_id and thread_id.strip(" ") else "42"
    prefix = f"/mail/{project_slug}"
    expected = f"{prefix}/thread/{quote(conversation_id, safe='')}"
    thread_links = [link for link in page.links if "/thread/" in link["href"]]
    assert [link["href"] for link in thread_links] == [expected]
    assert conversation_id in thread_links[0]["text"]
    message_links = [link for link in page.links if link["href"] == f"{prefix}/message/42"]
    assert len(message_links) == 1
    assert "Searchable subject" in message_links[0]["text"]
    assert any(tag == "article" for tag, _ in page.elements)
    assert any("after:inset-0" in (attrs.get("class") or "") for tag, attrs in page.elements if tag == "a")


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
def test_subjectless_results_still_have_an_accessible_message_link(template_env: Environment, name: str) -> None:
    result = _result()
    result["subject"] = ""
    page = _HTML(_render(template_env, name, results=[result]))
    message = next(link for link in page.links if link["href"] == "/mail/alpha/message/42")
    assert "(No subject)" in message["text"]


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
@pytest.mark.parametrize("project_slug", ["alpha", "beta"])
def test_empty_search_keeps_project_thread_browsing_available(
    template_env: Environment, name: str, project_slug: str,
) -> None:
    page = _HTML(_render(template_env, name, block="header_actions", project_slug=project_slug, results=[]))
    browse = next(link for link in page.links if link["href"] == f"/mail/{project_slug}/threads")
    assert "Threads" in browse["text"]


@pytest.mark.parametrize("query", [None, "", "   "])
def test_workspace_does_not_claim_a_failed_search_before_a_query(
    template_env: Environment, query: str | None,
) -> None:
    html = _render(template_env, "mail_project.html", results=[], q=query)
    assert "No conversations yet" in html
    assert "No matching messages" not in html


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
def test_real_no_match_search_and_existing_form_controls_are_preserved(template_env: Environment, name: str) -> None:
    html = _render(template_env, name, results=[], q="missing")
    assert ("No matching messages" if name == "mail_project.html" else "No results found") in html
    controls = {attrs.get("name"): attrs for tag, attrs in _HTML(html).elements if tag == "input"}
    assert controls["q"]["value"] == "missing"
    assert controls["scope"]["x-model"] == "scope"
    assert controls["order"]["x-model"] == "order"
    assert "checked" in controls["boost"]


PAYLOADS = (
    '<img src="missing" onerror="window.snippetExecuted=true">',
    '<script>window.snippetExecuted=true</script>',
    '<svg onload="window.snippetExecuted=true"></svg>',
    '<a href="https://example.invalid/">Untrusted link</a>',
    '</div><iframe srcdoc="untrusted"></iframe><div>',
    '<mark onclick="window.snippetExecuted=true">Untrusted highlight</mark>',
    '<!-- unclosed comment',
    '&lt;img src=x onerror=untrusted&gt;',
)


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("already_marked_safe", [False, True])
def test_search_snippets_cannot_create_html_or_change_navigation(
    template_env: Environment, name: str, payload: str, already_marked_safe: bool,
) -> None:
    snippet = f"Before {payload} <mark>needle</mark> after"
    if already_marked_safe:
        snippet = Markup(snippet)
    page = _HTML(_render(template_env, name, results=[_result(snippet=snippet)]))
    control = _HTML(_render(template_env, name, results=[_result(snippet="Before plain after")]))
    # Compare the complete content block, not a source substring. Escaping must
    # preserve every existing control/link and may add only attribute-free marks.
    assert [(tag, attrs) for tag, attrs in page.elements if tag != "mark"] == control.elements
    assert [(tag, attrs) for tag, attrs in page.elements if tag == "mark"] == [("mark", {})]
    assert page.links == control.links
    assert payload in "".join(page.text)


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
@pytest.mark.parametrize("max_tokens", [18, 22])
def test_actual_sqlite_fts_snippets_keep_highlights_but_not_message_html(
    template_env: Environment, name: str, max_tokens: int,
) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE fts_messages USING fts5(id, subject, body_md)")
        connection.execute(
            "INSERT INTO fts_messages VALUES (?, ?, ?)",
            (1, "Subject", '<img src="missing" onerror="untrusted"> needle and another needle'),
        )
        row = connection.execute(
            "SELECT snippet(fts_messages, 2, '<mark>', '</mark>', '…', ?) "
            "FROM fts_messages WHERE fts_messages MATCH ?",
            (max_tokens, "needle"),
        ).fetchone()
        assert row is not None
        snippet = row[0]
    finally:
        connection.close()
    assert "<img" in snippet, "Fixture must exercise raw HTML from SQLite, not pre-escaped text"
    assert snippet.count("<mark>") == 2
    page = _HTML(_render(template_env, name, results=[_result(snippet=snippet)]))
    assert not any(tag == "img" for tag, _ in page.elements)
    assert [attrs for tag, attrs in page.elements if tag == "mark"] == [{}, {}]
    assert '<img src="missing" onerror="untrusted">' in "".join(page.text)


@pytest.mark.parametrize("name", SEARCH_TEMPLATES)
@pytest.mark.parametrize("snippet,expected_marks", [
    ("Plain <text> & content", 0),
    ("Unmatched <mark>needle", 0),
    ("Unmatched needle</mark>", 0),
    ("<mark>one</mark> + <mark>two</mark>", 2),
    ("<mark><img src=x></mark>", 1),
])
def test_highlight_edges_preserve_text_without_injecting_elements(
    template_env: Environment, name: str, snippet: str, expected_marks: int,
) -> None:
    page = _HTML(_render(template_env, name, results=[_result(snippet=snippet)]))
    control = _HTML(_render(template_env, name, results=[_result(snippet="Plain preview")]))
    assert [(tag, attrs) for tag, attrs in page.elements if tag != "mark"] == control.elements
    assert [attrs for tag, attrs in page.elements if tag == "mark"] == [{}] * expected_marks
