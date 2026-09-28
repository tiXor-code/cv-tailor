#!/usr/bin/env python3
"""Zip extension/ into an installable bundle (default: ~/clawd/apps/admin/public/
scout-extension.zip, served from admin.teodorlutoiu.com/scout/extension).

Chrome installs it via chrome://extensions -> Developer mode -> Load unpacked,
pointed at the unzipped folder. No key or personal data is inside the bundle:
the extension key is pasted into its settings page and stays in that browser.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "extension"
DEFAULT_OUT = Path.home() / "clawd/apps/admin/public/scout-extension.zip"


def build(out: Path = DEFAULT_OUT) -> Path:
    manifest = json.loads((SRC / "manifest.json").read_text())
    # A test build adds 127.0.0.1; the shipped bundle never may.
    assert not any("127.0.0.1" in m for m in manifest["host_permissions"]), "test permissions in manifest"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        # subfolders too (icons/): a manifest icon missing from the zip stops Chrome loading it
        for f in sorted(SRC.rglob("*")):
            rel = f.relative_to(SRC)
            if f.is_file() and not any(part.startswith(".") for part in rel.parts):
                z.write(f, f"scout-fill/{rel.as_posix()}")
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT))
