"""Safe Release Packaging & Sensitive Information Scanner for Open QQ Dot.

Scans the codebase before packaging to prevent accidental leaks of:
- Personal friends database (friends.json, friends/)
- Private local access tokens (*.local.json)
- Subprocess redirection logs (dsh_out.txt, dsh_err.txt, bridge.log)
- Persistent runtime data (data/*.sqlite3)
- Hardcoded sensitive tokens or secrets
"""
from __future__ import annotations

import argparse
import hashlib
import fnmatch
import json
import os
import re
import sys
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# High-risk sensitive filenames and directories that MUST NEVER enter a release package
NOTICE_FILES = {"THIRD_PARTY_NOTICES.md"}

# Upstream public adversarial credential examples, not user secrets. Allow only
# this exact pinned corpus; edited or additional files still fail the scan.
# https://github.com/danielealbano/android-remote-control-mcp/blob/16f39717ce0969aa81a4ec132ba1cad861ba46cc/privacy-benchmark/src/main/resources/corpus_c.jsonl
PUBLIC_CREDENTIAL_FIXTURES = {
    'android/privacy-benchmark/src/main/resources/corpus_c.jsonl':
        '8276f1de054daceef0d9e78fca81fea7e0ebed6bac5ac7e034684377077e6945',
}

FORBIDDEN_PATTERNS = [
    "friends.json",
    "*.local.json",
    "dsh_*.txt",
    "bridge.log",
    "bridge.err.log",
    "*.sqlite",
    "*.sqlite3",
    "*.sqlite-*",
    "*.sqlite3-*", "*.db-*",
    "*.db",
    ".env",
    ".env.*",
    "*.bak",
    "*.pre-fix",
    "*.tmp",
    "*.key",
    "*.jks", "*.keystore", "keystore.properties", "local.properties",
    "*.pem", "*.p12", "*.pfx", "*.session", "auth.json", "credentials.json", "cookies.txt",
    ".*.source-nonce", ".source.lock", ".agent_mail_key*", ".experience_signing_key*", ".cockpit_admin_token",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", "id_rsa", "id_ed25519",
    "*.pid",
    "*.exe", "*.bin", "*.gguf", "*.pt", "*.pth", "*.safetensors",
    "*.log", "*.zip", "*.zip.tmp", "*.sha256",
    "*.lnk", "collect*.txt", "collect-final.txt", ".pytest_*",
    # internal handoff/feasibility notes are not public documentation
    "*交接*", "*验收*", "*可行性*", "*_report.md", "test_report.md",
    "handover.md", "unified_merge_notes.md",
    "AGENT_HANDOFF.md", "*接入核查*",
    "*续作核查*", "RELEASE_READINESS_*.md",
    "低成本模型实测与额度优化_20261025.md",
]

# Development material belongs in the public source archive, not in the
# installed application or the green runtime package.
APP_ONLY_DIRS = ("tests", "tools", "docs", "examples", ".github", "android", "mobile")

def _vendor_runtime_dir() -> str:
    try:
        value = json.loads((Path(__file__).resolve().parents[1] / "dotlink_vendor_defaults.json").read_text("utf-8"))
        return str(value.get("runtime_dir") or "qq-runtime").split("/")[0]
    except (OSError, ValueError):
        return "qq-runtime"


FORBIDDEN_DIRS = [
    ".gradle", ".kotlin", ".idea", "mobile",
    ".runtime", ".bootstrap", ".cache", "installation-backup",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "build",
    "/data",  # private application data at the project root; Android's data package is source
    "friends",
    _vendor_runtime_dir(),
    "qq-runtime",
    "dotlink",
    "capsules",
    "exports",
    "workspaces",
    "_backup_before_dsh_focus", "phone_app",
    ".git",
]


def load_gitignore_patterns(root: Path) -> list[str]:
    patterns = list(FORBIDDEN_PATTERNS)
    for d in FORBIDDEN_DIRS:
        patterns.append(f"{d}/")
    gitignore = root / ".gitignore"
    if gitignore.exists():
        for line in gitignore.read_text(encoding="utf-8").splitlines():
            l = line.strip()
            if l and not l.startswith("#"):
                patterns.append(l)
    return list(set(patterns))


def is_ignored(rel_path: str, patterns: list[str], app_only: bool = False) -> bool:
    if Path(rel_path).name in NOTICE_FILES:
        return False
    norm = rel_path.replace("\\", "/").strip("/").casefold()
    parts = norm.split("/")
    if app_only and any(part in APP_ONLY_DIRS for part in parts):
        return True

    for pat in patterns:
        p = pat.replace("\\", "/").strip("/").casefold()
        if pat.endswith("/"):
            dir_pat = p.rstrip("/")
            if pat.startswith("/"):
                if norm == dir_pat or norm.startswith(dir_pat + "/"):
                    return True
                continue
            if any(fnmatch.fnmatch(part, dir_pat) for part in parts):
                return True
            if fnmatch.fnmatch(norm, p) or fnmatch.fnmatch(norm + "/", p):
                return True
        else:
            if fnmatch.fnmatch(norm, p) or fnmatch.fnmatch(parts[-1], p):
                return True
            if any(fnmatch.fnmatch(part, p) for part in parts):
                return True
    return False


def scan_for_leaks(root: Path, file_list: list[Path], include_launcher=False) -> list[str]:
    """Scan file list for dangerous names or potential token leaks in content."""
    leaks = []
    secret_regex = re.compile(r"[\"']?(?:api[_-]?key|access_token|oauth_token|client_secret|bearer)[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{16,})[\"']", re.IGNORECASE)
    vendor_key = re.compile(r'(?<![\w-])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|nvapi-[A-Za-z0-9_-]{24,}|'
                            r'gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{30,})')
    synthetic = ('synthetic', 'example', 'fake', 'test', 'token_alice_', 'token_bob_', 'token_admin_', 'your_', 'placeholder')
    for f in file_list:
        rel = f.relative_to(root).as_posix()
        if f.is_symlink() or not f.resolve().is_relative_to(root.resolve()):
            leaks.append(f'[SYMLINK/OUTSIDE ROOT] {rel}')
            continue
        if rel in ('start.sh', 'Open QQ Dot.command') and b'\r' in f.read_bytes():
            leaks.append(f'[NON-PORTABLE SHELL] {rel}: use LF line endings')
        if include_launcher and rel == 'Open QQ Dot.exe':
            continue
        if f.name not in NOTICE_FILES and any(fnmatch.fnmatch(rel.lower(), pat.lower()) for pat in FORBIDDEN_PATTERNS):
            leaks.append(f'[FORBIDDEN FILE] {rel}')
            continue
        if any(rel.startswith(directory.lstrip('/') + '/') if directory.startswith('/')
               else directory in rel.split('/') for directory in FORBIDDEN_DIRS):
            leaks.append(f'[FORBIDDEN DIR] {rel}')
            continue
        if f.suffix.lower() in ('.json','.py','.pyw','.md','.txt','.html','.bat','.ps1','.yml','.yaml',
                               '.js','.jsx','.ts','.tsx','.mjs','.cjs','.toml','.ini','.cfg','.conf','.jsonl','.sh','.command',
                               '.kt','.kts','.java','.xml','.properties'):
            content=f.read_text(encoding='utf-8',errors='replace')
            values=secret_regex.findall(content)+vendor_key.findall(content)
            known_public_fixture = (rel in PUBLIC_CREDENTIAL_FIXTURES and
                hashlib.sha256(f.read_bytes().replace(b'\r\n', b'\n')).hexdigest() == PUBLIC_CREDENTIAL_FIXTURES[rel])
            if any(not value.lower().startswith(synthetic) for value in values) and not known_public_fixture:
                leaks.append(f'[POSSIBLE CREDENTIAL] {rel} (value redacted)')
            if re.search(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',content):
                leaks.append(f'[PRIVATE KEY] {rel}')

    return leaks


def create_clean_package(root: Path, output_zip: Path, include_launcher=False, app_only=False) -> tuple[int, float]:
    patterns = load_gitignore_patterns(root)
    included_files: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        # Prune ignored directories in-place
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()
                      and not getattr(Path(dirpath) / d, 'is_junction', lambda: False)()
                      and not is_ignored(f"{rel_dir}/{d}" if rel_dir != "." else d, patterns, app_only)]

        for f in filenames:
            rel_file = f"{rel_dir}/{f}" if rel_dir != "." else f
            if not is_ignored(rel_file, patterns, app_only):
                included_files.append(Path(dirpath) / f)

    if include_launcher and (root / 'Open QQ Dot.exe').is_file():
        included_files.append(root / 'Open QQ Dot.exe')
    # Pre-packaging security scan
    leaks = scan_for_leaks(root, included_files, include_launcher=include_launcher)
    if leaks:
        print("❌ SENSITIVE INFORMATION DETECTED! Packaging aborted:")
        for leak in leaks:
            print("  -", leak)
        raise SystemExit(1)

    # Package into clean zip
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = output_zip.with_suffix(".zip.tmp")

    count = 0
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in included_files:
            rel_path = f.relative_to(root).as_posix()
            archive_name=f"Open-QQ-Dot/{rel_path}"
            if rel_path in ('start.sh','Open QQ Dot.command'):
                info=zipfile.ZipInfo.from_file(f,archive_name)
                info.create_system=3
                info.external_attr=0o100755 << 16
                info.compress_type=zipfile.ZIP_DEFLATED
                zf.writestr(info,f.read_bytes())
            else:
                zf.write(f, arcname=archive_name)
            count += 1

    with zipfile.ZipFile(tmp_zip) as package:
        if package.testzip() is not None:
            raise RuntimeError('ZIP CRC verification failed')
        for f in included_files:
            if package.read('Open-QQ-Dot/'+f.relative_to(root).as_posix()) != f.read_bytes():
                raise RuntimeError('ZIP content mismatch')
    tmp_zip.replace(output_zip)
    digest=hashlib.sha256(output_zip.read_bytes()).hexdigest()
    output_zip.with_suffix('.zip.sha256').write_text(digest+'  '+output_zip.name+'\n',encoding='utf-8')

    size_mb = output_zip.stat().st_size / (1024 * 1024)
    return count, size_mb


def main():
    parser = argparse.ArgumentParser(description="Package Open QQ Dot cleanly for open-source release")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--output", default="../Open-QQ-Dot-round3-source.zip", help="Output ZIP path")
    parser.add_argument("--include-launcher", action="store_true", help="Include only the root Open QQ Dot.exe thin launcher")
    parser.add_argument("--check-only", action="store_true", help="Only scan without generating zip")
    parser.add_argument("--app-only", action="store_true",
                        help="Exclude tests/tools/docs/examples/.github for the installed application")
    args = parser.parse_args()
    app_only = bool(args.app_only)

    root = Path(args.root).resolve()
    out = Path(args.output).resolve()

    patterns = load_gitignore_patterns(root)
    candidate_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()
                      and not getattr(Path(dirpath) / d, 'is_junction', lambda: False)()
                      and not is_ignored(f"{rel_dir}/{d}" if rel_dir != "." else d, patterns, app_only)]
        for f in filenames:
            rel_file = f"{rel_dir}/{f}" if rel_dir != "." else f
            if not is_ignored(rel_file, patterns, app_only):
                candidate_files.append(Path(dirpath) / f)

    if args.include_launcher and (root / 'Open QQ Dot.exe').is_file():
        candidate_files.append(root / 'Open QQ Dot.exe')
    leaks = scan_for_leaks(root, candidate_files, include_launcher=args.include_launcher)
    if leaks:
        print(f"[FAIL] Security Scan FAILED! Found {len(leaks)} potential leak(s):")
        for l in leaks:
            print("  ", l)
        sys.exit(1)

    print(f"[PASS] Security Scan PASSED! {len(candidate_files)} candidate files; no supported secret pattern detected (not a complete security audit).")

    if not args.check_only:
        count, size_mb = create_clean_package(root, out, include_launcher=args.include_launcher, app_only=app_only)
        print(f"[DONE] Successfully created clean package: {out} ({count} files, {size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
