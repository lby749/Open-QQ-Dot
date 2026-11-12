"""Readers that normalize MPDD and CPED into one annotation input contract.

Neither dataset is in the project's importer format, and neither ships the
project's field names, so this module is the translation layer. It never
rewrites the source text: Traditional Chinese in MPDD and the CPED utterances
are passed through byte-for-byte, and gold labels are kept as the dataset
spelled them.

Observed source formats (verified against the downloaded files, not the READMEs)
-------------------------------------------------------------------------------
MPDD -- mpdd/mpdd/dialogue.json is ONE JSON object:

    {"1": [ {"speaker": "Mother Zuo",
             "utterance": "What is worth sending to that silly woman...",
             "listener": [{"name": "Father Zuo", "relation": "spouse"}],
             "emotion": "disgust"}, ... ], "2": [...], ...}

    mpdd/mpdd/metadata.json declares relation (25), emotion (7), and the
    field/position groupings. 4142 dialogues, 25548 utterances, Traditional
    Chinese, 1825 utterances address more than one listener.

CPED -- data/CPED/{train,valid,test}_split.csv, 18 columns:

    TV_ID,Dialogue_ID,Utterance_ID,Speaker,Gender,Age,Neuroticism,Extraversion,
    Openness,Agreeableness,Conscientiousness,Scene,FacePosition_LU,
    FacePosition_RD,Sentiment,Emotion,DA,Utterance

    94187 + 11137 + 27438 = 132762 utterances over 8086 + 934 + 2815 dialogues.
    The CSV really spells Emotion as anger/negative-other/depress/positive-other/
    sadness/fear/disgust/astonished and DA as agreement -- see annotate_labels.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DATA_ROOT_DEFAULT = r"E:\qwen35-nlp\data"

MPDD_DATA_SOURCE = "Dialogue-MPDD"
CPED_DATA_SOURCE = "CPED"

_MPDD_SPLIT_NAME = "mpdd"


class DatasetError(RuntimeError):
    """A dataset file is missing or does not match the format read here."""


@dataclass(frozen=True)
class Utterance:
    """One annotation target plus everything known about it."""

    annotation_id: str
    data_source: str
    split: str
    conversation_id: str
    message_seq: int
    speaker_id: str
    text: str
    context: tuple[dict, ...] = ()
    gold: dict = field(default_factory=dict)
    target_speaker: str | None = None
    listener_relations: tuple[tuple[str, str], ...] = ()
    metadata: dict = field(default_factory=dict)
    source_file: str = ""

    def prompt_row(self) -> dict:
        """The row shape written to JSONL, mirroring the project's importer fields.

        Field names follow dataset_importer._FIELD_KEYS so the export can be fed
        to DatasetImporter with a mapping, while ``labels``/``gold`` carry the
        annotation payload the importer intentionally has no column for.
        """
        return {
            "record_id": self.conversation_id,
            "source_record_id": self.annotation_id,
            "message_seq": self.message_seq,
            "speaker_id": self.speaker_id,
            "speaker_role": "unmapped",
            "text": self.text,
            "event_time": None,
            "source_origin": "PUBLIC_DATASET",
            "message_type": "text",
            "media_id": None,
            "data_source": self.data_source,
            "split": self.split,
            "target_speaker": self.target_speaker,
            "listener_relations": [{"speaker": name, "relation": relation}
                                   for name, relation in self.listener_relations],
            "context": list(self.context),
            "gold": dict(self.gold),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    data_root: Path
    split: str
    files: tuple[Path, ...]
    description: str


def _digest(*parts) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _first_existing(candidates) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def find_mpdd(root: Path) -> tuple[Path, Path] | None:
    """Locate dialogue.json and metadata.json anywhere under ``root``."""
    root = Path(root)
    if not root.exists():
        return None
    dialogue = _first_existing([
        root / "mpdd" / "mpdd" / "dialogue.json",
        root / "mpdd" / "dialogue.json",
    ])
    metadata = _first_existing([
        root / "mpdd" / "mpdd" / "metadata.json",
        root / "mpdd" / "metadata.json",
    ])
    if dialogue and metadata:
        return dialogue, metadata
    dialogues = sorted(root.rglob("dialogue.json"))
    metadatas = sorted(root.rglob("metadata.json"))
    # Ignore unrelated metadata.json files from other tooling at the data root.
    metadatas = [item for item in metadatas if "mpdd" in str(item).lower()]
    if dialogues and metadatas:
        return dialogues[0], metadatas[0]
    return None


def find_cped(root: Path) -> dict[str, Path]:
    """Map split name -> CSV path for whichever CPED splits are present."""
    root = Path(root)
    if not root.exists():
        return {}
    found: dict[str, Path] = {}
    for split in ("train", "valid", "test"):
        candidate = _first_existing([
            root / "cped" / f"{split}_split.csv",
            root / f"{split}_split.csv",
        ])
        if candidate is None:
            matches = sorted(root.rglob(f"{split}_split.csv"))
            candidate = matches[0] if matches else None
        if candidate is not None:
            found[split] = candidate
    return found


def discover(data_root=DATA_ROOT_DEFAULT) -> dict[str, DatasetSpec]:
    """Return the datasets available under ``data_root``, keyed by dataset name."""
    root = Path(data_root)
    specs: dict[str, DatasetSpec] = {}
    mpdd = find_mpdd(root)
    if mpdd:
        specs["mpdd"] = DatasetSpec(
            name="mpdd", data_root=root, split=_MPDD_SPLIT_NAME, files=mpdd,
            description="MPDD multi-party dialogue (Traditional Chinese): per-utterance emotion and listener relation annotations")
    for split, path in find_cped(root).items():
        specs[f"cped-{split}"] = DatasetSpec(
            name="cped", data_root=root, split=split, files=(path,),
            description=f"CPED Chinese personality emotion dialogue {split} split: Sentiment/Emotion/DA annotations")
    return specs


def available_splits(dataset: str, data_root=DATA_ROOT_DEFAULT) -> list[str]:
    return sorted(spec.split for spec in discover(data_root).values() if spec.name == dataset)


# --------------------------------------------------------------------------- #
# MPDD
# --------------------------------------------------------------------------- #
def _speaker_graph(dialogue: dict) -> dict[tuple[str, str], str]:
    """Build speaker -> listener -> relation from every listener annotation."""
    graph: dict[tuple[str, str], str] = {}
    for turns in dialogue.values():
        for turn in turns:
            speaker = turn.get("speaker")
            for listener in turn.get("listener") or ():
                name, relation = listener.get("name"), listener.get("relation")
                if speaker and name and relation:
                    graph.setdefault((speaker, name), relation)
                    graph.setdefault((name, speaker), _inverse(relation))
    return graph


_INVERSE_RELATION = {
    "parent": "child", "child": "parent", "grandparent": "grandchild",
    "grandchild": "grandparent", "teacher": "student", "student": "teacher",
    "boss": "subordinate", "subordinate": "boss",
    "parent-in-law": "son/daughter-in-law", "son/daughter-in-law": "parent-in-law",
    "other superior": "other inferior", "other inferior": "other superior",
}


def _inverse(relation: str) -> str:
    """The reciprocal of a relation where the corpus defines one."""
    return _INVERSE_RELATION.get(relation, relation)


def load_mpdd(spec: DatasetSpec, context_turns: int = 3, **_) -> list[Utterance]:
    """Read every MPDD dialogue into utterance records in speaking order."""
    dialogue_path, metadata_path = spec.files
    try:
        with dialogue_path.open(encoding="utf-8") as stream:
            dialogue = json.load(stream)
        with metadata_path.open(encoding="utf-8") as stream:
            metadata = json.load(stream)
    except (OSError, ValueError) as exc:
        raise DatasetError(f"MPDD files unreadable: {exc}") from exc
    if not isinstance(dialogue, dict) or not dialogue:
        raise DatasetError("MPDD dialogue.json is not a nonempty case-index object")
    for key in ("emotion", "relation"):
        if key not in metadata:
            raise DatasetError(f"MPDD metadata.json is missing {key!r}")

    speakers = set()
    for turns in dialogue.values():
        for turn in turns:
            speakers.add(turn.get("speaker"))
    graph = _speaker_graph(dialogue)

    items: list[Utterance] = []
    for case_index in sorted(dialogue, key=lambda item: (len(str(item)), str(item))):
        turns = dialogue[case_index]
        if not isinstance(turns, list):
            raise DatasetError(f"MPDD case {case_index} is not a list of utterances")
        history: list[dict] = []
        for ordinal, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise DatasetError(f"MPDD case {case_index} utterance {ordinal} is not an object")
            text = turn.get("utterance")
            speaker = turn.get("speaker")
            if not isinstance(text, str) or not text.strip():
                raise DatasetError(f"MPDD case {case_index} utterance {ordinal} has no text")
            if not isinstance(speaker, str) or not speaker.strip():
                raise DatasetError(f"MPDD case {case_index} utterance {ordinal} has no speaker")
            listeners = []
            for listener in turn.get("listener") or ():
                if isinstance(listener, dict) and listener.get("name"):
                    listeners.append((str(listener["name"]),
                                      str(listener.get("relation", "unknown"))))
            metadata_row = dict(turn)
            metadata_row.pop("utterance", None)
            metadata_row["case_index"] = case_index
            metadata_row["utterance_index"] = ordinal
            metadata_row["dialogue_speakers"] = sorted(
                {str(t.get("speaker")) for t in turns if t.get("speaker")})
            items.append(Utterance(
                annotation_id=_digest("mpdd", case_index, ordinal),
                data_source=MPDD_DATA_SOURCE,
                split=spec.split,
                conversation_id=f"mpdd:{case_index}",
                message_seq=ordinal,
                speaker_id=speaker,
                text=text,
                context=tuple(history[-context_turns:]) if context_turns else (),
                gold={"emotion": turn.get("emotion"),
                      "relation": listeners[0][1] if listeners else None},
                listener_relations=tuple(listeners),
                metadata=metadata_row,
                source_file=str(dialogue_path),
            ))
            history.append({"speaker": speaker, "text": text,
                            "listener_names": [name for name, _ in listeners]})
    return items


# --------------------------------------------------------------------------- #
# CPED
# --------------------------------------------------------------------------- #
CPED_COLUMNS = (
    "TV_ID", "Dialogue_ID", "Utterance_ID", "Speaker", "Gender", "Age",
    "Neuroticism", "Extraversion", "Openness", "Agreeableness",
    "Conscientiousness", "Scene", "FacePosition_LU", "FacePosition_RD",
    "Sentiment", "Emotion", "DA", "Utterance",
)


def load_cped(spec: DatasetSpec, context_turns: int = 3, **_) -> list[Utterance]:
    """Read one CPED split CSV into utterance records in file order."""
    (csv_path,) = spec.files
    items: list[Utterance] = []
    history: list[dict] = []
    previous_dialogue = None
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise DatasetError(f"{csv_path.name} has no header")
        missing = [name for name in CPED_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise DatasetError(f"{csv_path.name} is missing columns {missing}")
        for line, row in enumerate(reader, start=2):
            dialogue_id = (row.get("Dialogue_ID") or "").strip()
            utterance_id = (row.get("Utterance_ID") or "").strip()
            text = row.get("Utterance") or ""
            speaker = (row.get("Speaker") or "").strip()
            if not dialogue_id or not utterance_id:
                raise DatasetError(f"{csv_path.name} line {line} lacks a dialogue/utterance id")
            if not text.strip():
                raise DatasetError(f"{csv_path.name} line {line} has no utterance text")
            if not speaker:
                raise DatasetError(f"{csv_path.name} line {line} has no speaker")
            if dialogue_id != previous_dialogue:
                history = []
                previous_dialogue = dialogue_id
            items.append(Utterance(
                annotation_id=_digest("cped", spec.split, utterance_id),
                data_source=CPED_DATA_SOURCE,
                split=spec.split,
                conversation_id=f"cped:{dialogue_id}",
                message_seq=len(history),
                speaker_id=speaker,
                text=text,
                context=tuple(history[-context_turns:]) if context_turns else (),
                gold={"sentiment": row.get("Sentiment"), "emotion": row.get("Emotion"),
                      "da": row.get("DA"), "scene": row.get("Scene"),
                      "gender": row.get("Gender"), "age": row.get("Age")},
                metadata={"tv_id": row.get("TV_ID"), "dialogue_id": dialogue_id,
                          "utterance_id": utterance_id,
                          "big_five": {name: row.get(name) for name in
                                       ("Neuroticism", "Extraversion", "Openness",
                                        "Agreeableness", "Conscientiousness")},
                          "face_position": [row.get("FacePosition_LU"), row.get("FacePosition_RD")]},
                source_file=str(csv_path),
            ))
            history.append({"speaker": speaker, "text": text})
    return items


_LOADERS = {"mpdd": load_mpdd, "cped": load_cped}


def load(spec: DatasetSpec, context_turns: int = 3) -> list[Utterance]:
    """Load a dataset spec with its registered reader."""
    try:
        loader = _LOADERS[spec.name]
    except KeyError:
        raise DatasetError(f"no reader registered for dataset {spec.name!r}") from None
    return loader(spec, context_turns=context_turns)


def resolve(dataset: str, split: str | None = None,
            data_root=DATA_ROOT_DEFAULT) -> DatasetSpec:
    """Look up one dataset spec, defaulting to the dataset's only split."""
    specs = discover(data_root)
    if split:
        key = f"{dataset}-{split}"
        if key in specs:
            return specs[key]
        raise DatasetError(f"split {split!r} of {dataset!r} is not present under {data_root}")
    candidates = [spec for spec in specs.values() if spec.name == dataset]
    if not candidates:
        raise DatasetError(f"dataset {dataset!r} is not present under {data_root}")
    if len(candidates) > 1:
        raise DatasetError(
            f"dataset {dataset!r} has several splits "
            f"({sorted(spec.split for spec in candidates)}); name one with --split")
    return candidates[0]
