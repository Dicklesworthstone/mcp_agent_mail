import os
from pathlib import Path

import pytest

from mcp_agent_mail.app import _resolve_project_identity


def _is_wsl2() -> bool:
    # Heuristic: WSL_INTEROP env var present or /proc/version contains Microsoft
    try:
        if os.environ.get("WSL_INTEROP"):
            return True
        from pathlib import Path as _Path
        with _Path("/proc/version").open("r", encoding="utf-8", errors="ignore") as fh:
            data = fh.read().lower()
        return "microsoft" in data
    except Exception:
        return False


@pytest.mark.skipif(not _is_wsl2(), reason="WSL2-specific test")
def test_wsl2_path_normalization(tmp_path: Path) -> None:
    # On WSL2, ensure canonical_path is a resolved POSIX path under the Linux tree
    target = tmp_path / "proj"
    target.mkdir(parents=True, exist_ok=True)
    ident = _resolve_project_identity(str(target))
    assert Path(ident["canonical_path"]).exists()
    # Ensure we didn't accidentally produce a Windows-style drive path
    assert not str(ident["canonical_path"]).startswith(("C:\\", "D:\\")), "Expected POSIX path on WSL2"

