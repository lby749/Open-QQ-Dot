"""CLI: export QQ chat history to portable JSON (DotLink direct or local QQ export)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qq_history_export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
