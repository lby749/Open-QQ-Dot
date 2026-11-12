"""Download the two public dialogue datasets into a chosen data root.

Fetches MPDD (multi-party Chinese dialogue with emotion/relation labels) and
CPED (Chinese personalized/emotional dialogue) and verifies SHA256 digests.

Usage:
    python fetch_datasets.py [--data-root E:\\qwen35-nlp\\data]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

MPDD_ZIP_URL = "https://raw.githubusercontent.com/ntunlplab/Dialogue-MPDD/main/mpdd.zip"
CPED_BASE = "https://raw.githubusercontent.com/scutcyr/CPED/main/data/CPED"
CPED_FILES = ("train_split.csv", "valid_split.csv", "test_split.csv", "speakers.txt")
CPED_REPO = "https://github.com/scutcyr/CPED"
MPDD_REPO = "https://github.com/ntunlplab/Dialogue-MPDD"


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def fetch(url: str, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        print(f"  downloading {target.name} ...", flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": "dataset-fetch/1"})
        with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as out:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                out.write(block)
    size = target.stat().st_size
    if size == 0:
        raise RuntimeError(f"{target.name} downloaded empty")
    print(f"  ok {target.name} ({size / 1024:.1f} KiB)", flush=True)
    return {"name": target.name, "bytes": size, "sha256": digest(target), "url": url}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=r"E:\qwen35-nlp\data")
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    manifest = {"data_root": str(root), "sources": {
        "mpdd": {"repository": MPDD_REPO, "homepage": MPDD_REPO,
                 "citation": "Yi-Ting Chen, Hen-Hsen Huang, Hsin-Hsi Chen. LREC 2020."},
        "cped": {"repository": CPED_REPO, "homepage": CPED_REPO,
                 "citation": "Yirong Chen et al. arXiv:2205.14727"}},
        "files": []}

    print("MPDD:", flush=True)
    mpdd_zip = root / "mpdd" / "mpdd.zip"
    manifest["files"].append(fetch(MPDD_ZIP_URL, mpdd_zip))
    extract_to = root / "mpdd"
    with zipfile.ZipFile(mpdd_zip) as archive:
        names = archive.namelist()
        print(f"  zip contains: {names}", flush=True)
        for info in archive.infolist():
            if info.is_dir():
                continue
            # Keep the archive's own names; the reader searches recursively.
            archive.extract(info, extract_to)
    manifest["mpdd_zip_entries"] = names

    print("CPED:", flush=True)
    for name in CPED_FILES:
        manifest["files"].append(fetch(f"{CPED_BASE}/{name}", root / "cped" / name))

    manifest_path = root / "datasets-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest -> {manifest_path}", flush=True)

    found = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    print("files under data root:", flush=True)
    for item in found:
        print("  " + item, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
