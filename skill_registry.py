"""Skills and plugins installed in the agents on this computer.

The desktop console uses the same catalogue for its ``/`` menu and its ``@``
mentions.  A skill selected with ``/name`` is injected into that one agent turn
(capped, so a large SKILL.md cannot flood the prompt); a folder or plugin
mentioned with ``@folder:...`` / ``@plugin:...`` is passed as a reference.
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
MAX_SKILL_CHARS = 6000
_CACHE: dict[str, tuple[float, object]] = {}

SKILL_ROOTS = (
    (HOME / ".codex" / "skills", "Codex"),
    (HOME / ".claude" / "skills", "Claude"),
    (HOME / ".gemini" / "antigravity" / "builtin" / "skills", "Antigravity"),
    (HOME / ".dsh" / "skills", "DSH"),
    (ROOT / "skills", "Dot"),
    (ROOT / ".skills", "Dot"),
)
PLUGIN_ROOTS = (
    (HOME / ".codex" / "plugins" / "cache", "Codex"),
    (HOME / ".codex" / "plugins", "Codex"),
    (HOME / ".claude" / "plugins", "Claude"),
    (HOME / ".gemini" / "antigravity" / "plugins", "Antigravity"),
)
BUILTIN_COMMANDS = {
    "clear", "new", "model", "engine", "effort", "status", "plan", "memory",
    "rename", "compact", "help", "task", "chat", "qq", "stop", "continue",
}


@dataclass(frozen=True)
class Skill:
    name: str
    slug: str
    description: str
    path: str
    provider: str


@dataclass(frozen=True)
class Plugin:
    name: str
    path: str
    provider: str


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip("'\"")
        if value:
            result[key.strip().lower()] = value
    return result


def _skill_at(folder: Path, provider: str) -> Skill | None:
    file = folder / "SKILL.md"
    if not file.is_file():
        return None
    try:
        head = file.read_text("utf-8", errors="replace")[:4000]
    except OSError:
        return None
    meta = _frontmatter(head)
    slug = (meta.get("name") or folder.name).strip()
    description = meta.get("description") or f"{provider} skill"
    return Skill(name=slug, slug=folder.name, description=description[:240],
                 path=str(file), provider=provider)


def list_skills() -> list[Skill]:
    cached = _CACHE.get('skills')
    if cached and time.monotonic() - cached[0] < 8:
        return cached[1]
    found: dict[str, Skill] = {}
    for root, provider in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            skill = _skill_at(folder, provider)
            if skill is not None:
                found.setdefault(f"{provider}:{skill.slug}", skill)
    result = sorted(found.values(), key=lambda s: (s.provider, s.name.casefold()))
    _CACHE['skills'] = (time.monotonic(), result)
    return result


def find_skill(name: str) -> Skill | None:
    wanted = name.strip().lstrip("/").casefold()
    for skill in list_skills():
        if wanted in {skill.slug.casefold(), skill.name.casefold()}:
            return skill
    return None


def skill_body(skill: Skill, limit: int = MAX_SKILL_CHARS) -> str:
    try:
        text = Path(skill.path).read_text("utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
    text = text.strip()
    return text[:limit] + ("\n[skill truncated]" if len(text) > limit else "")


def list_plugins() -> list[Plugin]:
    cached = _CACHE.get('plugins')
    if cached and time.monotonic() - cached[0] < 8:
        return cached[1]
    found: dict[str, Plugin] = {}
    for root, provider in PLUGIN_ROOTS:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.name.startswith(".") or folder.name in {"cache", "bin", "lib", "brain", "scratch"}:
                continue
            found.setdefault(f"{provider}:{folder.name}", Plugin(folder.name, str(folder), provider))
    result = sorted(found.values(), key=lambda p: (p.provider, p.name.casefold()))
    _CACHE['plugins'] = (time.monotonic(), result)
    return result


def expand(text: str) -> str:
    """Inject one leading /skill and any @folder/@plugin mentions into a turn."""
    value = str(text or "")
    match = re.match(r"^\s*/([A-Za-z0-9_.-]+)\b\s*", value)
    if match and match.group(1).casefold() not in BUILTIN_COMMANDS:
        skill = find_skill(match.group(1))
        if skill is not None:
            body = skill_body(skill)
            if body:
                value = f"[SKILL /{match.group(1)} · {skill.provider}]\n{body}\n[/SKILL]\n\n{value[match.end():]}"
    notes = []
    for path in re.findall(r"@folder:(\"[^\"]+\"|\S+)", value):
        path = path.strip('"')
        folder = Path(path).expanduser()
        notes.append(f"[REFERENCED_FOLDER] {folder}" + ("" if folder.exists() else " (not found)"))
    for name in re.findall(r"@plugin:([A-Za-z0-9_.-]+)", value):
        plugin = next((item for item in list_plugins() if item.name.casefold() == name.casefold()), None)
        notes.append(f"[REFERENCED_PLUGIN] {name}" + (f" ({plugin.provider}: {plugin.path})" if plugin else " (not found)"))
    if notes:
        value = "\n".join(notes) + "\n\n" + value
    return value


def install(source: Path, providers: list[str] | None = None) -> list[Path]:
    """Copy one SKILL.md folder into the selected agents' skill directories."""
    source = Path(source).expanduser()
    if source.is_file() and source.name.casefold() == 'skill.md':
        source = source.parent
    if not (source / 'SKILL.md').is_file():
        raise ValueError('选择的文件夹里没有 SKILL.md')
    meta = _frontmatter((source / 'SKILL.md').read_text('utf-8', errors='replace')[:4000])
    slug = re.sub(r'[^A-Za-z0-9_.-]+', '-', (meta.get('name') or source.name)).strip('-') or 'skill'
    roots = {'codex': HOME / '.codex' / 'skills', 'claude': HOME / '.claude' / 'skills',
             'antigravity': HOME / '.gemini' / 'antigravity' / 'builtin' / 'skills'}
    chosen = [roots[p] for p in (providers or ['codex']) if p in roots]
    if not chosen:
        chosen = [ROOT / 'skills']
    installed = []
    for root in chosen:
        root.mkdir(parents=True, exist_ok=True)
        target = root / slug
        if target.exists():
            raise ValueError(f'目标技能已存在：{target}')
        shutil.copytree(source, target)
        installed.append(target)
    return installed
