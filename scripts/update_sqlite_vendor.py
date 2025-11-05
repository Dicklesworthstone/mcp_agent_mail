from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

RELEASE_TEMPLATE = "https://github.com/sql-js/sql.js/releases/download/{tag}/{filename}"
DEFAULT_TAG = "v1.10.1"
FILES = {
    "sql-wasm.js": "sql-wasm.js",
    "sql-wasm.wasm": "sql-wasm.wasm",
}

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
VENDOR_DIR = REPO_ROOT / "src" / "mcp_agent_mail" / "viewer_assets" / "vendor"
MANIFEST_PATH = REPO_ROOT / "src" / "mcp_agent_mail" / "viewer_assets" / "vendor_manifest.json"


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Update sql.js vendor assets with pinned checksums.")
    parser.add_argument("tag", nargs="?", default=DEFAULT_TAG, help="sql.js release tag (e.g. v1.10.1)")
    parser.add_argument("--dry-run", action="store_true", help="Download assets and print checksums without writing.")
    args = parser.parse_args()

    tag = args.tag

    tmp_dir = Path(tempfile.mkdtemp(prefix="sqljs-assets-"))
    try:
        checksums: dict[str, str] = {}
        for local_name, release_name in FILES.items():
            url = RELEASE_TEMPLATE.format(tag=tag, filename=release_name)
            destination = tmp_dir / local_name
            print(f"Downloading {url} -> {destination}")
            _download(url, destination)
            checksums[local_name] = _sha256(destination)

        print("\nChecksums:")
        for name, digest in checksums.items():
            print(f"  {name}: {digest}")

        if args.dry_run:
            return 0

        for name, _digest in checksums.items():
            src = tmp_dir / name
            dst = VENDOR_DIR / name
            print(f"Copying {src} -> {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        manifest = {
            "sql_js": {
                "version": tag.lstrip("v"),
                "source": f"https://github.com/sql-js/sql.js/releases/tag/{tag}",
                "files": {name: {"sha256": digest} for name, digest in checksums.items()},
            }
        }
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Updated manifest at {MANIFEST_PATH}")
        print("Done.")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
