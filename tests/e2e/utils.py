from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


@dataclass(frozen=True)
class E2EPaths:
    root: Path
    golden_dir: Path
    log_dir: Path


def get_e2e_paths() -> E2EPaths:
    root = Path(__file__).resolve().parent
    return E2EPaths(
        root=root,
        golden_dir=root / "golden",
        log_dir=root / "logs",
    )


def make_console() -> Console:
    return Console(force_terminal=True, color_system="truecolor", width=120, record=True)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default),
        encoding="utf-8",
    )


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if hasattr(value, "dict") and callable(getattr(value, "dict", None)) and not isinstance(value, dict):
        return value.dict()
    if hasattr(value, "root"):
        return value.root
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _apply_replacements(text: str, replacements: Iterable[tuple[str, str]]) -> str:
    out = text
    for src, token in replacements:
        if src:
            out = out.replace(src, token)
    return out


def _sort_known_lists(key: str | None, items: list[Any]) -> list[Any]:
    if not items:
        return items
    if all(isinstance(item, dict) for item in items):
        if key in {"messages", "items", "deliveries"}:
            return sorted(items, key=lambda x: (x.get("created_ts") or "", x.get("id") or 0))
        if key in {"agents", "projects"}:
            return sorted(items, key=lambda x: (x.get("name") or "", x.get("slug") or ""))
        if key in {"to", "cc", "bcc"}:
            return sorted(items, key=lambda x: str(x))
        if key in {"file_reservations"}:
            return sorted(items, key=lambda x: (x.get("path_pattern") or "", x.get("id") or 0))
        if "id" in items[0]:
            return sorted(items, key=lambda x: (x.get("id") or 0))
        if "name" in items[0]:
            return sorted(items, key=lambda x: (x.get("name") or ""))
    return items


def normalize_output(payload: Any, replacements: Iterable[tuple[str, str]], *, parent_key: str | None = None) -> Any:
    if is_dataclass(payload):
        return normalize_output(asdict(payload), replacements, parent_key=parent_key)
    if hasattr(payload, "model_dump"):
        try:
            dumped = payload.model_dump(mode="json")
        except TypeError:
            dumped = payload.model_dump()
        return normalize_output(dumped, replacements, parent_key=parent_key)
    if hasattr(payload, "root"):
        return normalize_output(payload.root, replacements, parent_key=parent_key)
    if hasattr(payload, "dict") and callable(getattr(payload, "dict", None)) and not isinstance(payload, dict):
        return normalize_output(payload.dict(), replacements, parent_key=parent_key)
    if isinstance(payload, Path):
        return _apply_replacements(str(payload), replacements)
    if isinstance(payload, datetime):
        return payload.isoformat()
    if isinstance(payload, dict):
        return {
            key: normalize_output(value, replacements, parent_key=key)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        normalized = [normalize_output(item, replacements, parent_key=parent_key) for item in payload]
        return _sort_known_lists(parent_key, normalized)
    if isinstance(payload, str):
        return _apply_replacements(payload, replacements)
    return payload


def diff_payload(expected: Any, actual: Any, *, path: str = "") -> list[str]:
    diffs: list[str] = []
    if type(expected) is not type(actual):
        diffs.append(f"{path or '<root>'}: type {type(expected).__name__} != {type(actual).__name__}")
        return diffs
    if isinstance(expected, dict):
        expected_keys = set(expected.keys())
        actual_keys = set(actual.keys())
        for key in sorted(expected_keys - actual_keys):
            diffs.append(f"{path}.{key}: missing in actual")
        for key in sorted(actual_keys - expected_keys):
            diffs.append(f"{path}.{key}: unexpected in actual")
        for key in sorted(expected_keys & actual_keys):
            diffs.extend(diff_payload(expected[key], actual[key], path=f"{path}.{key}" if path else key))
        return diffs
    if isinstance(expected, list):
        if len(expected) != len(actual):
            diffs.append(f"{path or '<root>'}: length {len(expected)} != {len(actual)}")
        for idx, (exp_item, act_item) in enumerate(zip(expected, actual, strict=False)):
            diffs.extend(diff_payload(exp_item, act_item, path=f"{path}[{idx}]"))
        return diffs
    if expected != actual:
        diffs.append(f"{path or '<root>'}: {expected!r} != {actual!r}")
    return diffs


def render_phase(console: Console, title: str, details: dict[str, Any]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in details.items():
        table.add_row(str(key), json.dumps(value, ensure_ascii=True)[:2000])
    console.print(Panel(table, title=title, expand=False))


def assert_matches_golden(
    name: str,
    actual: Any,
    *,
    console: Console,
    replacements: Iterable[tuple[str, str]],
    update: bool,
) -> None:
    paths = get_e2e_paths()
    golden_path = paths.golden_dir / f"{name}.json"
    normalized = normalize_output(actual, replacements)
    if update or not golden_path.exists():
        write_json(golden_path, normalized)
        console.print(Panel(f"Wrote golden file {golden_path}", title="golden update", expand=False))
        return
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    diffs = diff_payload(expected, normalized)
    if diffs:
        diff_text = "\n".join(diffs[:200])
        console.print(Panel(diff_text, title="golden mismatch", expand=False))
        raise AssertionError(f"Golden mismatch for {name}: {len(diffs)} differences")


def write_log(name: str, payload: dict[str, Any]) -> Path:
    paths = get_e2e_paths()
    log_path = paths.log_dir / f"{name}.json"
    write_json(log_path, payload)
    return log_path
