"""Bounds for host secrets that must never be exposed to friend-scoped tools.

This is defense in depth, not a sandbox: an agent running as the same Windows
user with command access can still use arbitrary code. The real boundary is a
separate low-privilege OS account.
"""
from pathlib import Path

PROTECTED_NAMES = {
    "house_web.local.json", "friends.json", "friend.json",
    "api-key.bin", "deepseek-key.bin", ".agent_mail_key_v1",
    ".env", "credentials", "secrets",
}
PROTECTED_DIRS = {".ssh", ".aws", ".gnupg"}
PROTECTED_TERMS = tuple(sorted(PROTECTED_NAMES | PROTECTED_DIRS))


def is_protected_path(path) -> bool:
    resolved = Path(path).resolve()
    name = resolved.name.casefold()
    if name in {item.casefold() for item in PROTECTED_NAMES}:
        return True
    if name.startswith("api-") and name.endswith("-key.bin"):
        return True
    return bool(PROTECTED_DIRS.intersection({part.casefold() for part in resolved.parts}))


def command_touches_secret(command: str) -> bool:
    lower = str(command or "").casefold()
    return any(term.casefold() in lower for term in PROTECTED_TERMS)
