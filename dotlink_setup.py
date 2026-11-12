"""Prepare the built-in QQ runtime for DotLink (desktop QQ connection).

Kept as a CLI entry point; the implementation lives in :mod:`dotlink`, which also
starts the runtime, performs quick login and renders the QR inside the desktop
window (no WebUI browser page).
"""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("QQ_BRIDGE_HOME", Path(__file__).resolve().parent))


def main() -> int:
    from dotlink import Paths, prepare
    summary = prepare(Paths(ROOT))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not any("未找到 QQ 直连运行时" in item for item in summary.get("warnings", [])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
