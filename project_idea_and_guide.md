# Original prompt:

```
I have an idea for a python project I want you to help me design in the best way possible, doing research on the web and really thinking super hard about the best way to do things.

Basically, it's a python mcp (model context protocol) server that is built using the fastmcp library with http transport only (not stdio or sse, which are deprecated).

The name of the project is `mcp-agent-mail`. The purpose of it is to coordinate and weave together the efforts of multiple coding agents (such as the codex cli tool from openai; the claude code tool from anthropic; the gemini-cli from google, open-code, etc) so they can all work on the same project but in a more coordinated way.

The basic mechanism will be a set of tools that allows agents, upon starting work on a task, to create a tempoary but persistent for the life of the task, identity using some memorable but unique name (this could come from picking random from two long sets of strings to make a name, such as [Red, Orange, Pink, Black, Purple, Blue, Brown, White, Green, Chartreuse, Lilac, Fuscia, etc.] and [Stone, Lake, Dog, Creak, Pond, Cat, Bear, Mountain, Hill, Snow, Castle, etc.] so we get agent names like GreenCastle or RedCat); this identity would be paired with the agent's program  and model type (say, `Claude Code using Opus 4.1`, or `Codex using GPT-5-Codex with High Reasoning Effort` ), with an inception date-time; with a project identifier (say, the working directory the coding agent started in), and a task description. 

That identity would then be linked to what is essentially like an "email account" for the agent which lets it send and receive messages (formatted as github-flavored markdown that allows for images to be included by reference to a file or base64 encoded highly compressed webp images in-line); these messages could be addressed to one or more agents by name (e.g., RedCat), and each agent would have the equivalent of an LDAP server too look up agents and see their name and other info about what they're working on, what coding agent software/model they're using, the date-time they were last active, the most recent changes they made, etc. Each of these messages would be stored using sqlite in a sensible way that allows for easy searching, but also as actual .md files in a sensible folder structure that mirrors the agent identity structure and we could use git to track all changes, so that every time a message is created and sent by and agent to other agents, it creates a file and a commit in this repo, which then lets us piggy back on all the git related functionality. 

So what is the purpose of this? Well, you could have them all working on one big project; say you have a python fastapi backend and a nextjs frontend each in separate repos. You want to have a few agents working in the backend AND the frontend all at the same time. But you don't want to them to conflict with each other; you don't want the backend agents to pick the same file to work on and then get confused and freak out when unrecognized changes start happening and they decide to do a `git checkout` on the file and wipe out the other agent's work. Or you want your backend agents to be using codex with gpt5, while your frontend agents are using claude code with opus, and you want everything to be perfectly harmonized. You can manually do this now by having the agents created detailed documentation but that requires a ton of manual instructions and often the human in the loop has to act as the "liason" passing messages from one agent to another. It would be much better and easier if they could instead communicate without each other autonomously and keep each other informed of what's happening. But this has to be done in a way that fits into existing workflows nicely. So for one thing, it MUST be fully async; when an agent finishes working on a step, it can then call a "check_my_messages" mcp tool that retrieves any recent messages and shows them to the agent so they can keep them in mind. Some of these messages could have urgent action items requiring immediate response and action. Other might be more like "FYI" messages that the agent should keep in mind. And not every agent NEEDS to know about everything all the other agents are doing or working on; in fact, that would be distracting and detrimental and waste context space. But each agent could keep track or look up which agent is doing what, and then address messages just to the relevant agents to keep things efficient. 
```


# Response 1 from GPT-5 Pro:

```
Skip to content
ChatGPT said:

Below is a rigorous design for mcp-agent-mail, with an MVP server you can run today (HTTP transport only) and extend. I did a sweep of the MCP ecosystem to make sure the architecture sits on the current rails: Streamable HTTP is the recommended remote transport; the legacy HTTP+SSE transport is deprecated; STDIO is still common locally (not deprecated), but you asked for HTTP‑only and the code honors that.
gofastmcp.com+2Model Context Protocol+2
Core idea, constraints, and assumptions

    Agents (e.g., Claude Code, Codex, Gemini CLI) coordinate asynchronously through an MCP server exposing a mail-like toolset and directory/“LDAP” queries.

    Transport is Streamable HTTP only: we'll expose a single /mcp/ endpoint via FastMCP 2.0, which natively supports this and can run as a direct HTTP server or as an ASGI app behind Uvicorn.
    gofastmcp.com

    Messages are GFM markdown persisted in Git (per project), plus SQLite metadata with FTS5 for search; Git commits on each message/claim update let us piggyback diffing, history, and blame.
    SQLite

    Agents get ephemeral, memorable identities (e.g., GreenCastle) bound to program/model, inception time, project id, and task description; generated from adjective+noun sets. (You can swap in a library like unique-names-generator if you prefer.)
    PyPI

Why MCP + FastMCP + Streamable HTTP

    MCP gives you an interoperable tool vocabulary (tools/resources/prompts) and JSON‑RPC semantics across transports; Streamable HTTP is the path forward for remote deployments.
    Model Context Protocol+1

    FastMCP 2.0 is a high‑level Python framework that reduces boilerplate; mcp.run(transport="http", host=..., port=..., path="/mcp") stands up a production‑grade HTTP endpoint at /mcp/. It also provides an http_app() if you prefer running under Uvicorn/Gunicorn.
    gofastmcp.com+1

High-level architecture

+-------------------------------+         +-------------------+
| Coding Agents (various CLIs)  |  HTTP   |  mcp-agent-mail   |
|  - call MCP tools             +-------->+  FastMCP server   |
|  - pull messages on demand    |         |  (Streamable HTTP)|
+-------------------------------+         +----------+--------+
                                                     |
                                                     | writes/reads
                                               +-----v-------------------+
                                               | Per-project Git repos   |
                                               |   .mcp-mail/            |
                                               |   ├─ agents/<name>/     |
                                               |   │    inbox/outbox/... |
                                               |   ├─ messages/YYYY/MM   |
                                               |   └─ claims/            |
                                               +-----+--------------------+
                                                     |
                                                     | indexes & metadata
                                               +-----v-------------------+
                                               | SQLite (FTS5)           |
                                               |  agents/messages/claims |
                                               +-------------------------+

Key flows

    Agent registers → register_agent(...) stores profile in SQLite, writes agents/<name>/profile.json and commits.

    Agent sends message → send_message(...) writes one canonical .md plus per‑recipient mailbox copies, commits, indexes in FTS5.

    Agent checks messages → check_my_messages(...) pulls unread/urgent since a timestamp, optionally includes bodies.

    Avoiding conflicts → reserve_file_paths(...) creates TTL leases for files/globs with Git‑tracked claim files and DB rows; agents voluntarily honor leases before editing; conflicts are resolved at the MCP layer before the edit loop begins.

On transport choices (brief correction)

    SSE transport (the old "HTTP+SSE" pairing) is deprecated; Streamable HTTP replaced it. STDIO remains widely used for local tools (not deprecated). You asked for HTTP‑only; the server below runs with transport="http" and doesn't expose STDIO/SSE.
    gofastmcp.com+1

Data model (SQLite + FTS5)

Tables (selected):

    projects(id TEXT PRIMARY KEY, human_key TEXT, slug TEXT UNIQUE, created_ts INTEGER, meta JSON)

    agents(id TEXT PRIMARY KEY, project_id TEXT, name TEXT, program TEXT, model TEXT, inception_ts INTEGER, task TEXT, last_active_ts INTEGER, UNIQUE(project_id,name))

    messages(id TEXT PRIMARY KEY, project_id TEXT, thread_id TEXT, subject TEXT, body_md TEXT, from_agent TEXT, created_ts INTEGER, importance TEXT, ack_required INTEGER)

    message_recipients(message_id TEXT, agent_name TEXT, kind TEXT, read_ts INTEGER, ack_ts INTEGER, PRIMARY KEY(message_id,agent_name,kind))

    claims(id TEXT PRIMARY KEY, project_id TEXT, agent_name TEXT, path TEXT, exclusive INTEGER, reason TEXT, created_ts INTEGER, expires_ts INTEGER, released_ts INTEGER)

    fts_messages (FTS5 virtual table on subject, body_md with external content & triggers).
    SQLite+1

Search: SELECT m.id FROM fts_messages WHERE fts_messages MATCH ? LIMIT ? and join to messages.
On‑disk layout (per project)

<mcp-mail-store>/projects/<slug>/repo/.git/
<mcp-mail-store>/projects/<slug>/repo/
  agents/<AgentName>/profile.json
  agents/<AgentName>/inbox/YYYY/MM/<msg-id>.md
  agents/<AgentName>/outbox/YYYY/MM/<msg-id>.md
  messages/YYYY/MM/<msg-id>.md
  claims/<sha1-of-path>.json

Reasoning:

    Dual persistence: human‑auditable markdown + database for indexing/queries.

    Per-recipient mailbox copies keep the Git UX intuitive (open agents/RedCat/inbox/... in an editor).

    Claims are visible artifacts in Git history and easy to diff/review.

Message file format (GFM + YAML frontmatter)

---
id: "msg_20251023_7b3dc3a7"
thread_id: "TKT-123"
project: "backend-repo"
from: "GreenCastle"
to: ["RedCat","BlueLake"]
cc: []
created: "2025-10-23T15:22:14Z"
importance: "high"
ack_required: true
attachments:
  - type: "file"
    path: "../../attachments/2a/2a6f.../diagram.webp"
  # or:
  - type: "inline"
    media_type: "image/webp"
    data_base64: "<very-compressed-base64>"
---

# Build plan for /api/users routes

...

"LDAP" directory semantics

    list_agents(project, active_only) returns name/program/model/last_active/task and recent changes (derived from Git log scoped to .mcp-mail and optionally your code repo if you configure that).

    whois(agent_name) returns full profile.

File ownership & conflict avoidance

    Leases (claims): reserve_file_paths(agent, ["app/api/*.py"], ttl=3600, exclusive=True, reason="migrations")

        Returns conflicts if another active exclusive claim overlaps.

        Creates claims/<hash>.json and DB row with expires_ts; leases are advisory but visible to all agents. You can add a pre‑commit hook to enforce (future enhancement).

    Strategy: optimistic editing inside a claimed surface; other agents respect TTL and either take shared claims (read/observe) or wait.

    Git's own index locking won't help coordinate intent across agents; it only protects local writes. We surface intent explicitly with lease files and DB state.
    Stack Overflow

Security/ops

    Run as HTTP server at /mcp/; add bearer/JWT/OAuth later via FastMCP's auth helpers (not included in the MVP).
    gofastmcp.com

    Health endpoint: easy to add as a custom route if you deploy behind a LB.
    gofastmcp.com

Minimal Viable Server (HTTP‑only) — complete code

This is a working baseline that implements identities, directory, messaging (with per‑recipient mailboxes), claims/leases, FTS search, and Git persistence. It uses Streamable HTTP only.

pyproject.toml

[project]
name = "mcp-agent-mail"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
  "fastmcp>=2.3.0",
  "GitPython>=3.1.40",
  "filelock>=3.12.2"
]

[tool.uv]
# if you use Astral's uv

[tool.setuptools]
py-modules = []

server.py

from __future__ import annotations
import os, re, json, time, uuid, base64, hashlib, sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any
from filelock import FileLock
from git import Repo, Actor
from fastmcp import FastMCP, Context

# ---------- configuration ----------
STORE_ROOT = Path(os.environ.get("MCP_MAIL_STORE", "~/.mcp-agent-mail")).expanduser()
DB_PATH = STORE_ROOT / "store.sqlite3"
PROJECTS_DIR = STORE_ROOT / "projects"
COMMIT_AUTHOR = Actor("mcp-agent-mail", "mcp-agent-mail@local")
NOW = lambda: int(time.time())
ISO = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

ADJECTIVES = ["Red","Orange","Pink","Black","Purple","Blue","Brown","White","Green","Chartreuse","Lilac","Fuscia"]
NOUNS = ["Stone","Lake","Dog","Creak","Pond","Cat","Bear","Mountain","Hill","Snow","Castle"]

mcp = FastMCP("mcp-agent-mail")

# ---------- storage bootstrap ----------
def _ensure_dirs():
    STORE_ROOT.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _init_db():
    _ensure_dirs()
    conn = _db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS projects(
      id TEXT PRIMARY KEY,
      human_key TEXT,
      slug TEXT UNIQUE,
      created_ts INTEGER,
      meta TEXT
    );
    CREATE TABLE IF NOT EXISTS agents(
      id TEXT PRIMARY KEY,
      project_id TEXT,
      name TEXT,
      program TEXT,
      model TEXT,
      inception_ts INTEGER,
      task TEXT,
      last_active_ts INTEGER,
      UNIQUE(project_id,name),
      FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS messages(
      id TEXT PRIMARY KEY,
      project_id TEXT,
      thread_id TEXT,
      subject TEXT,
      body_md TEXT,
      from_agent TEXT,
      created_ts INTEGER,
      importance TEXT,
      ack_required INTEGER,
      FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS message_recipients(
      message_id TEXT,
      agent_name TEXT,
      kind TEXT,             -- 'to' | 'cc' | 'bcc'
      read_ts INTEGER,
      ack_ts INTEGER,
      PRIMARY KEY(message_id, agent_name, kind),
      FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS claims(
      id TEXT PRIMARY KEY,
      project_id TEXT,
      agent_name TEXT,
      path TEXT,
      exclusive INTEGER,
      reason TEXT,
      created_ts INTEGER,
      expires_ts INTEGER,
      released_ts INTEGER,
      FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    -- FTS5: external content to avoid duplication
    CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(
      subject, body_md, content='messages', content_rowid='rowid'
    );
    CREATE TRIGGER IF NOT EXISTS trg_messages_ai AFTER INSERT ON messages BEGIN
      INSERT INTO fts_messages(rowid, subject, body_md) VALUES (new.rowid, new.subject, new.body_md);
    END;
    CREATE TRIGGER IF NOT EXISTS trg_messages_ad AFTER DELETE ON messages BEGIN
      INSERT INTO fts_messages(fts_messages, rowid, subject, body_md) VALUES('delete', old.rowid, old.subject, old.body_md);
    END;
    CREATE TRIGGER IF NOT EXISTS trg_messages_au AFTER UPDATE ON messages BEGIN
      INSERT INTO fts_messages(fts_messages, rowid, subject, body_md) VALUES('delete', old.rowid, old.subject, old.body_md);
      INSERT INTO fts_messages(rowid, subject, body_md) VALUES (new.rowid, new.subject, new.body_md);
    END;
    """)
    conn.commit()
    conn.close()

_init_db()

# ---------- helpers ----------
def _slug(s: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9._-]+", "-", s.strip())[:40] or "proj"
    digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"

def _project_get_or_create(human_key: str) -> Dict[str, Any]:
    conn = _db()
    slug = _slug(human_key)
    cur = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,))
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    ts = NOW()
    conn.execute(
        "INSERT INTO projects(id,human_key,slug,created_ts,meta) VALUES(?,?,?,?,?)",
        (pid, human_key, slug, ts, json.dumps({}))
    )
    conn.commit()
    conn.close()
    # ensure repo exists
    repo_dir = PROJECTS_DIR / slug / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        Repo.init(repo_dir)
        (repo_dir / ".gitattributes").write_text("# reserved for future use\n", encoding="utf-8")
        repo = Repo(repo_dir)
        repo.index.add([".gitattributes"])
        repo.index.commit("chore: initialize mcp-agent-mail repo", author=COMMIT_AUTHOR, committer=COMMIT_AUTHOR)
    return {"id": pid, "human_key": human_key, "slug": slug, "created_ts": ts, "meta": "{}"}

def _repo_for_project(slug: str) -> Repo:
    return Repo(PROJECTS_DIR / slug / "repo")

def _project_paths(slug: str) -> Dict[str, Path]:
    root = PROJECTS_DIR / slug / "repo"
    return {
        "root": root,
        "agents": root / "agents",
        "messages": root / "messages",
        "claims": root / "claims",
    }

def _ensure_project_tree(slug: str):
    p = _project_paths(slug)
    for k, v in p.items():
        if k != "root":
            v.mkdir(parents=True, exist_ok=True)

def _unique_agent_name(conn: sqlite3.Connection, project_id: str, hint: Optional[str]) -> str:
    if hint:
        # sanitise and check uniqueness
        candidate = re.sub(r"[^a-zA-Z0-9]", "", hint.strip())[:40] or None
        if candidate:
            row = conn.execute("SELECT 1 FROM agents WHERE project_id=? AND name=?", (project_id, candidate)).fetchone()
            if not row:
                return candidate
    # generate adjective+noun
    import random
    for _ in range(1000):
        candidate = f"{random.choice(ADJECTIVES)}{random.choice(NOUNS)}"
        row = conn.execute("SELECT 1 FROM agents WHERE project_id=? AND name=?", (project_id, candidate)).fetchone()
        if not row:
            return candidate
    raise RuntimeError("could not generate unique agent name")

def _write_json(path: Path, data: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

def _write_markdown(path: Path, frontmatter: Dict[str, Any], body_md: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = json.dumps(frontmatter, indent=2, sort_keys=False)
    # YAML is common, but we use JSON frontmatter fenced by ---json for simplicity
    content = f"---json\n{fm}\n---\n\n{body_md.strip()}\n"
    path.write_text(content, encoding="utf-8")

def _y_m_dirs(base: Path, ts: int) -> Path:
    return base / time.strftime("%Y", time.gmtime(ts)) / time.strftime("%m", time.gmtime(ts))

def _commit(repo: Repo, paths: List[str], message: str):
    if not paths: return
    repo.index.add(paths)
    repo.index.commit(message, author=COMMIT_AUTHOR, committer=COMMIT_AUTHOR)

def _project_by_slug(conn: sqlite3.Connection, slug: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()

def _touch_last_active(conn: sqlite3.Connection, project_id: str, agent_name: str):
    conn.execute("UPDATE agents SET last_active_ts=? WHERE project_id=? AND name=?", (NOW(), project_id, agent_name))
    conn.commit()

# ---------- MCP tools ----------

@mcp.tool
def create_agent(project_key: str, program: str, model: str, task_description: str, name_hint: Optional[str]=None) -> Dict[str, Any]:
    """
    Create a temporary-but-persistent agent identity in a project. Returns profile.
    project_key: human identifier for the project, e.g., a working directory path or repo URL
    """
    proj = _project_get_or_create(project_key)
    slug = proj["slug"]
    _ensure_project_tree(slug)
    repo = _repo_for_project(slug)
    paths = _project_paths(slug)
    lock = FileLock(str(paths["root"] / ".mcp-mail.lock"))

    with lock:
        conn = _db()
        try:
            name = _unique_agent_name(conn, proj["id"], name_hint)
            aid = f"agent_{uuid.uuid4().hex[:10]}"
            ts = NOW()
            conn.execute(
                "INSERT INTO agents(id,project_id,name,program,model,inception_ts,task,last_active_ts) VALUES(?,?,?,?,?,?,?,?)",
                (aid, proj["id"], name, program, model, ts, task_description, ts)
            )
            conn.commit()
        finally:
            conn.close()

        # write profile.json in git
        profile = {
            "id": aid,
            "name": name,
            "program": program,
            "model": model,
            "project_slug": slug,
            "inception_ts": ts,
            "inception_iso": ISO(ts),
            "task": task_description
        }
        prof_path = paths["root"] / "agents" / name / "profile.json"
        _write_json(prof_path, profile)
        _commit(repo, [str(prof_path.relative_to(paths["root"]))], f"agent: create {name}")
        return profile

@mcp.tool
def whois(project_key: str, agent_name: str) -> Dict[str, Any]:
    """Return a single agent profile."""
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug=? OR human_key=?", (_slug(project_key), project_key)).fetchone()
    if not proj:
        conn.close()
        raise ValueError("project not found")
    row = conn.execute("SELECT * FROM agents WHERE project_id=? AND name=?", (proj["id"], agent_name)).fetchone()
    conn.close()
    if not row: raise ValueError("agent not found")
    return {k: row[k] for k in row.keys()}

@mcp.tool
def list_agents(project_key: str, active_only: bool=True) -> List[Dict[str, Any]]:
    """List agents in a project with recent activity metadata."""
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug=? OR human_key=?", (_slug(project_key), project_key)).fetchone()
    if not proj:
        conn.close()
        return []
    q = "SELECT name, program, model, task, inception_ts, last_active_ts FROM agents WHERE project_id=?"
    rows = conn.execute(q, (proj["id"],)).fetchall()
    conn.close()
    out = []
    now = NOW()
    for r in rows:
        if active_only and r["last_active_ts"] and now - r["last_active_ts"] > 7*24*3600:
            continue
        out.append(dict(r))
    return out

@mcp.tool
def send_message(project_key: str, from_agent: str, to: List[str], subject: str, body_md: str,
                 cc: Optional[List[str]]=None, bcc: Optional[List[str]]=None,
                 importance: str="normal", ack_required: bool=False, thread_id: Optional[str]=None) -> Dict[str, Any]:
    """
    Send a markdown message to one or more agents. Creates Git commits for message and per-recipient inbox copies.
    """
    cc = cc or []; bcc = bcc or []
    proj = _project_get_or_create(project_key)
    slug = proj["slug"]
    _ensure_project_tree(slug)
    repo = _repo_for_project(slug)
    paths = _project_paths(slug)
    lock = FileLock(str(paths["root"] / ".mcp-mail.lock"))

    ts = NOW()
    mid = f"msg_{time.strftime('%Y%m%d', time.gmtime(ts))}_{uuid.uuid4().hex[:8]}"
    fm = {
        "id": mid,
        "thread_id": thread_id,
        "project": project_key,
        "from": from_agent,
        "to": to,
        "cc": cc,
        "created": ISO(ts),
        "importance": importance,
        "ack_required": ack_required
    }

    with lock:
        conn = _db()
        try:
            # validate sender exists
            pid = conn.execute("SELECT id FROM projects WHERE slug=? OR human_key=?", (slug, project_key)).fetchone()["id"]
            srow = conn.execute("SELECT 1 FROM agents WHERE project_id=? AND name=?", (pid, from_agent)).fetchone()
            if not srow: raise ValueError("from_agent not registered")
            # store DB
            conn.execute(
                "INSERT INTO messages(id,project_id,thread_id,subject,body_md,from_agent,created_ts,importance,ack_required) VALUES(?,?,?,?,?,?,?,?,?)",
                (mid, pid, thread_id, subject, body_md, from_agent, ts, importance, int(ack_required))
            )
            for name, kind in [(n,"to") for n in to] + [(n,"cc") for n in cc] + [(n,"bcc") for n in bcc]:
                conn.execute("INSERT OR IGNORE INTO message_recipients(message_id,agent_name,kind,read_ts,ack_ts) VALUES(?,?,?,?,?)",
                             (mid, name, kind, None, None))
            conn.commit()
            _touch_last_active(conn, pid, from_agent)
        finally:
            conn.close()

        # write canonical message
        ymd_dir = _y_m_dirs(paths["messages"], ts)
        canonical = ymd_dir / f"{mid}.md"
        _write_markdown(canonical, fm | {"subject": subject}, body_md)

        # write outbox copy for sender
        outbox_copy = _y_m_dirs(paths["root"] / "agents" / from_agent / "outbox", ts) / f"{mid}.md"
        _write_markdown(outbox_copy, fm | {"subject": subject}, body_md)

        # write inbox copies
        inbox_paths = []
        for r in to + cc + bcc:
            inbox_path = _y_m_dirs(paths["root"] / "agents" / r / "inbox", ts) / f"{mid}.md"
            _write_markdown(inbox_path, fm | {"subject": subject}, body_md)
            inbox_paths.append(str(inbox_path.relative_to(paths["root"])))

        # commit
        rels = [str(canonical.relative_to(paths["root"])), str(outbox_copy.relative_to(paths["root"]))] + inbox_paths
        _commit(repo, rels, f"mail: {from_agent} -> {', '.join(to or ['(none)'])} | {subject}")
        return {"id": mid, "created": ISO(ts), "subject": subject, "recipients": {"to": to, "cc": cc, "bcc": bcc}}

@mcp.tool
def check_my_messages(project_key: str, agent_name: str, since_ts: Optional[int]=None,
                      urgent_only: bool=False, include_bodies: bool=False, limit: int=20) -> List[Dict[str, Any]]:
    """
    Fetch recent messages for an agent. Marks nothing as read; just returns.
    """
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug=? OR human_key=?", (_slug(project_key), project_key)).fetchone()
    if not proj:
        conn.close(); return []
    p = (proj["id"], agent_name)
    q = """
    SELECT m.id, m.subject, m.body_md, m.from_agent, m.created_ts, m.importance, m.ack_required, mr.kind
    FROM messages m
    JOIN message_recipients mr ON mr.message_id = m.id
    WHERE m.project_id=? AND mr.agent_name=?
    """
    args = list(p)
    if since_ts:
        q += " AND m.created_ts > ?"; args.append(since_ts)
    if urgent_only:
        q += " AND m.importance IN ('high','urgent')"
    q += " ORDER BY m.created_ts DESC LIMIT ?"; args.append(limit)
    rows = conn.execute(q, tuple(args)).fetchall()
    _touch_last_active(conn, proj["id"], agent_name)
    conn.close()
    out = []
    for r in rows:
        item = {
            "id": r["id"], "subject": r["subject"], "from": r["from_agent"],
            "created": ISO(r["created_ts"]), "importance": r["importance"],
            "ack_required": bool(r["ack_required"]), "kind": r["kind"]
        }
        if include_bodies: item["body_md"] = r["body_md"]
        out.append(item)
    return out

@mcp.tool
def acknowledge_message(project_key: str, agent_name: str, message_id: str) -> Dict[str, Any]:
    """Acknowledge a message addressed to agent_name."""
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug=? OR human_key=?", (_slug(project_key), project_key)).fetchone()
    if not proj: conn.close(); raise ValueError("project not found")
    ts = NOW()
    cur = conn.execute("UPDATE message_recipients SET ack_ts=? WHERE agent_name=? AND message_id=?",
                       (ts, agent_name, message_id))
    conn.commit(); conn.close()
    return {"message_id": message_id, "agent": agent_name, "acknowledged": ISO(ts), "updated": cur.rowcount}

@mcp.tool
def reserve_file_paths(project_key: str, agent_name: str, paths: List[str], ttl_seconds: int=3600,
                exclusive: bool=True, reason: str="") -> Dict[str, Any]:
    """
    Request claims (leases) on project-relative paths/globs. Returns conflicts if any.
    """
    proj = _project_get_or_create(project_key)
    slug = proj["slug"]
    _ensure_project_tree(slug)
    repo = _repo_for_project(slug)
    ppaths = _project_paths(slug)
    lock = FileLock(str(ppaths["root"] / ".mcp-mail.lock"))
    ts = NOW()
    exp = ts + max(60, ttl_seconds)

    conflicts = []
    to_insert = []
    with lock:
        conn = _db()
        try:
            # expire old leases
            conn.execute("UPDATE claims SET released_ts=? WHERE released_ts IS NULL AND expires_ts < ?", (ts, ts))
            for path in paths:
                # conflict if overlapping active claim exists and (exclusive OR theirs is exclusive)
                rows = conn.execute("""
                  SELECT agent_name, path, exclusive, expires_ts FROM claims
                  WHERE project_id=? AND released_ts IS NULL AND expires_ts > ? AND path=?
                """, (proj["id"], ts, path)).fetchall()
                if rows:
                    # if any existing claim is exclusive or we request exclusive, it's a conflict
                    if any(r["exclusive"] or exclusive for r in rows if r["agent_name"] != agent_name):
                        conflicts.append({"path": path, "holders": [dict(r) for r in rows]})
                        continue
                cid = f"clm_{uuid.uuid4().hex[:10]}"
                to_insert.append((cid, proj["id"], agent_name, path, int(exclusive), reason, ts, exp, None))
            for rec in to_insert:
                conn.execute("INSERT INTO claims(id,project_id,agent_name,path,exclusive,reason,created_ts,expires_ts,released_ts) VALUES(?,?,?,?,?,?,?,?,?)", rec)
            conn.commit()
        finally:
            conn.close()

        # write claim files and commit
        written = []
        for rec in to_insert:
            _, _, ag, path, ex, rsn, cts, ets, _ = rec
            payload = {
                "agent": ag, "path": path, "exclusive": bool(ex), "reason": rsn,
                "created": ISO(cts), "expires": ISO(ets)
            }
            h = hashlib.sha1(path.encode("utf-8")).hexdigest()
            claim_file = ppaths["claims"] / f"{h}.json"
            _write_json(claim_file, payload)
            written.append(str(claim_file.relative_to(ppaths["root"])))
        if written:
            _commit(repo, written, f"claim: {agent_name} {'exclusive' if exclusive else 'shared'} {len(written)} path(s)")
    return {"granted": [rec[3] for rec in to_insert], "conflicts": conflicts, "expires_ts": exp}

@mcp.tool
def release_claims(project_key: str, agent_name: str, paths: List[str]) -> Dict[str, Any]:
    """Release active claims held by the agent on the given paths."""
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug=? OR human_key=?", (_slug(project_key), project_key)).fetchone()
    if not proj: conn.close(); raise ValueError("project not found")
    ts = NOW()
    updated = 0
    for p in paths:
        cur = conn.execute("""
          UPDATE claims SET released_ts=?
          WHERE project_id=? AND agent_name=? AND path=? AND released_ts IS NULL
        """, (ts, proj["id"], agent_name, p))
        updated += cur.rowcount
    conn.commit(); conn.close()
    # we leave the JSON artifact in Git for audit
    return {"released": updated, "at": ISO(ts)}

@mcp.tool
def search_messages(project_key: str, query: str, limit: int=20) -> List[Dict[str, Any]]:
    """FTS search over subject and body."""
    conn = _db()
    proj = conn.execute("SELECT * FROM projects WHERE slug=? OR human_key=?", (_slug(project_key), project_key)).fetchone()
    if not proj: conn.close(); return []
    rows = conn.execute("""
      SELECT m.id, m.subject, m.from_agent, m.created_ts, m.importance
      FROM fts_messages fm JOIN messages m ON m.rowid = fm.rowid
      WHERE fm MATCH ? AND m.project_id=?
      ORDER BY m.created_ts DESC LIMIT ?
    """, (query, proj["id"], limit)).fetchall()
    conn.close()
    return [{"id": r["id"], "subject": r["subject"], "from": r["from_agent"], "created": ISO(r["created_ts"]), "importance": r["importance"]} for r in rows]

if __name__ == "__main__":
    # HTTP-only server at /mcp/
    mcp.run(transport="http", host=os.environ.get("HOST","0.0.0.0"), port=int(os.environ.get("PORT","8000")), path="/mcp/")

Run it

uv pip install -e .
python server.py
# server at http://localhost:8000/mcp/

FastMCP's HTTP deployment defaults to /mcp/; you can customize the path/host/port via the run() parameters as shown.
gofastmcp.com
Tooling surface (current MVP)

    create_agent(project_key, program, model, task_description, name_hint?)

    whois(project_key, agent_name)

    list_agents(project_key, active_only=True)

    send_message(project_key, from_agent, to[], subject, body_md, cc?, bcc?, importance="normal", ack_required=False, thread_id?)

    check_my_messages(project_key, agent_name, since_ts?, urgent_only=False, include_bodies=False, limit=20)

    acknowledge_message(project_key, agent_name, message_id)

    reserve_file_paths(project_key, agent_name, paths[], ttl_seconds=3600, exclusive=True, reason?)

    release_claims(project_key, agent_name, paths[])

    search_messages(project_key, query, limit=20)

All tools are asynchronous in usage (agents poll check_my_messages between steps). Consumers can call with FastMCP's client over Streamable HTTP.
gofastmcp.com
Design notes & extensions

    Images: The format supports either file references or inline base64 WebP (data:image/webp;base64,...). For automatic conversion you can add Pillow to ingest arbitrary images and emit compressed WebP.

    Auth: Add BearerAuth or OAuth via FastMCP auth helpers once you deploy remotely.
    gofastmcp.com

    CORS / Inspector: If you connect directly from a browser (e.g., MCP Inspector web), add CORS middleware to the http_app().
    gofastmcp.com

    Pre-commit guard: Provide a small hook that calls reserve_file_paths before commits; fail the commit if an exclusive conflicting lease exists (future work).

    Git integration with code repos: Today we keep a separate .mcp-mail repo per project; you can also embed under your code repo as a subdirectory or submodule if that's more convenient for humans reviewing the mail/claims alongside code.

    Richer directory: Track per‑agent "recent changes" by scanning your code repo logs where commits include Author: {AgentName} <agent@local> or commit trailers like Agent: GreenCastle. (FastMCP provides client APIs; your build pipeline could also stamp these.)
    GitPython Documentation

    Search UX: FTS5 is already wired; consider external‑content triggers for incremental reindexing (we used them) and per‑field boosts if needed.
    SQLite

    Deployment: If you prefer ASGI, replace run() with app = mcp.http_app(path="/mcp/") and run uvicorn server:app --host 0.0.0.0 --port 8000.
    gofastmcp.com

Why these choices are aligned with the ecosystem

    Streamable HTTP is the forward‑compatible, remote‑friendly transport; the spec documents how to keep optional compatibility with deprecated HTTP+SSE if you ever need it. We're not enabling SSE here.
    Model Context Protocol

    FastMCP 2.0's run(transport="http") and http_app() give you a clean path from local to production with auth, health checks, and mounting options.
    gofastmcp.com

    SQLite FTS5 gives efficient message search with minimal ops footprint.
    SQLite

If you want, I can extend this MVP with (a) inline image conversion to WebP, (b) git pre‑commit enforcement, (c) richer threading and conversation summaries, or (d) a resource layer (resource://inbox/{agent}?since=...) so clients can pull message bodies as MCP resources.


```