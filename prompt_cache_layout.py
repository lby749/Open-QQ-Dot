"""Provider-neutral prompt layout for reusable, tenant-scoped cache prefixes.

Only move a real PROGRAM system block into the system role. User-controlled
persona/profile blocks remain user content, never privileged instructions.
Unrecognized prompts are passed through unchanged. No content is discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

# These blocks have stable content *within a friend's configuration* and belong
# before volatile retrieval/summary/conversation data. Keeping them in their
# original order avoids altering the existing prompt semantics.
_STABLE = frozenset({
    'PERSONA', 'PERSONA_TEMPLATE_STYLE', 'EMOTION_POLICY', 'TIMING_POLICY',
    'MEMORY_POLICY', 'FRIEND_PROFILE', 'FRIEND_NICKNAME',
})
_SECTION = re.compile(r'(?m)^【([A-Z][A-Z0-9_]*)】 SOURCE=([^\s]+) TYPE=(RULE|DATA)(?:\n|$)')


@dataclass(frozen=True)
class CacheLayout:
    system: str
    stable_user: str
    dynamic_user: str
    original: str

    @property
    def enabled(self) -> bool:
        return bool(self.system)

    def reconstructed(self) -> str:
        parts = [item for item in (self.system, self.stable_user, self.dynamic_user) if item]
        return '\n\n'.join(parts)


def split_prompt(prompt: str) -> CacheLayout:
    """Separate reusable prefix without elevating untrusted profile data.

    Only prompts rendered with the project's typed SYSTEM_RULES header are
    eligible. No broad regex against arbitrary user text or fallback summary
    prompts. Fail closed if structure differs from the expected layout.
    """
    fallback = CacheLayout('', '', prompt, prompt)
    if not isinstance(prompt, str) or not prompt.startswith('【SYSTEM_RULES】 SOURCE=program TYPE=RULE\n'):
        return fallback
    sections = list(_SECTION.finditer(prompt))
    if not sections or sections[0].start() != 0 or sections[0].group(1) != 'SYSTEM_RULES':
        return fallback
    # Require exactly two line breaks before every successive block.
    if any(prompt[m.start()-2:m.start()] != '\n\n' for m in sections[1:]):
        return fallback
    ends = [next_match.start() - 2 for next_match in sections[1:]] + [len(prompt)]
    blocks = [prompt[m.start():end] for m, end in zip(sections, ends)]
    stable_until = 1
    for sec in sections[1:]:
        if sec.group(1) not in _STABLE:
            break
        stable_until += 1
    system = blocks[0]
    stable_user = '\n\n'.join(blocks[1:stable_until])
    dynamic_user = '\n\n'.join(blocks[stable_until:])
    layout = CacheLayout(system, stable_user, dynamic_user, prompt)
    return layout if layout.reconstructed() == prompt else fallback


def official_host(base: str) -> str:
    parsed = urlparse(base)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        return ''
    return (parsed.hostname or '').lower()


def prefix_statistics(left: CacheLayout, right: CacheLayout) -> dict:
    """Offline **character** proxy, not a provider cache-hit/token claim."""
    def prefix(a, b):
        for index, pair in enumerate(zip(a, b)):
            if pair[0] != pair[1]:
                return index
        return min(len(a), len(b))
    return {
        'legacy_shared_chars': prefix(left.original, right.original),
        'stable_prefix_shared_chars': prefix(
            '\n\n'.join((left.system, left.stable_user)).strip(),
            '\n\n'.join((right.system, right.stable_user)).strip()),
        'cacheable_layout': left.enabled and right.enabled,
        'measurement': 'character_proxy_not_provider_cached_tokens',
    }
