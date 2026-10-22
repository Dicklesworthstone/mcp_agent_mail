"""Regression coverage for project-scoped conversation discovery (issue #277)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app


class _Links(HTMLParser):
    """Collect real links and reject invalid nested anchors in rendered cards."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            assert self._current is None, "Rendered page contains nested anchors"
            self._current = {"href": dict(attrs).get("href") or "", "text": ""}
            self.links.append(self._current)

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current = None


def _thread_links(response: Response, project: str = "alpha") -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    prefix = f"/mail/{project}/thread/"
    return [link for link in _Links(response.text).links if link["href"].startswith(prefix)]


def _message(
    mid: int,
    thread_id: str | None,
    subject: str,
    seconds: int,
    *,
    project_id: int = 1,
    sender_id: int = 1,
) -> dict[str, Any]:
    return {
        "id": mid,
        "pid": project_id,
        "sid": sender_id,
        "tid": thread_id,
        "subject": subject,
        "body": f"Body for message {mid}.",
        "created": datetime(2026, 1, 1) + timedelta(seconds=seconds),
    }


async def _seed_messages(*messages: dict[str, Any]) -> None:
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO messages "
                "(id, project_id, sender_id, thread_id, subject, body_md, importance, ack_required, created_ts) "
                "VALUES (:id, :pid, :sid, :tid, :subject, :body, 'normal', 1, :created)"
            ),
            list(messages),
        )
        await session.commit()


@pytest.fixture
async def mail_client(isolated_env) -> AsyncIterator[AsyncClient]:
    await ensure_schema()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO projects (id, slug, human_key, created_at) "
                "VALUES (:id, :slug, :key, '2026-01-01 00:00:00')"
            ),
            [
                {"id": 1, "slug": "alpha", "key": "Workspace Alpha"},
                {"id": 2, "slug": "beta", "key": "Workspace Beta"},
                {"id": 3, "slug": "empty", "key": "Empty Workspace"},
            ],
        )
        await session.execute(
            text(
                "INSERT INTO agents (id, project_id, name, program, model, task_description, "
                "inception_ts, last_active_ts, attachments_policy, contact_policy) "
                "VALUES (:id, :pid, :name, 'test', 'test', 'Navigation regression tests', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00', 'auto', 'auto')"
            ),
            [
                {"id": 1, "pid": 1, "name": "BlueLake"},
                {"id": 2, "pid": 2, "name": "GreenCastle"},
                {"id": 3, "pid": 1, "name": "RedStone"},
            ],
        )
        await session.commit()
    app = build_http_app(get_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_project_exposes_thread_browsing_and_direct_navigation(mail_client: AsyncClient) -> None:
    await _seed_messages(_message(1, "release", "Release planning", 1))
    project = await mail_client.get("/mail/alpha")
    assert project.status_code == 200
    links = _Links(project.text).links
    action = next(link for link in links if link["href"] == "/mail/alpha/threads")
    assert "Threads" in action["text"]

    listing = await mail_client.get(action["href"])
    thread = _thread_links(listing)[0]
    assert thread["href"] == "/mail/alpha/thread/release"
    assert "Release planning" in thread["text"]
    assert "1 message" in " ".join(thread["text"].split())
    assert "Latest activity" in thread["text"]
    detail = await mail_client.get(thread["href"])
    assert detail.status_code == 200
    assert "Body for message 1." in detail.text
    assert any(link["href"] == "/mail/alpha" for link in _Links(detail.text).links)


@pytest.mark.asyncio
async def test_threads_and_activity_are_isolated_by_message_project(mail_client: AsyncClient) -> None:
    await _seed_messages(
        _message(1, "shared", "Alpha opening", 1),
        _message(2, "shared", "Alpha update", 2),
        _message(3, "shared", "Beta confidential", 90, project_id=2, sender_id=2),
        _message(4, "cross-project", "Delivered to Alpha", 3, sender_id=2),
        _message(5, "beta-only", "Beta only", 91, project_id=2, sender_id=2),
    )
    alpha = await mail_client.get("/mail/alpha/threads")
    threads = _thread_links(alpha)
    assert [link["href"] for link in threads] == [
        "/mail/alpha/thread/cross-project", "/mail/alpha/thread/shared"
    ]
    assert "Alpha update" in threads[1]["text"]
    assert "2 messages" in " ".join(threads[1]["text"].split())
    assert "Beta confidential" not in alpha.text
    assert "Beta only" not in alpha.text
    assert "/mail/beta/thread/" not in alpha.text

    beta = await mail_client.get("/mail/beta/threads")
    assert len(_thread_links(beta, "beta")) == 2
    assert "Alpha update" not in beta.text
    assert "Delivered to Alpha" not in beta.text
    detail = await mail_client.get("/mail/alpha/thread/shared")
    assert "Body for message 1." in detail.text
    assert "Body for message 2." in detail.text
    assert "Body for message 3." not in detail.text


@pytest.mark.asyncio
async def test_numeric_starters_merge_with_replies_and_standalone_messages_remain_visible(
    mail_client: AsyncClient,
) -> None:
    await _seed_messages(
        _message(100, None, "Original", 1),
        _message(101, "100", "First reply", 2),
        _message(102, "100", "Second reply", 3),
        _message(103, None, "Standalone", 4),
        _message(104, "", "Empty thread ID", 5),
        _message(105, "   ", "Blank thread ID", 6),
    )
    threads = _thread_links(await mail_client.get("/mail/alpha/threads"))
    assert [link["href"] for link in threads] == [
        f"/mail/alpha/thread/{tid}" for tid in (105, 104, 103, 100)
    ]
    assert "3 messages" in " ".join(threads[-1]["text"].split())
    detail = await mail_client.get(threads[-1]["href"])
    assert detail.status_code == 200
    for mid in (100, 101, 102):
        assert f"Body for message {mid}." in detail.text
    assert "Body for message 103." not in detail.text
    for thread in threads[:-1]:
        assert (await mail_client.get(thread["href"])).status_code == 200


@pytest.mark.asyncio
async def test_pagination_groups_before_limiting_and_has_stable_activity_order(mail_client: AsyncClient) -> None:
    await _seed_messages(
        _message(10, "busy", "Busy start", 10),
        _message(11, "busy", "Busy reply", 11),
        _message(12, "busy", "Busy latest", 12),
        _message(20, "older-a", "Older A", 1),
        _message(21, "older-b", "Older B", 1),
    )
    first = await mail_client.get("/mail/alpha/threads?limit=1")
    threads = _thread_links(first)
    assert len(threads) == 1
    assert threads[0]["href"] == "/mail/alpha/thread/busy"
    assert "3 messages" in " ".join(threads[0]["text"].split())
    assert 'rel="prev"' not in first.text
    assert any(link["href"] == "/mail/alpha/threads?page=2&limit=1" for link in _Links(first.text).links)

    second = await mail_client.get("/mail/alpha/threads?page=2&limit=1")
    assert _thread_links(second)[0]["href"] == "/mail/alpha/thread/older-b"
    assert any(link["href"] == "/mail/alpha/threads?page=1&limit=1" for link in _Links(second.text).links)
    last = await mail_client.get("/mail/alpha/threads?page=3&limit=1")
    assert _thread_links(last)[0]["href"] == "/mail/alpha/thread/older-a"
    assert 'rel="next"' not in last.text
    beyond = await mail_client.get("/mail/alpha/threads?page=4&limit=1")
    assert _thread_links(beyond) == []
    assert "No more threads" in beyond.text
    assert 'rel="prev"' in beyond.text
    assert 'rel="next"' not in beyond.text


@pytest.mark.asyncio
async def test_empty_workspace_has_navigation_and_missing_project_is_not_an_empty_list(
    mail_client: AsyncClient,
) -> None:
    project = await mail_client.get("/mail/empty")
    assert project.status_code == 200
    assert any(link["href"] == "/mail/empty/threads" for link in _Links(project.text).links)
    listing = await mail_client.get("/mail/empty/threads")
    assert _thread_links(listing, "empty") == []
    assert "No threads yet" in listing.text
    assert any(link["href"] == "/mail/empty" for link in _Links(listing.text).links)
    assert 'aria-label="Thread pagination"' not in listing.text
    assert (await mail_client.get("/mail/missing/threads")).status_code == 404


@pytest.mark.asyncio
async def test_human_key_navigation_uses_canonical_slug_and_encodes_thread_identifiers(
    mail_client: AsyncClient,
) -> None:
    thread_id = "release ?#&"
    subject = "<script>alert(1)</script>"
    await _seed_messages(_message(1, thread_id, subject, 1))
    project_path = f"/mail/{quote('Workspace Alpha', safe='')}"
    project = await mail_client.get(project_path)
    assert project.status_code == 200
    assert any(link["href"] == "/mail/alpha/threads" for link in _Links(project.text).links)
    listing = await mail_client.get(f"{project_path}/threads")
    thread = _thread_links(listing)[0]
    assert thread["href"] == f"/mail/alpha/thread/{quote(thread_id, safe='')}"
    assert subject not in listing.text
    assert "&lt;script&gt;" in listing.text
    detail = await mail_client.get(thread["href"])
    assert detail.status_code == 200
    assert "Body for message 1." in detail.text


@pytest.mark.asyncio
async def test_search_message_and_inbox_navigation_still_work_without_nested_links(mail_client: AsyncClient) -> None:
    await _seed_messages(_message(1, "search-thread", "Searchable subject", 1))
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (1, 1, 'to')")
        )
        await session.commit()
    project = await mail_client.get("/mail/alpha", params={"q": "Searchable"})
    assert project.status_code == 200
    links = _Links(project.text).links
    assert any(link["href"] == "/mail/alpha/thread/search-thread" for link in links)
    assert any(link["href"] == "/mail/alpha/message/1" and "Searchable subject" in link["text"] for link in links)
    # The subject link still opens the whole card, except its independent thread link.
    assert "after:absolute after:inset-0" in project.text
    for path in ("/mail/alpha/search?q=Searchable", "/mail/alpha/inbox/BlueLake", "/mail/alpha/message/1"):
        response = await mail_client.get(path)
        assert response.status_code == 200
        assert "Searchable subject" in response.text


@pytest.mark.asyncio
async def test_multiple_recipients_do_not_inflate_counts_or_mark_messages_read(mail_client: AsyncClient) -> None:
    await _seed_messages(_message(1, "many-recipients", "Team update", 1))
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (1, :aid, 'to')"),
            [{"aid": 1}, {"aid": 3}],
        )
        await session.commit()
    threads = _thread_links(await mail_client.get("/mail/alpha/threads"))
    assert len(threads) == 1
    assert "1 message" in " ".join(threads[0]["text"].split())
    assert (await mail_client.get(threads[0]["href"])).status_code == 200
    async with get_session() as session:
        rows = await session.execute(text("SELECT read_ts, ack_ts FROM message_recipients ORDER BY agent_id"))
        assert [tuple(row) for row in rows.fetchall()] == [(None, None), (None, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,page", [(0, 0), (-4, -7)])
async def test_nonpositive_pagination_values_are_clamped(mail_client: AsyncClient, limit: int, page: int) -> None:
    await _seed_messages(_message(1, "one", "One", 1), _message(2, "two", "Two", 2))
    response = await mail_client.get("/mail/alpha/threads", params={"limit": limit, "page": page})
    assert len(_thread_links(response)) == 1
    assert any(link["href"] == "/mail/alpha/threads?page=2&limit=1" for link in _Links(response.text).links)


@pytest.mark.asyncio
async def test_thread_page_size_is_capped_and_invalid_parameters_are_rejected(mail_client: AsyncClient) -> None:
    await _seed_messages(*(_message(mid, f"thread-{mid}", f"Subject {mid}", mid) for mid in range(1, 202)))
    response = await mail_client.get("/mail/alpha/threads?limit=999999")
    assert len(_thread_links(response)) == 200
    assert any(link["href"] == "/mail/alpha/threads?page=2&limit=200" for link in _Links(response.text).links)
    beyond = await mail_client.get("/mail/alpha/threads?page=999999&limit=999999")
    assert _thread_links(beyond) == []
    assert any(link["href"] == "/mail/alpha/threads?page=9999&limit=200" for link in _Links(beyond.text).links)
    for params in ({"limit": "invalid"}, {"page": "invalid"}):
        assert (await mail_client.get("/mail/alpha/threads", params=params)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("project_key", ["alpha", "Workspace Alpha"])
@pytest.mark.parametrize("thread_id", [None, "", "   ", "release", "release ?#&", 'literal%2F"'])
async def test_thread_message_browsing_round_trip_stays_in_the_message_project(
    mail_client: AsyncClient,
    project_key: str,
    thread_id: str | None,
) -> None:
    conversation_id = thread_id if thread_id and thread_id.strip(" ") else "100"
    await _seed_messages(
        # The sender belongs to Beta, but the conversation belongs to Alpha.
        _message(100, thread_id, "Alpha opening", 1, sender_id=2),
        _message(101, conversation_id, "Alpha reply", 2),
        _message(200, conversation_id, "Beta private", 3, project_id=2, sender_id=2),
    )
    listing = await mail_client.get(f"/mail/{quote(project_key, safe='')}/threads")
    threads = _thread_links(listing)
    assert len(threads) == 1
    thread_url = f"/mail/alpha/thread/{quote(conversation_id, safe='')}"
    assert threads[0]["href"] == thread_url

    detail = await mail_client.get(threads[0]["href"])
    assert detail.status_code == 200
    detail_links = _Links(detail.text).links
    # Breadcrumb, header, and footer all return to the current workspace's list.
    assert sum(link["href"] == "/mail/alpha/threads" for link in detail_links) >= 3
    assert any(link["href"] == "/mail/alpha" for link in detail_links)
    assert "Body for message 200." not in detail.text

    for mid in (100, 101):
        message_link = next(link for link in detail_links if link["href"] == f"/mail/alpha/message/{mid}")
        message = await mail_client.get(message_link["href"])
        assert message.status_code == 200
        assert f"Body for message {mid}." in message.text
        assert "Body for message 200." not in message.text
        links = _Links(message.text).links
        conversation_links = [
            link for link in links if "View Thread" in link["text"] or "View Full Thread" in link["text"]
        ]
        assert conversation_links, "Even a standalone starter must link back to its conversation"
        assert all(link["href"] == thread_url for link in conversation_links)
        # Named threads also expose the conversation preview's separate link.
        if thread_id and thread_id.strip(" "):
            assert any("View Full Thread" in link["text"] for link in conversation_links)
        returned = await mail_client.get(conversation_links[0]["href"])
        assert returned.status_code == 200
        assert "Body for message 100." in returned.text
        assert "Body for message 101." in returned.text
        assert "Body for message 200." not in returned.text

        browse = next(link for link in links if link["href"] == "/mail/alpha/threads")
        reopened = await mail_client.get(browse["href"])
        assert [link["href"] for link in _thread_links(reopened)] == [thread_url]
        assert all(not link["href"].startswith("/mail/beta/thread/") for link in links)
        assert all(link["href"] != "/mail/beta/threads" for link in links)


@pytest.mark.asyncio
async def test_workspace_surfaces_recent_active_conversations(
    mail_client: AsyncClient,
) -> None:
    await _seed_messages(
        _message(1, "oldest", "Oldest Alpha", 1),
        _message(2, "busy", "Busy opening", 2),
        _message(3, "busy", "Busy latest", 30),
        _message(4, "middle-a", "Middle A", 10),
        _message(5, "middle-b", "Middle B", 11),
        _message(6, "middle-c", "Middle C", 12),
        _message(7, "middle-d", "Middle D", 13),
        _message(8, "newest", "Newest Alpha", 40),
        _message(9, "beta-only", "Beta only", 100, project_id=2, sender_id=2),
    )

    workspace = await mail_client.get("/mail/alpha")
    assert workspace.status_code == 200
    assert "Active Conversations" in workspace.text
    assert "Browse all threads" in workspace.text

    links = _thread_links(workspace)
    assert [link["href"] for link in links] == [
        "/mail/alpha/thread/newest",
        "/mail/alpha/thread/busy",
        "/mail/alpha/thread/middle-d",
        "/mail/alpha/thread/middle-c",
        "/mail/alpha/thread/middle-b",
        "/mail/alpha/thread/middle-a",
    ]
    assert "Busy latest" in links[1]["text"]
    assert "2 messages" in " ".join(links[1]["text"].split())
    assert "Oldest Alpha" not in workspace.text
    assert "Beta only" not in workspace.text
    assert "/mail/beta/thread/" not in workspace.text
    assert any(link["href"] == "/mail/alpha/threads" for link in _Links(workspace.text).links)


@pytest.mark.asyncio
async def test_workspace_active_conversations_empty_and_encoded(
    mail_client: AsyncClient,
) -> None:
    empty = await mail_client.get("/mail/empty")
    assert empty.status_code == 200
    assert "No conversations yet" in empty.text
    assert _thread_links(empty, "empty") == []

    thread_id = "workspace ?#&"
    await _seed_messages(_message(1, thread_id, "Encoded workspace thread", 1))
    project_path = f"/mail/{quote('Workspace Alpha', safe='')}"
    workspace = await mail_client.get(project_path)
    assert workspace.status_code == 200
    assert _thread_links(workspace)[0]["href"] == f"/mail/alpha/thread/{quote(thread_id, safe='')}"
    assert any(link["href"] == "/mail/alpha/threads" for link in _Links(workspace.text).links)


@pytest.mark.asyncio
async def test_numeric_thread_id_does_not_capture_unrelated_message_with_same_database_id(
    mail_client: AsyncClient,
) -> None:
    await _seed_messages(
        _message(41, "42", "Numeric thread opening", 1),
        _message(43, "42", "Numeric thread reply", 3),
        # Same numeric database ID as the thread key, but explicitly belongs
        # to another conversation and must not leak into thread "42".
        _message(42, "other-thread", "Unrelated message 42", 2),
    )

    listing = await mail_client.get("/mail/alpha/threads")
    assert listing.status_code == 200
    links = _thread_links(listing)
    hrefs = [link["href"] for link in links]
    assert "/mail/alpha/thread/42" in hrefs
    assert "/mail/alpha/thread/other-thread" in hrefs

    numeric = await mail_client.get("/mail/alpha/thread/42")
    assert numeric.status_code == 200
    assert "Body for message 41." in numeric.text
    assert "Body for message 43." in numeric.text
    assert "Body for message 42." not in numeric.text
    assert "Unrelated message 42" not in numeric.text

    other = await mail_client.get("/mail/alpha/thread/other-thread")
    assert other.status_code == 200
    assert "Body for message 42." in other.text
    assert "Body for message 41." not in other.text
    assert "Body for message 43." not in other.text


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "", "   ", "release", "\t", " \t ", "\u00a0"])
async def test_agent_inbox_exposes_independent_canonical_thread_navigation(
    mail_client: AsyncClient,
    thread_id: str | None,
) -> None:
    conversation_id = thread_id if thread_id and thread_id.strip(" ") else "100"
    await _seed_messages(_message(100, thread_id, "Inbox navigation", 1))
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (100, 1, 'to')")
        )
        await session.commit()

    response = await mail_client.get("/mail/alpha/inbox/BlueLake")
    assert response.status_code == 200
    links = _Links(response.text).links
    assert any(link["href"] == "/mail/alpha/threads" for link in links)
    assert f'"conversation_id": {json.dumps(conversation_id)}' in response.text
    assert "encodeURIComponent(item.conversation_id)" in response.text


def _conversation_preview_links(response: Response) -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    section = response.text.split("<!-- Thread Quick Info - Link to Full Thread View -->", 1)[1]
    return _Links(section).links


@pytest.mark.asyncio
@pytest.mark.parametrize("project_key", ["alpha", "Workspace Alpha"])
@pytest.mark.parametrize("starter_thread", [None, "", "   ", "release", "00100", "\t", " \t ", "release ?#&"])
async def test_message_preview_includes_starters_and_replies_in_the_message_project(
    mail_client: AsyncClient,
    project_key: str,
    starter_thread: str | None,
) -> None:
    conversation_id = starter_thread if starter_thread and starter_thread.strip(" ") else "100"
    await _seed_messages(
        # A remote sender does not change the message's project.
        _message(100, starter_thread, "Opening", 1, sender_id=2),
        _message(101, conversation_id, "First reply", 2),
        _message(102, conversation_id, "Second reply", 3),
        _message(200, conversation_id, "Other project", 4, project_id=2, sender_id=2),
        _message(201, "unrelated", "Other conversation", 5),
    )
    thread_url = f"/mail/alpha/thread/{quote(conversation_id, safe='')}"
    listing = await mail_client.get(f"/mail/{quote(project_key, safe='')}/threads")
    entry = next(link for link in _thread_links(listing) if link["href"] == thread_url)
    assert "3 messages" in " ".join(entry["text"].split())

    for mid in (100, 101, 102):
        response = await mail_client.get(f"/mail/{quote(project_key, safe='')}/message/{mid}")
        links = _conversation_preview_links(response)
        assert ">3 messages</span>" in response.text
        assert [link["href"] for link in links if "/message/" in link["href"]] == [
            f"/mail/alpha/message/{other}" for other in (100, 101, 102) if other != mid
        ]
        full_thread = next(link for link in links if "View Full Thread" in link["text"])
        assert full_thread["href"] == thread_url
        detail = await mail_client.get(full_thread["href"])
        assert detail.status_code == 200
        for other in (100, 101, 102):
            assert f"Body for message {other}." in detail.text
        assert "Body for message 200." not in detail.text
        assert "Body for message 201." not in detail.text
        assert not any("/mail/beta/" in link["href"] for link in links)
        assert not any("more" in link["text"] for link in links)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 5, 6, 7, 12, 202])
@pytest.mark.parametrize("position", ["first", "last"])
async def test_message_preview_is_bounded_without_losing_the_total_or_hiding_an_extra_reply(
    mail_client: AsyncClient,
    count: int,
    position: str,
) -> None:
    # Equal timestamps must have deterministic ID ordering, including the opener.
    await _seed_messages(*(
        _message(mid, None if mid == 100 else "100", f"Conversation message {mid}", 1)
        for mid in range(100, 100 + count)
    ))
    mid = 100 if position == "first" else 99 + count
    response = await mail_client.get(f"/mail/alpha/message/{mid}")
    links = _conversation_preview_links(response)
    expected_ids = [other for other in range(100, 100 + count) if other != mid][:5]
    assert [link["href"] for link in links if "/message/" in link["href"]] == [
        f"/mail/alpha/message/{other}" for other in expected_ids
    ]
    if count == 1:
        assert "Part of a Conversation Thread" not in response.text
        assert not links
        return
    assert f">{count} messages</span>" in response.text
    remaining = count - 1 - len(expected_ids)
    more = [link for link in links if "more" in link["text"]]
    if remaining:
        assert len(more) == 1
        assert " ".join(more[0]["text"].split()) == f"+{remaining} more"
        assert more[0]["href"] == "/mail/alpha/thread/100"
    else:
        assert not more


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", ["42", "0042", "99999999999999999999999999"])
async def test_message_preview_keeps_numeric_conversations_distinct(
    mail_client: AsyncClient,
    thread_id: str,
) -> None:
    await _seed_messages(
        _message(41, thread_id, "Numeric opening", 1),
        _message(43, thread_id, "Numeric reply", 2),
        _message(42, "other-thread", "Unrelated message 42", 3),
        _message(50, "00042", "A distinct numeric identifier", 4),
    )
    response = await mail_client.get("/mail/alpha/message/43")
    links = _conversation_preview_links(response)
    assert ">2 messages</span>" in response.text
    assert [link["href"] for link in links if "/message/" in link["href"]] == ["/mail/alpha/message/41"]
    assert next(link for link in links if "View Full Thread" in link["text"])["href"] == (
        f"/mail/alpha/thread/{thread_id}"
    )
    assert "Unrelated message 42" not in response.text
    assert "A distinct numeric identifier" not in response.text


@pytest.mark.asyncio
async def test_message_preview_does_not_change_recipient_state_or_duplicate_recipients(
    mail_client: AsyncClient,
) -> None:
    await _seed_messages(_message(100, None, "Opening", 1), _message(101, "100", "Reply", 2))
    async with get_session() as session:
        await session.execute(
            text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (:mid, :aid, 'to')"),
            [{"mid": mid, "aid": aid} for mid in (100, 101) for aid in (1, 3)],
        )
        await session.commit()
    response = await mail_client.get("/mail/alpha/message/100")
    assert response.status_code == 200
    assert ">2 messages</span>" in response.text
    assert [link["href"] for link in _conversation_preview_links(response) if "/message/" in link["href"]] == [
        "/mail/alpha/message/101"
    ]
    async with get_session() as session:
        rows = await session.execute(text("SELECT read_ts, ack_ts FROM message_recipients"))
        assert [tuple(row) for row in rows.fetchall()] == [(None, None)] * 4


def _thread_page_message_ids(response: Response) -> list[int]:
    assert response.status_code == 200, response.text
    prefix = "/mail/alpha/message/"
    return [
        int(link["href"][len(prefix):])
        for link in _Links(response.text).links
        if link["href"].startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_thread_detail_opens_latest_page_and_all_history_is_reachable(mail_client: AsyncClient) -> None:
    await _seed_messages(*(
        _message(mid, "busy", "Opening subject" if mid == 1 else f"Reply {mid}", 1)
        for mid in range(1, 122)
    ))
    response = await mail_client.get("/mail/alpha/thread/busy")
    assert _thread_page_message_ids(response) == list(range(101, 122))
    assert "121 messages" in response.text
    assert "Opening subject" in response.text
    assert "Messages 101\u2013121 of 121" in response.text
    assert "End of conversation" in response.text

    all_ids = []
    while True:
        all_ids[:0] = _thread_page_message_ids(response)
        previous = [link for link in _Links(response.text).links if "Older messages" in link["text"]]
        if not previous:
            break
        response = await mail_client.get(previous[0]["href"])
        assert "Opening subject" in response.text
    assert all_ids == list(range(1, 122))
    assert "More recent messages available" in response.text
    latest = next(link for link in _Links(response.text).links if "Latest messages" in link["text"])
    assert latest["href"] == "/mail/alpha/thread/busy?limit=50"
    assert _thread_page_message_ids(await mail_client.get(latest["href"])) == list(range(101, 122))


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,page,expected", [
    (0, 0, [1]),
    (-4, -7, [1]),
    (2, 1, [1, 2]),
    (2, 2, [3, 4]),
    (2, 10**60, [5]),
    (2, -(10**60), [1, 2]),
    (10**60, 1, [1, 2, 3, 4, 5]),
])
async def test_thread_detail_clamps_parameters_before_sqlite_binding(
    mail_client: AsyncClient, limit: int, page: int, expected: list[int],
) -> None:
    await _seed_messages(*(_message(mid, "busy", f"Reply {mid}", 1) for mid in range(1, 6)))
    response = await mail_client.get("/mail/alpha/thread/busy", params={"limit": limit, "page": page})
    assert _thread_page_message_ids(response) == expected
    assert "5 messages" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("parameters", [{"page": "bad"}, {"limit": "bad"}, {"page": "1.5"}])
async def test_thread_detail_rejects_invalid_pagination(
    mail_client: AsyncClient, parameters: dict[str, str],
) -> None:
    response = await mail_client.get("/mail/alpha/thread/busy", params=parameters)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("starter_thread", [None, "", "   "])
async def test_paginated_thread_keeps_unthreaded_opener_with_replies(
    mail_client: AsyncClient, starter_thread: str | None,
) -> None:
    await _seed_messages(
        _message(42, starter_thread, "Opening subject", 1),
        *(_message(mid, "42", f"Reply {mid}", 1) for mid in range(43, 47)),
    )
    first = await mail_client.get("/mail/alpha/thread/42?limit=2&page=1")
    second = await mail_client.get("/mail/alpha/thread/42?limit=2&page=2")
    last = await mail_client.get("/mail/alpha/thread/42?limit=2")
    assert _thread_page_message_ids(first) == [42, 43]
    assert _thread_page_message_ids(second) == [44, 45]
    assert _thread_page_message_ids(last) == [46]
    assert "5 messages" in last.text
    assert "Opening subject" in last.text


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", ["007", "release ?#&", "\t", "\u00a0", "会話"])
async def test_thread_pagination_links_remain_project_scoped_and_encoded(
    mail_client: AsyncClient, thread_id: str,
) -> None:
    await _seed_messages(
        *(_message(mid, thread_id, f"Alpha {mid}", 1, sender_id=2) for mid in range(100, 105)),
        _message(7, "other", "Unrelated numeric collision", 1),
        _message(200, thread_id, "Beta private", 2, project_id=2, sender_id=2),
    )
    path = f"/mail/{quote('Workspace Alpha', safe='')}/thread/{quote(thread_id, safe='')}"
    response = await mail_client.get(path, params={"limit": 2, "page": 2})
    assert _thread_page_message_ids(response) == [102, 103]
    assert "5 messages" in response.text
    assert "Body for message 200." not in response.text
    assert "Unrelated numeric collision" not in response.text
    links = _Links(response.text).links
    older = next(link for link in links if "Older messages" in link["text"])
    newer = next(link for link in links if "Newer messages" in link["text"])
    assert older["href"] == f"/mail/alpha/thread/{quote(thread_id, safe='')}?page=1&limit=2"
    assert newer["href"] == f"/mail/alpha/thread/{quote(thread_id, safe='')}?page=3&limit=2"
    assert _thread_page_message_ids(await mail_client.get(older["href"])) == [100, 101]
    assert _thread_page_message_ids(await mail_client.get(newer["href"])) == [104]


@pytest.mark.asyncio
async def test_thread_page_size_is_capped_without_changing_recipient_state(mail_client: AsyncClient) -> None:
    await _seed_messages(*(_message(mid, "busy", f"Reply {mid}", 1) for mid in range(1, 402)))
    async with get_session() as session:
        await session.execute(text("INSERT INTO message_recipients (message_id, agent_id, kind) VALUES (201, 1, 'to')"))
        await session.commit()
    response = await mail_client.get("/mail/alpha/thread/busy?limit=999999&page=2")
    assert _thread_page_message_ids(response) == list(range(201, 401))
    assert "401 messages" in response.text
    assert "Body for message 401." not in response.text
    async with get_session() as session:
        row = (await session.execute(text("SELECT read_ts, ack_ts FROM message_recipients WHERE message_id = 201"))).one()
        assert tuple(row) == (None, None)
