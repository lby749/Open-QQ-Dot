"""Corpus annotation runner: feed MPDD and CPED to the local Qwen3.5-2B and score it.

Typical use
-----------
    # see what data was found
    python dataset_annotation/cli.py find --data-root E:\\qwen35-nlp\\data

    # a fast sanity run: 200 utterances, 2 tasks, one split
    python dataset_annotation/cli.py run --dataset cped --split valid \\
        --tasks cped-emotion,cped-da --limit 200

    # the full run over both corpora
    python dataset_annotation/cli.py run --dataset cped --tasks all
    python dataset_annotation/cli.py run --dataset mpdd --tasks all

Every invocation is resumable: progress is checkpointed per (dataset, split,
task) into ``--out/state.json`` and predictions are appended to a JSONL file, so
re-running the same command continues where it stopped instead of restarting.
Re-running an already finished run is a no-op unless ``--restart`` is given.

Output contract
---------------
One JSON object per line in ``<out>/<dataset>-<split>.jsonl``:

    {"record_id", "message_seq", "speaker_id", "text", "source_origin",
     "message_type", "media_id", "data_source", "split", "target_speaker",
     "listener_relations", "context", "metadata", "gold",      <- source side
     "task", "field", "value", "confidence", "ambiguous",      <- model side
     "gold_value", "match", "error", "attempts", "annotation_id"}

The source-side field names mirror ``dataset_importer._FIELD_KEYS`` so the text
can also be fed to the project's importer through a plain JSONL mapping; the
model's answer and the dataset's gold label deliberately sit in their own keys
because the importer has no column for them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # allow ``python dataset_annotation/cli.py``
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import annotate_tasks  # noqa: E402
import dataset_loaders as loaders  # noqa: E402
from annotate_client import AnnotationClient, ModelUnavailable  # noqa: E402
from annotate_labels import gloss, matched  # noqa: E402

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"
BATCH_SIZE = 64


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _json_default(value):
    return str(value)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def resolve_tasks(task_arg: str, dataset: str) -> list[str]:
    """Expand ``all`` / comma lists into concrete task keys for a dataset."""
    if task_arg.strip().lower() in ("all", "*"):
        keys = annotate_tasks.tasks_for(dataset)
        if not keys:
            raise SystemExit(f"no tasks registered for dataset {dataset!r}")
        return keys
    keys = [item.strip() for item in task_arg.split(",") if item.strip()]
    if not keys:
        raise SystemExit("--tasks is empty")
    for key in keys:
        task = annotate_tasks.get_task(key)
        if task.dataset != dataset:
            raise SystemExit(f"task {key!r} belongs to dataset {task.dataset!r}, "
                             f"not {dataset!r}")
    return keys


def _whole_dialogues(items, limit: int) -> list:
    """Keep whole leading dialogues until at least ``limit`` utterances are held.

    Slicing a corpus at an arbitrary index cuts dialogues in half, which strips
    the context an utterance needs and makes every downstream score meaningless.
    """
    kept: list = []
    current: str | None = None
    for item in items:
        if current is not None and item.conversation_id != current and len(kept) >= limit:
            break
        current = item.conversation_id
        kept.append(item)
    return kept


def _listener_for(task, utterance, target_speaker):
    _, listener = task.messages(utterance, target_speaker)
    return listener


# --------------------------------------------------------------------------- #
# find
# --------------------------------------------------------------------------- #
def command_find(args) -> int:
    specs = loaders.discover(args.data_root)
    if not specs:
        print(f"no dataset found under {args.data_root}", flush=True)
        return 1
    print(f"data root: {args.data_root}", flush=True)
    for key in sorted(specs):
        spec = specs[key]
        files = ", ".join(path.name for path in spec.files)
        tasks = ", ".join(annotate_tasks.tasks_for(spec.name))
        print(f"  {key:<14} {spec.description}", flush=True)
        print(f"                 files: {files}", flush=True)
        print(f"                 tasks: {tasks}", flush=True)
    return 0


def command_tasks(args) -> int:
    for key in sorted(annotate_tasks.TASKS):
        task = annotate_tasks.TASKS[key]
        gold = f"gold={task.gold_key}" if task.scored else "no gold (agreement only)"
        print(f"  {key:<16} dataset={task.dataset:<5} field={task.field:<10} "
              f"{len(task.allowed)} labels, {gold}", flush=True)
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@dataclass
class Progress:
    """Per (task) cursor inside one dataset/split run."""

    next_index: int = 0
    scored: int = 0
    matches: int = 0
    errors: int = 0
    ambiguous: int = 0
    confidence_sum: float = 0.0
    confidence_count: int = 0
    done: bool = False
    latencies: list = field(default_factory=list)
    warmup_latencies: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"next_index": self.next_index, "scored": self.scored,
                "matches": self.matches, "errors": self.errors,
                "ambiguous": self.ambiguous,
                "confidence_sum": round(self.confidence_sum, 4),
                "confidence_count": self.confidence_count, "done": self.done,
                "latencies": [round(value, 3) for value in self.latencies],
                "warmup_latencies": [round(value, 3) for value in self.warmup_latencies]}

    @classmethod
    def from_dict(cls, payload: dict) -> "Progress":
        fresh = cls()
        for key in fresh.__dict__:
            if key in payload and payload[key] is not None:
                setattr(fresh, key, payload[key])
        return fresh


def command_run(args) -> int:
    spec = loaders.resolve(args.dataset, args.split, args.data_root)
    tasks = resolve_tasks(args.tasks, spec.name)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = out_dir / f"{spec.name}-{spec.split}.jsonl"
    state_path = out_dir / "state.json"
    summary_path = out_dir / f"summary-{spec.name}-{spec.split}.json"

    print(f"dataset   : {spec.name} split={spec.split}", flush=True)
    print(f"tasks     : {', '.join(tasks)}", flush=True)
    print(f"data files: {', '.join(str(p) for p in spec.files)}", flush=True)

    started = time.perf_counter()
    items = loaders.load(spec, context_turns=args.context_turns)
    print(f"loaded    : {len(items)} utterances in {time.perf_counter() - started:.1f}s",
          flush=True)
    if args.limit is not None:
        if args.group_by_dialogue:
            items = _whole_dialogues(items, args.limit)
            print(f"limit     : {len(items)} utterances in "
                  f"{len({item.conversation_id for item in items})} whole dialogues",
                  flush=True)
        else:
            items = items[:args.limit]
            print(f"limit     : first {len(items)} utterances "
                  f"({len({item.conversation_id for item in items})} dialogues, "
                  f"possibly truncated mid-dialogue)", flush=True)

    state = _load_json(state_path) or {}
    run_key = f"{spec.name}-{spec.split}"
    run_state = state.setdefault(run_key, {"dataset": spec.name, "split": spec.split,
                                           "source_files": [str(p) for p in spec.files],
                                           "tasks": {}})
    if args.restart:
        run_state["tasks"] = {}
        if predictions_path.exists():
            predictions_path.unlink()
        print("restart   : cleared previous progress for this run", flush=True)

    todo = []
    for key in tasks:
        progress = Progress.from_dict(run_state["tasks"].get(key, {}))
        run_state["tasks"][key] = progress.as_dict()
        if progress.done and not args.restart:
            print(f"task {key}: already complete, skipping", flush=True)
            continue
        todo.append((key, progress))
    if not todo:
        print("nothing to do", flush=True)
        _write_json(state_path, state)
        return 0

    checkpoint_every = max(1, args.checkpoint_every)
    client = AnnotationClient(project_root=args.project_root,
                              slots=args.slots, context_size=args.context_size,
                              force_cpu=args.cpu, device=getattr(args,'device',None))
    try:
        client.start()
        print(f"model     : device={client.actual_device} slots={client.slots} "
              f"ctx={client.context_size} load={client.load_seconds:.2f}s",
              flush=True)
        if client.fallback_reason:
            print(f"            fallback reason: {client.fallback_reason}", flush=True)
        with predictions_path.open("a", encoding="utf-8") as sink:
            for key, progress in todo:
                task = annotate_tasks.get_task(key)
                print(f"--- task {key} (field={task.field}) "
                      f"from index {progress.next_index}", flush=True)
                _run_one_task(client, task, items, progress, sink, args,
                              checkpoint_every, lambda: _write_json(state_path, state))
                progress.done = progress.next_index >= len(items)
                run_state["tasks"][key] = progress.as_dict()
                _write_json(state_path, state)
                accuracy = (f"{progress.matches / progress.scored:.1%}"
                            if progress.scored else "n/a")
                print(f"    done {progress.next_index}/{len(items)} "
                      f"match={progress.matches}/{progress.scored} ({accuracy}) "
                      f"errors={progress.errors}", flush=True)
    finally:
        client.close()

    summary = _summarize(spec, tasks, run_state, items, time.perf_counter() - started,
                         args.gloss, args.context_turns)
    _write_json(summary_path, summary)
    print(f"predictions -> {predictions_path}", flush=True)
    print(f"summary     -> {summary_path}", flush=True)
    _print_summary(summary)
    return 0


def _run_one_task(client, task, items, progress: Progress, sink, args,
                  checkpoint_every: int, flush_state) -> None:
    """Annotate one task over ``items``, appending rows and checkpointing."""
    started = time.perf_counter()
    index = progress.next_index
    while index < len(items):
        batch = items[index:index + BATCH_SIZE]
        listeners = [_listener_for(task, item, args.target_speaker) for item in batch]
        results = client.annotate_many(task, batch, target_speaker=args.target_speaker,
                                       glosses=args.gloss)
        for utterance, listener, result in zip(batch, listeners, results):
            _emit_row(task, utterance, listener, result, sink, progress)
        sink.flush()
        index += len(batch)
        progress.next_index = index
        if checkpoint_every and index % checkpoint_every < BATCH_SIZE:
            flush_state()
        if batch:
            rate = len(batch) / max(time.perf_counter() - started, 1e-9)
            print(f"    {index}/{len(items)}  {rate:.1f} utt/s  "
                  f"errors={progress.errors}", flush=True)
    progress.next_index = len(items)


def _emit_row(task, utterance, listener, result, sink, progress: Progress) -> None:
    """Write one JSONL row and fold the result into the running counters."""
    gold_value = task.gold_value(utterance, listener)
    row = utterance.prompt_row()
    row.update({
        "annotation_id": utterance.annotation_id,
        "task": task.key,
        "field": task.field,
        "value": result.get("value"),
        "confidence": result.get("confidence"),
        "ambiguous": result.get("ambiguous"),
        "attempts": result.get("attempts"),
        "seconds": result.get("seconds"),
        "error": result.get("error"),
        "gold_value": gold_value,
        "match": (matched(result.get("value"), gold_value, task.allowed)
                  if result.get("value") is not None and gold_value is not None else None),
    })
    sink.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")
    if gold_value is not None:
        progress.scored += 1
    if row["match"]:
        progress.matches += 1
    if result.get("error"):
        progress.errors += 1
    if result.get("ambiguous"):
        progress.ambiguous += 1
    if isinstance(result.get("confidence"), (int, float)):
        progress.confidence_sum += float(result["confidence"])
        progress.confidence_count += 1
    seconds = result.get("seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        # Keep a bounded sample so state.json cannot grow without limit.
        if len(progress.latencies) < 2048:
            progress.latencies.append(round(float(seconds), 3))
        if len(progress.warmup_latencies) < 6:
            progress.warmup_latencies.append(round(float(seconds), 3))


def _summarize(spec, tasks, run_state, items, elapsed, glosses=False,
               context_turns=3) -> dict:
    per_task = {}
    for key in tasks:
        progress = Progress.from_dict(run_state["tasks"].get(key, {}))
        accuracy = (progress.matches / progress.scored) if progress.scored else None
        latencies = [value for value in progress.latencies if value > 0]
        per_task[key] = {
            "processed": progress.next_index,
            "gold_scored": progress.scored,
            "matches": progress.matches,
            "accuracy": None if accuracy is None else round(accuracy, 4),
            "errors": progress.errors,
            "ambiguous": progress.ambiguous,
            "mean_confidence": (round(progress.confidence_sum / progress.confidence_count, 4)
                                if progress.confidence_count else None),
            "median_seconds": round(statistics.median(latencies), 3) if latencies else None,
            "warmup_seconds": progress.warmup_latencies,
            "prompt_sha256": hashlib.sha256(
                annotate_tasks.get_task(key).system_prompt(glosses=glosses)
                .encode("utf-8")).hexdigest()[:16],
        }
    return {"dataset": spec.name, "split": spec.split, "utterances": len(items),
            "context_turns": context_turns,
            "label_glosses_in_prompt": bool(glosses),
            "gloss_note": ("labels are the datasets' own tokens" if not glosses else
                           "prompt also carries our own Chinese reading of each label; "
                           "the repositories ship no Chinese label text"),
            "tasks": per_task, "elapsed_seconds": round(elapsed, 2),
            "utterances_per_second": (round(sum(item["processed"] for item in per_task.values())
                                            / elapsed, 3) if elapsed > 0 else None),
            "source_files": [str(path) for path in spec.files]}


def _print_summary(summary: dict) -> None:
    print(f"summary: {summary['dataset']}/{summary['split']} "
          f"{summary['utterances']} utterances, {summary['elapsed_seconds']}s, "
          f"{summary['utterances_per_second']} utt/s", flush=True)
    for key, row in sorted(summary["tasks"].items()):
        accuracy = row["accuracy"]
        text = "n/a (no gold)" if accuracy is None else f"{accuracy:.1%}"
        print(f"  {key:<16} acc={text:<14} gold={row['gold_scored']:<7} "
              f"errors={row['errors']:<5} ambiguous={row['ambiguous']:<6} "
              f"conf={row['mean_confidence']} med_s={row['median_seconds']}", flush=True)


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def command_verify(args) -> int:
    """Re-check the export against the dataset: no fabricated text, valid labels."""
    spec = loaders.resolve(args.dataset, args.split, args.data_root)
    out_dir = Path(args.out).resolve()
    predictions_path = out_dir / f"{spec.name}-{spec.split}.jsonl"
    if not predictions_path.is_file():
        print(f"no predictions at {predictions_path}", flush=True)
        return 1
    print(f"rebuilding source index from {spec.name}/{spec.split} ...", flush=True)
    source = {item.annotation_id: item for item in loaders.load(spec, context_turns=0)}
    print(f"source utterances: {len(source)}", flush=True)

    checked = text_mismatch = unknown_id = bad_label = 0
    mismatched: list = []
    gold_mismatch = 0
    with predictions_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            checked += 1
            item = source.get(row.get("annotation_id"))
            if item is None:
                unknown_id += 1
                continue
            if row.get("text") != item.text:
                text_mismatch += 1
                mismatched.append(line_number)
            task = annotate_tasks.get_task(row["task"])
            if row.get("value") is not None and row["value"] not in task.allowed:
                bad_label += 1
            gold = task.gold_value(item, item.listener_relations or None)
            if gold is not None and row.get("gold_value") != gold:
                gold_mismatch += 1
    ok = not (text_mismatch or unknown_id or bad_label or gold_mismatch)
    report = {"predictions": str(predictions_path), "rows_checked": checked,
              "unknown_annotation_id": unknown_id, "text_mismatch": text_mismatch,
              "gold_mismatch": gold_mismatch, "label_outside_enum": bad_label,
              "mismatched_lines": mismatched[:20], "source_utterances": len(source),
              "ok": ok}
    _write_json(out_dir / f"verify-{spec.name}-{spec.split}.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if ok else 2


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #
def command_inspect(args) -> int:
    spec = loaders.resolve(args.dataset, args.split, args.data_root)
    items = loaders.load(spec, context_turns=args.context_turns)
    print(f"{spec.name}/{spec.split}: {len(items)} utterances", flush=True)
    for item in items[:args.limit]:
        print(json.dumps(item.prompt_row(), ensure_ascii=False, indent=2), flush=True)
    if args.show_prompt:
        for key in resolve_tasks(args.tasks, spec.name)[:1]:
            task = annotate_tasks.get_task(key)
            messages, listener = task.messages(items[0], args.target_speaker,
                                               glosses=args.gloss)
            print(f"--- system prompt for {key} (glosses={args.gloss}) ---", flush=True)
            print(messages[0]["content"], flush=True)
            print("--- user payload ---", flush=True)
            print(messages[1]["content"], flush=True)
            print("--- schema ---", flush=True)
            print(json.dumps(task.schema(), ensure_ascii=False), flush=True)
            print(f"--- listener for relation: {listener}", flush=True)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataset-annotation",
        description="Annotate MPDD and CPED with the local Qwen3.5-2B model.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target):
        target.add_argument("--data-root", default=loaders.DATA_ROOT_DEFAULT,
                            help="directory holding the downloaded datasets")
        target.add_argument("--out", default=str(DEFAULT_OUT_DIR),
                            help="output directory")
        target.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent),
                            help="project root providing the model runtime")

    find = sub.add_parser("find", help="list datasets found under the data root")
    find.add_argument("--data-root", default=loaders.DATA_ROOT_DEFAULT)
    find.set_defaults(func=command_find)

    tasks = sub.add_parser("tasks", help="list annotation tasks")
    tasks.set_defaults(func=command_tasks)

    run = sub.add_parser("run", help="annotate a dataset split")
    add_common(run)
    run.add_argument("--dataset", required=True, choices=["mpdd", "cped"])
    run.add_argument("--split", default=None,
                     help="cped: train|valid|test; mpdd: mpdd (default: only split)")
    run.add_argument("--tasks", default="all",
                     help="comma list of task keys, or 'all'")
    run.add_argument("--limit", type=int, default=None,
                     help="only process the first N utterances")
    run.add_argument("--group-by-dialogue", action="store_true",
                     help="with --limit, keep whole dialogues instead of cutting mid-dialogue")
    run.add_argument("--context-turns", type=int, default=3,
                     help="how many preceding utterances to include (default 3)")
    run.add_argument("--target-speaker", default=None,
                     help="MPDD relation: ask about this listener instead of the first")
    run.add_argument("--slots", type=int, default=None,
                     help="parallel llama.cpp slots (default: runtime.json)")
    run.add_argument("--context-size", type=int, default=16384)
    run.add_argument("--device", choices=("auto","cuda","mps","metal","cpu"), default=None, help="llama.cpp backend: CUDA, then Metal, then CPU in auto mode")
    run.add_argument("--cpu", action="store_true", help="force the CPU server")
    run.add_argument("--restart", action="store_true", help="discard saved progress")
    run.add_argument("--gloss", action="store_true",
                     help="append our own Chinese reading of each label to the prompt; "
                          "the datasets ship no Chinese label text, so this measures a "
                          "reinterpreted task and is recorded as such")
    run.add_argument("--checkpoint-every", type=int, default=BATCH_SIZE,
                     help="flush progress to state.json every N utterances")
    run.set_defaults(func=command_run)

    verify = sub.add_parser("verify", help="re-check an export against the dataset")
    add_common(verify)
    verify.add_argument("--dataset", required=True, choices=["mpdd", "cped"])
    verify.add_argument("--split", default=None)
    verify.set_defaults(func=command_verify)

    inspect = sub.add_parser("inspect", help="print sample records and prompts")
    add_common(inspect)
    inspect.add_argument("--dataset", required=True, choices=["mpdd", "cped"])
    inspect.add_argument("--split", default=None)
    inspect.add_argument("--tasks", default="all")
    inspect.add_argument("--limit", type=int, default=2)
    inspect.add_argument("--context-turns", type=int, default=3)
    inspect.add_argument("--target-speaker", default=None)
    inspect.add_argument("--gloss", action="store_true",
                         help="show the prompt with our own Chinese label readings")
    inspect.add_argument("--show-prompt", action="store_true")
    inspect.set_defaults(func=command_inspect)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (loaders.DatasetError, ModelUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 3
    except KeyboardInterrupt:
        print("interrupted; progress is saved, re-run the same command to resume",
              file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
