"""Verify a built Open QQ Dot zip contains no private data or model weights."""
from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

FORBIDDEN_PARTS = {"/friends/", "/workspaces/", "/qq-runtime/", "/state/",
                   "/docs/", "/.git/", "/__pycache__/", "/.pytest"}
MODEL_SUFFIXES = (".gguf", ".onnx", ".safetensors", ".pt", ".pth", ".bin")
SECRET_NAMES = ("friends.json", ".env", "id_rsa", "auth.json", "cookies.txt",
                "local.json", ".source-nonce", ".source.lock")


def check(path: Path, *, source: bool = False) -> list[str]:
    errors = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if not any(name.endswith("/LICENSE") or name == "LICENSE" for name in names):
            errors.append("no LICENSE file")
        if not any("NapCat-LICENSE" in name for name in names):
            errors.append("no NapCat license")
        for name in names:
            folded = "/" + name.casefold().lstrip("/")
            relative = name.casefold().removeprefix('open-qq-dot/').lstrip('/')
            if relative.startswith('data/') or any(part in folded for part in FORBIDDEN_PARTS if not (source and part == '/docs/')):
                errors.append("forbidden path: " + name)
            if Path(name).name.casefold() == 'agent_handoff.md' or '接入核查' in Path(name).name:
                errors.append('internal handoff document: ' + name)
            if name.casefold().endswith(MODEL_SUFFIXES):
                errors.append("model file: " + name)
            if Path(name).name.casefold() in SECRET_NAMES:
                errors.append("private file: " + name)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='store_true', help='源码包允许公开文档；仍检查私人数据、模型与内部交接文档')
    parser.add_argument("archives", nargs="+")
    args = parser.parse_args()
    failed = False
    for name in args.archives:
        path = Path(name)
        errors = check(path, source=args.source)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{path.name}: {path.stat().st_size / 1024 ** 2:.1f} MB sha256={digest}")
        if errors:
            failed = True
            for error in errors[:20]:
                print("  FAIL", error)
        else:
            print("  PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
