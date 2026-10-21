"""Conversation reply links and the Human Overseer composer.

Template/component tests render production child templates with a minimal layout
shell and run the production JavaScript in Node. Fetch responses are fixtures;
these tests do not establish delivery. The separate full-service test exercises
the real HTTP application, database, and archive in isolated_env.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "mcp_agent_mail" / "templates"
THREAD_IDS = [None, "", "   ", "release", "0042", "  exact  ", "release ?#&", 'literal%2F"', "計画", "\t", "\u00a0"]


class _Page(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.scripts: list[str] = []
        self._link: dict[str, str] | None = None
        self._script: list[str] | None = None
        self.feed(html)
        self.close()
        assert self._link is None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            assert self._link is None, "Nested anchors"
            self._link = {"href": dict(attrs).get("href") or "", "text": ""}
            self.links.append(self._link)
        elif tag == "script":
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link["text"] += data
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._link = None
        elif tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


@pytest.fixture
def templates() -> Environment:
    return Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block header_actions %}{% endblock %}{% block content %}{% endblock %}"}),
            FileSystemLoader(str(TEMPLATES)),
        ]),
        autoescape=select_autoescape(["html", "xml"]),
        enable_async=True,
    )


def _context(thread_id: str | None, project: str = "alpha", subject: str = "Release planning") -> dict[str, Any]:
    key = thread_id if thread_id and thread_id.strip(" ") else "100"
    return {
        "project": {"slug": project, "human_key": f"Workspace {project}"},
        "message": {
            "id": 100, "thread_id": thread_id, "subject": subject, "importance": "normal",
            "sender": "beta:RemoteAgent", "sender_project_slug": "beta", "created": "2026-01-01",
            "body_html": "<p>Original message</p>", "attachments": [],
        },
        "recipients": [], "commit_sha": None, "thread_items": [], "thread_message_count": 1,
        "thread_id": key, "thread_subject": subject, "messages": [], "message_count": 1,
        "page": 1, "page_count": 1, "limit": 50, "prev_page": None, "next_page": None,
        "first_message": 1, "last_message": 1,
        "agents": [{"name": "BlueLake"}, {"name": "RedStone"}],
    }


def _render(templates: Environment, name: str, context: dict[str, Any]) -> _Page:
    return _Page(asyncio.run(templates.get_template(name).render_async(**context)))


def _component(templates: Environment, context: dict[str, Any]) -> str:
    page = _render(templates, "overseer_compose.html", context)
    return next(script for script in page.scripts if "function overseerComposer()" in script)


def _run(script: str, query: str, actions: str) -> Any:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for composer JavaScript tests")
    # Test-only fetch/window/timer adapters. No requests leave this process.
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
global.window = {location: {search: input.query, href: 'initial'}};
const requests = [], timers = [];
global.setTimeout = (fn, delay) => { timers.push({fn, delay}); return timers.length; };
global.fetch = async (url, options) => {
  requests.push({url, ...options, payload: JSON.parse(options.body)});
  return {ok: true, json: async () => ({success: true, message_id: 101, recipients: ['BlueLake']})};
};
const composer = new Function(input.script + '; return overseerComposer();')();
composer.$nextTick = fn => fn();
(async () => {
  const run = new Function('composer', 'requests', 'timers', 'assert',
      'return (async () => {' + input.actions + '})();');
  await run(composer, requests, timers, assert);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(
        [node, "-e", harness], input=json.dumps({"script": script, "query": query, "actions": actions}),
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


@pytest.mark.parametrize("thread_id", THREAD_IDS)
@pytest.mark.parametrize("template", ["mail_message.html", "mail_thread.html"])
def test_reply_links_preserve_project_and_exact_conversation(
    templates: Environment, thread_id: str | None, template: str,
) -> None:
    subject = '</script><img src=x onerror="alert(1)"> & Release'
    context = _context(thread_id, subject=subject)
    page = _render(templates, template, context)
    links = [link for link in page.links if "Reply as Human Overseer" in link["text"]]
    assert len(links) == 1
    url = urlsplit(links[0]["href"])
    assert url.path == "/mail/alpha/overseer/compose"
    assert not url.fragment
    params = parse_qs(url.query)
    assert params == {"reply_thread": [context["thread_id"]], "reply_subject": [subject]}
    result = _run(_component(templates, context), "?" + url.query, """
composer.init();
assert.equal(requests.length, 0);
assert.deepEqual(composer.selectedRecipients, []);
assert.equal(composer.body, '');
console.log(JSON.stringify({thread: composer.conversationId(), cancel: composer.conversationUrl(composer.replyThreadId), subject: composer.subject}));
""")
    assert result["thread"] == context["thread_id"]
    assert result["cancel"] == f"/mail/alpha/thread/{quote(context['thread_id'], safe='')}"
    assert result["subject"] == "Re: " + subject


@pytest.mark.parametrize("thread_id", ["100", "0042", "  exact  ", "release ?#&", 'literal%2F"', "\t", "\u00a0"])
def test_confirmed_reply_sends_exact_id_once_and_returns_to_conversation(templates: Environment, thread_id: str) -> None:
    query = "?" + urlencode({"reply_thread": thread_id, "reply_subject": "Plan", "project": "beta", "return_to": "https://invalid.example"})
    result = _run(_component(templates, _context(thread_id)), query, """
composer.init();
composer.selectedRecipients = ['BlueLake']; composer.body = ' Please proceed. ';
await composer.confirmSend();
assert.equal(requests.length, 0, 'confirmation cannot be bypassed');
await composer.sendMessage();
assert.equal(requests.length, 0, 'opening the review must not send');
assert.equal(composer.showConfirmModal, true);
await Promise.all([composer.confirmSend(), composer.confirmSend()]);
assert.equal(requests.length, 1);
assert.equal(composer.sending, true);
assert.equal(requests[0].method, 'POST');
composer.threadId = 'different'; composer.replyThreadId = 'different';
composer.selectedRecipients.push('RedStone');
timers.find(timer => timer.delay === 2000).fn();
console.log(JSON.stringify({request: requests[0], destination: window.location.href}));
""")
    assert result["request"]["url"] == "/mail/alpha/overseer/send"
    assert result["request"]["payload"] == {
        "recipients": ["BlueLake"], "subject": "Re: Plan", "body_md": "Please proceed.", "thread_id": thread_id,
    }
    assert result["destination"] == f"/mail/alpha/thread/{quote(thread_id, safe='')}"


@pytest.mark.parametrize("query", ["", "?reply_thread=", "?reply_thread=+++", "?reply_subject=Ignored&body=Ignore&recipients=BlueLake"])
def test_new_message_composer_stays_empty_and_returns_to_project(templates: Environment, query: str) -> None:
    _run(_component(templates, _context(None)), query, """
composer.init();
assert.equal(composer.replyThreadId, null); assert.equal(composer.subject, '');
assert.equal(composer.body, ''); assert.equal(composer.conversationId(), null);
assert.deepEqual(composer.selectedRecipients, []);
composer.subject = 'New conversation'; composer.body = 'Hello'; composer.selectedRecipients = ['BlueLake'];
await composer.sendMessage(); await composer.confirmSend();
assert.equal(requests[0].payload.thread_id, null);
timers.find(timer => timer.delay === 2000).fn();
assert.equal(window.location.href, '/mail/alpha');
""")


@pytest.mark.parametrize("failure", ["http", "network", "unconfirmed", "bad-json"])
def test_failed_send_preserves_draft_and_requires_confirmation_again(templates: Environment, failure: str) -> None:
    _run(_component(templates, _context("release")), "?reply_thread=release", f"const failure = {json.dumps(failure)};" + """
composer.init(); composer.body = 'Retain this draft'; composer.selectedRecipients = ['BlueLake'];
const success = global.fetch;
global.fetch = async (url, options) => {
  requests.push({url, payload: JSON.parse(options.body)});
  if (failure === 'network') throw new Error('Offline');
  if (failure === 'http') return {ok: false, status: 503, json: async () => ({detail: 'Unavailable'})};
  if (failure === 'bad-json') return {ok: true, json: async () => { throw new Error('Invalid JSON'); }};
  return {ok: true, json: async () => ({success: false})};
};
await composer.sendMessage(); await composer.confirmSend();
assert.equal(composer.sending, false); assert.equal(composer.toastType, 'error');
assert.equal(composer.body, 'Retain this draft'); assert.deepEqual(composer.selectedRecipients, ['BlueLake']);
assert.equal(timers.filter(timer => timer.delay === 2000).length, 0);
await composer.confirmSend(); assert.equal(requests.length, 1);
global.fetch = success;
await composer.sendMessage(); await composer.confirmSend();
assert.equal(requests.length, 2); assert.equal(composer.toastType, 'success');
""")


@pytest.mark.parametrize("invalid", ["recipients", "subject", "body", "too-many"])
def test_confirmation_revalidates_the_draft(templates: Environment, invalid: str) -> None:
    _run(_component(templates, _context("release")), "?reply_thread=release", f"const invalid = {json.dumps(invalid)};" + """
composer.init(); composer.subject = 'Plan'; composer.body = 'Proceed'; composer.selectedRecipients = ['BlueLake'];
await composer.sendMessage();
if (invalid === 'recipients') composer.selectedRecipients = [];
if (invalid === 'subject') composer.subject = ' ';
if (invalid === 'body') composer.body = ' ';
if (invalid === 'too-many') composer.selectedRecipients = Array(101).fill('BlueLake');
await composer.confirmSend();
assert.equal(requests.length, 0); assert.equal(composer.sending, false);
""")


def test_reply_prefill_is_idempotent_and_does_not_trust_other_query_fields(templates: Environment) -> None:
    _run(_component(templates, _context("release")), "?reply_thread=release&reply_subject=Re%3A+Plan&body=Injected&recipients=BlueLake", """
composer.init(); assert.equal(composer.subject, 'Re: Plan');
assert.equal(composer.body, ''); assert.deepEqual(composer.selectedRecipients, []);
composer.subject = 'Edited subject'; composer.body = 'Edited draft'; composer.init();
assert.equal(composer.subject, 'Edited subject'); assert.equal(composer.body, 'Edited draft');
composer.threadId = 'accidental edit'; assert.equal(composer.conversationId(), 'release');
assert.equal(requests.length, 0);
""")


def test_long_unicode_reply_subject_is_bounded_without_splitting_characters(templates: Environment) -> None:
    result = _run(_component(templates, _context("release")), "?" + urlencode({"reply_thread": "release", "reply_subject": "😀" * 200}), """
composer.init(); assert.ok(composer.subject.length <= 200);
console.log(JSON.stringify(composer.subject));
""")
    assert result == "Re: " + "😀" * 98


@pytest.mark.parametrize("mode", ["both-missing", "sanitizer-missing", "parser-missing", "parser-throws", "sanitizer-throws"])
def test_preview_fails_closed_when_dependencies_are_unavailable(templates: Environment, mode: str) -> None:
    result = _run(_component(templates, _context("release")), "", f"const mode = {json.dumps(mode)};" + """
if (mode !== 'parser-missing' && mode !== 'both-missing')
  global.marked = {parse: body => {if (mode === 'parser-throws') throw new Error('parser'); return body;}};
if (mode !== 'sanitizer-missing' && mode !== 'both-missing')
  global.DOMPurify = {sanitize: html => {if (mode === 'sanitizer-throws') throw new Error('sanitizer'); return 'SANITIZED';}};
composer.body = '<img src=x onerror="alert(1)"><script>alert(2)</script>&';
composer.updatePreview();
console.log(JSON.stringify(composer.renderedPreview));
""")
    assert "<img" not in result and "<script" not in result
    assert "&lt;img" in result and "&amp;" in result


def test_preview_uses_sanitizer_when_both_dependencies_are_available(templates: Environment) -> None:
    _run(_component(templates, _context("release")), "", """
const calls = [];
global.marked = {parse: body => {calls.push(body); return '<strong>parsed</strong>';}};
global.DOMPurify = {sanitize: html => {calls.push(html); return '<strong>safe</strong>';}};
composer.body = '**draft**'; composer.updatePreview();
assert.deepEqual(calls, ['**draft**', '<strong>parsed</strong>']);
assert.equal(composer.renderedPreview, '<strong>safe</strong>');
""")


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "release", "0042"])
async def test_full_service_reply_round_trip(isolated_env, thread_id: str | None) -> None:
    """Prove delivery with the real app, SQLite, and Git archive, not the fetch fixture."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from mcp_agent_mail.config import get_settings
    from mcp_agent_mail.db import ensure_schema, get_session
    from mcp_agent_mail.http import build_http_app

    await ensure_schema()
    key = thread_id or "100"
    async with get_session() as session:
        await session.execute(text(
            "INSERT INTO projects (id, slug, human_key, created_at) VALUES "
            "(1, 'alpha', 'Workspace Alpha', '2026-01-01'), (2, 'beta', 'Workspace Beta', '2026-01-01')"
        ))
        await session.execute(text(
            "INSERT INTO agents (id, project_id, name, program, model, task_description, "
            "inception_ts, last_active_ts, attachments_policy, contact_policy) "
            "VALUES (:id, :pid, :name, 'test', 'test', 'Reply regression', "
            "'2026-01-01', '2026-01-01', 'auto', 'open')"
        ), [{"id": 1, "pid": 1, "name": "BlueLake"}, {"id": 2, "pid": 2, "name": "GreenCastle"}])
        await session.execute(text(
            "INSERT INTO messages (id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts) "
            "VALUES (:id, :pid, 2, :tid, 'Original subject', :body, 'normal', 1, '2026-01-01')"
        ), [
            {"id": 100, "pid": 1, "tid": thread_id, "body": "Alpha opener"},
            {"id": 200, "pid": 2, "tid": key, "body": "Beta private body"},
        ])
        await session.execute(text(
            "INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (100, 1, 'to')"
        ))
        await session.commit()

    app = build_http_app(get_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        message = await client.get("/mail/Workspace%20Alpha/message/100")
        assert message.status_code == 200
        link = next(link for link in _Page(message.text).links if "Reply as Human Overseer" in link["text"])
        compose = await client.get(link["href"])
        assert compose.status_code == 200
        script = next(script for script in _Page(compose.text).scripts if "function overseerComposer()" in script)
        request = _run(script, "?" + urlsplit(link["href"]).query, """
composer.init();
assert.deepEqual(composer.agents, ['BlueLake']);
assert.deepEqual(composer.selectedRecipients, []);
composer.selectedRecipients = ['BlueLake']; composer.body = 'Reply integration probe';
await composer.sendMessage(); await composer.confirmSend();
console.log(JSON.stringify(requests[0]));
""")
        sent = await client.post(request["url"], json=request["payload"])
        assert sent.status_code == 200, sent.text
        result = sent.json()
        assert result["success"] is True
        assert result["recipients"] == ["BlueLake"]
        conversation = await client.get(f"/mail/alpha/thread/{quote(key, safe='')}")
        assert conversation.status_code == 200
        assert "Alpha opener" in conversation.text
        assert "Reply integration probe" in conversation.text
        assert "Beta private body" not in conversation.text
        beta = await client.get(f"/mail/beta/thread/{quote(key, safe='')}")
        assert "Reply integration probe" not in beta.text
        async with get_session() as session:
            row = (await session.execute(text(
                "SELECT project_id, thread_id FROM messages WHERE id = :id"
            ), {"id": result["message_id"]})).one()
            assert tuple(row) == (1, key)
            original_state = (await session.execute(text(
                "SELECT read_ts, ack_ts FROM message_recipients WHERE message_id = 100"
            ))).one()
            assert tuple(original_state) == (None, None)
