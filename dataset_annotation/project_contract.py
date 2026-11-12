"""The project's own four-label classification contract, mirrored for plan A.

Plan A asks whether the project's *own* classifier task works on CPED/MPDD text,
so the task definition must be the project's, not one invented here. This module
reproduces that contract exactly and pins it:

* the four labels and their enums come from ``message_classifier.ENUMS``;
* the system prompt is ``message_classifier.SYSTEM`` verbatim;
* the review reasons are ``message_classifier.REVIEW_REASONS``;
* the generation parameters are ``message_classifier.GENERATION``;
* the request/validation shape is ``message_classifier.SHAPE`` as recorded here.

``contract_fingerprint()`` hashes the live project module. A run stores that
fingerprint, so if the project ever edits its prompt or enums, an old run is
visibly not comparable instead of silently mismatched.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from message_classifier import (  # noqa: E402  - deliberate reuse of the project contract
    ENUMS, REVIEW_REASONS, SYSTEM,
)
try:
    from message_classifier import GENERATION
except ImportError:
    GENERATION = {"temperature": 0, "top_k": 1, "top_p": 1, "seed": 42, "stream": False}

# Mirrors message_classifier.DETAIL_SCHEMA, including the nested "labels" object
# and the output-token ceiling the project uses for this schema.
MAX_TOKENS = 180
LABEL_FIELDS = ("intent", "emotion", "tone", "initiative")
CONTEXT_TURNS = 3
CONTEXT_CHARS = 160
CURRENT_CHARS = 800
FOLLOWING_TURNS = 1
SLOTS = 4
CONTEXT_PER_SLOT = 4096


def detail_schema() -> dict:
    label_schema = {"type": "object",
                    "properties": {key: {"type": "string", "enum": list(values)}
                                   for key, values in ENUMS.items()},
                    "required": list(ENUMS), "additionalProperties": False}
    return {"type": "object",
            "properties": {"labels": label_schema,
                           "needs_review": {"type": "boolean"},
                           "reason": {"type": "string", "enum": list(REVIEW_REASONS)}},
            "required": ["labels", "needs_review", "reason"],
            "additionalProperties": False}


def contract_fingerprint() -> str:
    """Hash the live project contract so a run records which one it used."""
    payload = json.dumps({"system": SYSTEM, "enums": ENUMS,
                          "reasons": list(REVIEW_REASONS),
                          "generation": GENERATION, "schema": detail_schema()},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_detail(value) -> dict:
    """Same validation the project performs on a model reply."""
    if not isinstance(value, dict) or set(value) != {"labels", "needs_review", "reason"}:
        raise ValueError("reply is not a {labels, needs_review, reason} object")
    labels = value["labels"]
    if not isinstance(labels, dict) or set(labels) != set(ENUMS):
        raise ValueError("labels object has the wrong fields")
    for key, allowed in ENUMS.items():
        if labels[key] not in allowed:
            raise ValueError(f"label {key}={labels[key]!r} is outside the enum")
    if type(value["needs_review"]) is not bool:
        raise ValueError("needs_review must be a boolean")
    if value["reason"] not in REVIEW_REASONS:
        raise ValueError("reason is outside the enum")
    if value["needs_review"] == (value["reason"] == "clear"):
        raise ValueError("needs_review and reason are inconsistent")
    return {"labels": dict(labels), "needs_review": value["needs_review"],
            "reason": value["reason"]}


def build_messages(text, direction=None, context=(), following=()):
    """Build the project's exact request messages and window.

    Truncation matches ``LocalMessageClassifier.classify_with_review``: the last
    three previous turns, each capped at 160 characters, the current message at
    800, and at most the first actual following turn.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a nonempty string")
    previous, next_messages = [], []
    for entries, output in ((list(context)[-CONTEXT_TURNS:], previous),
                            (list(following)[:FOLLOWING_TURNS], next_messages)):
        for entry in entries:
            if not isinstance(entry, (str, dict)):
                raise ValueError("context entries must be strings or dicts")
            item = entry if isinstance(entry, str) else entry.get("text", entry.get("content", ""))
            if not isinstance(item, str):
                raise ValueError("context text must be a string")
            output.append({"text": item[:CONTEXT_CHARS],
                           "direction": None if isinstance(entry, str) else entry.get("direction"),
                           "seq": None if isinstance(entry, str) else entry.get("seq")})
    payload = {"previous_context": previous,
               "current_message": {"text": text[:CURRENT_CHARS], "direction": direction},
               "后文(参考，不是分类目标)": next_messages}
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def apply_short_message_guard(text, direction, context, detail):
    """The project promotes a windowless message of <=3 chars to review.

    ``classify_with_review`` does this after validation, precisely: with no
    previous turn, a stripped length of at most three characters forces
    ``needs_review=True, reason='very_short_message'`` without touching the labels.
    """
    if not list(context) and len(text.strip()) <= 3:
        detail = dict(detail, needs_review=True, reason="very_short_message")
    return detail


def generation_payload(messages: list) -> dict:
    return {"messages": messages, **GENERATION, "max_tokens": MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "classification",
                                                "strict": True,
                                                "schema": detail_schema()}}}
