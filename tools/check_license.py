"""Fail CI if the project license or required GPL notices drift."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    errors = []
    if "GNU GENERAL PUBLIC LICENSE" not in license_text or "Version 3" not in license_text:
        errors.append("LICENSE does not contain GPLv3 text")
    if "GPL-3.0-or-later" not in license_text:
        errors.append("LICENSE lacks SPDX GPL-3.0-or-later")
    if "GPL-3.0-or-later" not in notices:
        errors.append("THIRD_PARTY_NOTICES.md does not state GPL-3.0-or-later")
    if "MIT License" in readme and "GPL-3.0-or-later" not in readme:
        errors.append("README still claims MIT licensing")
    for required in ("PySide6-Fluent-Widgets-LICENSE", "PySide6_Essentials", "NapCat-LICENSE"):
        if not (ROOT / "licenses" / "third-party" / required).exists():
            errors.append(f"missing third-party notice: {required}")
    if errors:
        print("license check failed:")
        for item in errors:
            print(" -", item)
        return 1
    print("license check passed: GPL-3.0-or-later with third-party notices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
