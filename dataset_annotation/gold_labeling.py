"""Human-in-the-loop gold labeling for plan A: sheet building, validation, scoring.

Plan A measures the project's own four-label task (intent/emotion/tone/
initiative) on CPED and MPDD text. Those labels exist only in this project, so
accuracy needs human gold labels. The loop is:

1. ``build_sheet`` writes one row per sampled item. Model labels are pre-filled
   as a starting point; the ``human_*`` columns start empty. Pre-filling is
   cheaper for the annotator but anchors their judgment, so every sheet records
   that the pre-fill happened.
2. The annotator fills the ``human_*`` columns (or blanks them to reject the
   model's guess and re-decide).
3. ``read_sheet`` validates the sheet strictly: enums, blank handling, and the
   project's needs_review/reason consistency rule.
4. ``score`` reports per-field accuracy with a Wilson interval and a
   majority-class baseline, plus agreement between model and human.
"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path

try:
    import project_contract
    from project_contract import ENUMS, LABEL_FIELDS, REVIEW_REASONS
except ImportError:
    from . import project_contract
    from .project_contract import ENUMS, LABEL_FIELDS, REVIEW_REASONS

SHEET_COLUMNS = [
    "item_id", "dataset", "split", "conversation", "turn", "speaker",
    "speaker_count", "direction", "synthetic_direction", "direction_policy",
    "narration_after_quote", "narration_tail",
    "context", "text", "following",
    "model_intent", "model_emotion", "model_tone", "model_initiative",
    "model_needs_review", "model_reason",
    "human_intent", "human_emotion", "human_tone", "human_initiative",
    "human_needs_review", "human_reason", "annotator_note",
]
HUMAN_LABEL_COLUMNS = [f"human_{field}" for field in LABEL_FIELDS]


class SheetError(ValueError):
    """The returned labeling sheet does not satisfy the frozen contract."""


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #
def _window(item):
    """Split an item's context into the project's previous / following parts."""
    previous = [{"text": entry.get("text", ""), "direction": entry.get("direction"),
                 "seq": entry.get("seq")} for entry in item.context]
    following = [{"text": entry.get("text", ""), "direction": entry.get("direction"),
                  "seq": entry.get("seq")} for entry in item.metadata.get("following", ())]
    return previous, following


def model_job(item, ablate_direction: bool = False) -> dict:
    """The exact classification request the project's own client would make."""
    previous, following = _window(item)
    direction = None if ablate_direction else item.metadata.get("direction")
    return {"text": item.text, "direction": direction,
            "context": previous, "following": following}


def build_sheet(items, path, results, ablate_direction: bool = False) -> Path:
    """Write the annotator workbook as UTF-8-with-BOM CSV (Excel-safe)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SHEET_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for item, result in zip(items, results):
        labels = (result or {}).get("labels") or {}
        previous, following = _window(item)
        row = {
            "item_id": item.annotation_id,
            "dataset": item.data_source,
            "split": item.split,
            "conversation": item.conversation_id,
            "turn": item.message_seq,
            "speaker": item.speaker_id,
            "speaker_count": item.metadata.get("speaker_count"),
            "direction": item.metadata.get("direction"),
            "synthetic_direction": item.metadata.get("synthetic_direction"),
            "direction_policy": item.metadata.get("direction_policy"),
            "narration_after_quote": item.metadata.get("narration_after_quote"),
            "narration_tail": item.metadata.get("narration_tail", ""),
            "context": json.dumps(previous, ensure_ascii=False),
            "text": item.text,
            "following": json.dumps(following, ensure_ascii=False),
            "model_intent": labels.get("intent", ""),
            "model_emotion": labels.get("emotion", ""),
            "model_tone": labels.get("tone", ""),
            "model_initiative": labels.get("initiative", ""),
            "model_needs_review": (result or {}).get("needs_review"),
            "model_reason": (result or {}).get("reason", ""),
        }
        for column in HUMAN_LABEL_COLUMNS + ["human_needs_review", "human_reason",
                                             "annotator_note"]:
            row[column] = ""
        writer.writerow(row)
    # utf-8-sig so Excel on a Chinese Windows locale opens it without mojibake.
    path.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
    return path


# --------------------------------------------------------------------------- #
# Reading and validating
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GoldRow:
    item_id: str
    labels: dict
    needs_review: bool | None
    reason: str | None
    note: str
    model_labels: dict


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def read_sheet(path, require_complete: bool = True) -> tuple[list[GoldRow], list[str]]:
    """Read a filled sheet. Returns ``(rows, problems)``.

    A row is either fully labeled (all four fields) or skipped as not yet done.
    Anything half-filled, or outside an enum, is a problem.
    """
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise SheetError("the sheet has no header")
        missing = [name for name in SHEET_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise SheetError(f"the sheet is missing columns: {missing}")
        rows, problems = [], []
        seen: set[str] = set()
        for number, raw in enumerate(reader, start=2):
            item_id = _clean(raw["item_id"])
            if not item_id:
                problems.append(f"line {number}: empty item_id")
                continue
            if item_id in seen:
                problems.append(f"line {number}: duplicate item_id {item_id}")
                continue
            seen.add(item_id)
            values = {field: _clean(raw[f"human_{field}"]) for field in LABEL_FIELDS}
            filled = [field for field, value in values.items() if value]
            if not filled:
                if require_complete:
                    problems.append(f"line {number}: no human labels for {item_id}")
                continue
            if len(filled) != len(LABEL_FIELDS):
                problems.append(f"line {number}: partially labeled {item_id}; "
                                f"missing {sorted(set(LABEL_FIELDS) - set(filled))}")
                continue
            bad = [f"{field}={values[field]!r}" for field in LABEL_FIELDS
                   if values[field] not in ENUMS[field]]
            if bad:
                problems.append(f"line {number}: value outside the enum: {bad}")
                continue
            review = _clean(raw["human_needs_review"]).lower()
            if review in ("true", "yes", "1", "y", "是"):
                needs_review = True
            elif review in ("false", "no", "0", "n", "否"):
                needs_review = False
            elif review == "":
                needs_review = None
            else:
                problems.append(f"line {number}: needs_review must be true/false or blank")
                continue
            reason = _clean(raw["human_reason"]) or None
            if reason is not None and reason not in REVIEW_REASONS:
                problems.append(f"line {number}: reason {reason!r} is outside the enum")
                continue
            if needs_review is not None and reason is not None:
                # Same consistency rule the project enforces on the model.
                if needs_review == (reason == "clear"):
                    problems.append(f"line {number}: needs_review and reason are "
                                    f"inconsistent ({needs_review}/{reason})")
                    continue
            if needs_review is None:
                # Default the review flag from the reason, mirroring the project.
                needs_review = reason != "clear"
            rows.append(GoldRow(
                item_id=item_id, labels=values, needs_review=needs_review,
                reason=reason, note=_clean(raw["annotator_note"]),
                model_labels={field: _clean(raw[f"model_{field}"]) for field in LABEL_FIELDS}))
        return rows, problems


def save_answer_key(rows: list[GoldRow], path) -> Path:
    """Persist the validated gold labels as JSONL keyed by item_id."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps({
                "item_id": row.item_id, "labels": row.labels,
                "needs_review": row.needs_review, "reason": row.reason,
                "note": row.note, "model_labels": row.model_labels},
                ensure_ascii=False) + "\n")
    return path


def load_answer_key(path) -> dict[str, GoldRow]:
    rows: dict[str, GoldRow] = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            rows[payload["item_id"]] = GoldRow(
                item_id=payload["item_id"], labels=payload["labels"],
                needs_review=payload.get("needs_review"), reason=payload.get("reason"),
                note=payload.get("note", ""), model_labels=payload.get("model_labels", {}))
    return rows


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (better than normal approx
    when the count is small, which matters at n=100)."""
    if total <= 0:
        return (0.0, 0.0)
    phat = hits / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total))
    return (max(0.0, (centre - margin) / denominator),
            min(1.0, (centre + margin) / denominator))


def score(predictions: dict[str, dict], gold: dict[str, GoldRow]) -> dict:
    """Per-field accuracy against human labels, with baselines and an interval.

    ``predictions`` maps item_id -> the model's ``{"labels", ...}`` result.
    """
    keys = [key for key in gold if key in predictions]
    per_field = {}
    for field in LABEL_FIELDS:
        valid = []
        for key in keys:
            predicted = (predictions[key].get("labels") or {}).get(field)
            if predicted:
                valid.append((predicted, gold[key].labels[field]))
        hits = sum(1 for predicted, truth in valid if predicted == truth)
        total = len(valid)
        low, high = wilson(hits, total)
        counts: dict[str, int] = {}
        for _, truth in valid:
            counts[truth] = counts.get(truth, 0) + 1
        majority = max(counts.values()) / total if total else 0.0
        per_field[field] = {
            "total": total, "hits": hits,
            "accuracy": None if not total else round(hits / total, 4),
            "wilson_low": None if not total else round(low, 4),
            "wilson_high": None if not total else round(high, 4),
            "majority_class_baseline": round(majority, 4),
            "gold_distribution": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        }
    joints = [all((predictions[key]["labels"] or {}).get(f) == gold[key].labels[f]
                  for f in LABEL_FIELDS) for key in keys
              if predictions[key].get("labels")]
    agreement = None
    compared = [key for key in keys if gold[key].model_labels.get("intent")]
    if compared:
        agree = sum(1 for key in compared
                    if all(gold[key].model_labels.get(f) == gold[key].labels[f]
                           for f in LABEL_FIELDS))
        agreement = round(agree / len(compared), 4)
    return {"scored_items": len(keys), "per_field": per_field,
            "all_four_correct": sum(joints), "all_four_accuracy":
                None if not joints else round(sum(joints) / len(joints), 4),
            "model_matches_prefill_on_all_four": agreement,
            "prefill_note": ("model labels were pre-filled in the sheet, so agreement "
                             "is not an independent model accuracy measurement")}
