"""Deterministic speaker->direction mapping and stratified gold sampling.

Direction policy (chosen by the user, not inferred)
---------------------------------------------------
Neither corpus carries a real in/out direction. Inventing "the main speaker is
the friend" would fabricate a relationship that is not in the data, so instead:

* exactly two distinct speakers in a conversation -> the speaker whose first
  utterance comes first is ``in``, the other is ``out``, held constant for the
  whole conversation;
* three or more speakers -> ``direction=None``; a binary flag cannot represent
  the turn structure without loss, so it is not forced. Those items are tagged
  and can be run as a separate study.

The value describes speaker identity / turn structure only. ``synthetic`` says
whether it was derived by the two-speaker rule, and it must never be read as a
real send/receive direction. The ablation runs every item with
``direction=None`` under otherwise identical conditions.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

DIRECTION_IN = "in"
DIRECTION_OUT = "out"

# MPDD utterances are TV-script lines and many mix spoken dialogue with
# third-person stage narration, for example
#     "I... I didn't drink too much. Who... who says I'm drunk, I'll fight them."
#     "Can't even speak clearly, and you still claim you aren't drunk?" Struggling to support Guo Zhangjian to sit on the sofa.
# The narration is not speech, so it is a genuine confound for tone/intent. It is
# detected and counted, never silently removed.
_NARRATION = re.compile(
    r'^\s*["\u201c\u300c][^"\u201d\u300d]{0,400}["\u201d\u300d]\s*(?P<tail>.*)$', re.S)


def narration_tail(text: str) -> str:
    """Text that follows a leading quoted line, empty when there is none."""
    match = _NARRATION.match(text or "")
    if not match:
        return ""
    return (match.group("tail") or "").strip()


def has_narration(text: str) -> bool:
    """Whether an utterance carries narration outside its quoted speech."""
    return bool(narration_tail(text))

# How an item's direction was obtained.
SYNTHETIC = "synthetic_speaker_map"
UNAVAILABLE = "unavailable_multi_speaker"
ABLATION = "ablation_null"


@dataclass(frozen=True)
class DirectionMap:
    """Per-conversation speaker ordering and the direction it implies."""

    conversation_id: str
    order: tuple[str, ...]        # speakers in first-appearance order
    mapping: dict                 # speaker -> in/out, empty when not binary
    synthetic: bool

    def direction_for(self, speaker: str) -> str | None:
        return self.mapping.get(speaker)

    @property
    def binary(self) -> bool:
        return bool(self.mapping)


def speaker_order(items) -> dict[str, tuple[str, ...]]:
    """First-appearance speaker order per conversation, in corpus order."""
    order: dict[str, list[str]] = {}
    for item in items:
        seen = order.setdefault(item.conversation_id, [])
        if item.speaker_id not in seen:
            seen.append(item.speaker_id)
    return {key: tuple(value) for key, value in order.items()}


def build_direction_maps(items, max_speakers: int = 2) -> dict[str, DirectionMap]:
    """Derive a direction map per conversation under the two-speaker rule."""
    maps: dict[str, DirectionMap] = {}
    for conversation_id, speakers in speaker_order(items).items():
        if len(speakers) <= max_speakers and len(speakers) == 2:
            mapping = {speakers[0]: DIRECTION_IN, speakers[1]: DIRECTION_OUT}
            maps[conversation_id] = DirectionMap(conversation_id, speakers, mapping, True)
        else:
            maps[conversation_id] = DirectionMap(conversation_id, speakers, {}, False)
    return maps


def apply_direction(item, direction_map: DirectionMap, ablate: bool = False):
    """Attach direction metadata to an utterance as a new frozen record.

    ``ablate=True`` keeps every other field identical but sets direction to
    None, which is the null-direction comparison arm.
    """
    narration = has_narration(item.text)
    tail = narration_tail(item.text)
    if ablate:
        return replace(item, metadata=dict(
            item.metadata,
            direction=None,
            direction_policy=ABLATION,
            synthetic_direction=False,
            speaker_count=len(direction_map.order),
            narration_after_quote=narration,
            narration_tail=tail[:200],
        ))
    value = direction_map.direction_for(item.speaker_id)
    return replace(item, metadata=dict(
        item.metadata,
        direction=value,
        direction_policy=SYNTHETIC if direction_map.binary else UNAVAILABLE,
        synthetic_direction=direction_map.binary,
        speaker_count=len(direction_map.order),
        narration_after_quote=narration,
        narration_tail=tail[:200],
    ))


def direction_for_context(items, direction_maps) -> list:
    """Return copies whose context rows carry their own speaker's direction.

    A context row belongs to a different speaker, so it must be given that
    speaker's direction rather than the target speaker's. Handles previous and
    following rows alike, since a following row can also be another speaker.
    Utterance is frozen, so new records are returned instead of mutating.
    """
    out = []
    for item in items:
        mapping = direction_maps.get(item.conversation_id)
        following = item.metadata.get("following")
        if mapping is None:
            out.append(item)
            continue
        context = tuple(
            {**entry, "direction": mapping.direction_for(entry.get("speaker"))}
            for entry in item.context)
        metadata = item.metadata
        if following:
            metadata = dict(metadata, following=[
                {**entry, "direction": mapping.direction_for(entry.get("speaker"))}
                for entry in following])
        out.append(replace(item, context=context, metadata=metadata))
    return out


def attach_following(selected, corpus) -> list:
    """Return copies carrying the next utterance of the same conversation.

    The project's window has a real following turn, and CPED/MPDD both have one
    in corpus order, so it is supplied rather than left empty. Truncation and
    the "at most one" rule are applied by project_contract, not here.
    """
    by_conversation: dict[str, list] = {}
    for item in corpus:
        by_conversation.setdefault(item.conversation_id, []).append(item)
    for group in by_conversation.values():
        group.sort(key=lambda entry: entry.message_seq)
    position = {entry.annotation_id: index
                for group in by_conversation.values()
                for index, entry in enumerate(group)}
    out = []
    for item in selected:
        group = by_conversation.get(item.conversation_id, [])
        index = position.get(item.annotation_id, -1)
        following = []
        if 0 <= index < len(group) - 1:
            nxt = group[index + 1]
            following = [{"speaker": nxt.speaker_id, "text": nxt.text,
                          "seq": nxt.message_seq}]
        out.append(replace(item, metadata=dict(item.metadata, following=following)))
    return out


# --------------------------------------------------------------------------- #
# Deterministic stratified sampling
# --------------------------------------------------------------------------- #
def _rank(item) -> str:
    """Stable per-item ordering key, independent of file order and of Python's
    hash randomisation."""
    return hashlib.sha256(f"{item.data_source}|{item.annotation_id}".encode()).hexdigest()


@dataclass(frozen=True)
class SampleSpec:
    n: int = 100
    min_speakers: int = 2
    max_speakers: int = 2
    stratify_by: str = "gold_key"


def sample_items(items, direction_maps, spec: SampleSpec) -> tuple[list, list]:
    """Stratified, deterministic sample plus the items the filter excluded.

    Stratification spreads the sample across the corpus instead of taking one
    contiguous block, and uses a stable hash order so the same inputs always
    produce the same sample. Returns ``(selected, excluded)``.
    """
    eligible, excluded = [], []
    for item in items:
        mapping = direction_maps.get(item.conversation_id)
        count = len(mapping.order) if mapping else 0
        if spec.min_speakers <= count <= spec.max_speakers:
            eligible.append(item)
        else:
            excluded.append(item)

    # Group by the stratification key, then round-robin so no stratum dominates.
    groups: dict[str, list] = {}
    for item in eligible:
        value = item.gold.get(spec.stratify_by)
        key = str(value) if value not in (None, "") else "unknown"
        groups.setdefault(key, []).append(item)
    for group in groups.values():
        group.sort(key=_rank)

    chosen: list = []
    keys = sorted(groups)
    cursor = 0
    while len(chosen) < spec.n and any(groups[key] for key in keys):
        key = keys[cursor % len(keys)]
        if groups[key]:
            chosen.append(groups[key].pop(0))
        cursor += 1
    chosen.sort(key=_rank)
    return chosen, excluded


def sample_summary(items, direction_maps) -> dict:
    """Counts that show how much of a corpus survives the two-speaker filter."""
    conversations: dict[str, set] = {}
    for item in items:
        conversations.setdefault(item.conversation_id, set()).add(item.speaker_id)
    sizes: dict[int, int] = {}
    for speakers in conversations.values():
        sizes[len(speakers)] = sizes.get(len(speakers), 0) + 1
    narrated = sum(1 for item in items if has_narration(item.text))
    return {"utterances": len(items), "conversations": len(conversations),
            "conversation_speaker_counts": dict(sorted(sizes.items())),
            "two_speaker_conversations": sizes.get(2, 0),
            "utterances_with_narration_after_quote": narrated,
            "narration_note": ("MPDD mixes spoken lines with stage narration; flagged, "
                               "not removed") if narrated else None}


def write_manifest(path, payload) -> None:
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
