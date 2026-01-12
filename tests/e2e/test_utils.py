from __future__ import annotations

from tests.e2e.utils import diff_payload, normalize_output


def test_diff_payload_reports_mismatch() -> None:
    expected = {"a": 1, "b": [1, 2], "c": {"d": "x"}}
    actual = {"a": 2, "b": [1], "c": {"d": "y"}, "e": 3}
    diffs = diff_payload(expected, actual)
    assert any("a" in diff for diff in diffs)
    assert any("b" in diff for diff in diffs)
    assert any("c" in diff for diff in diffs)
    assert any("e" in diff for diff in diffs)


def test_normalize_output_replacements_and_sort() -> None:
    payload = {
        "paths": ["/tmp/run/file.txt", "/tmp/run/other.txt"],
        "agents": [{"name": "Zed"}, {"name": "Alpha"}],
    }
    normalized = normalize_output(payload, [("/tmp/run", "<tmp>")])
    assert normalized["paths"][0].startswith("<tmp>")
    assert [agent["name"] for agent in normalized["agents"]] == ["Alpha", "Zed"]
