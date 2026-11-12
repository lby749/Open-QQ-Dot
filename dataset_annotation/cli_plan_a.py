"""Plan A: score the project's own four-label classifier on CPED and MPDD text.

Plan A uses the two corpora purely as *input text*, and measures the label set
this project actually uses (intent / emotion / tone / initiative) from
``message_classifier``. No dataset-specific label is involved, so no
interpretation of CPED's or MPDD's own scheme can leak into the score.

Usage
-----
    # 1. deterministic stratified sample, with the user's direction policy
    python dataset_annotation/cli_plan_a.py sample --data-root E:\\qwen35-nlp\\data

    # 2. pre-label with the local model and write the annotator sheet
    python dataset_annotation/cli_plan_a.py classify --arm synthetic
    python dataset_annotation/cli_plan_a.py classify --arm null      # ablation

    # 3. after filling the human_* columns in the sheet
    python dataset_annotation/cli_plan_a.py score --arm synthetic
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import annotate_tasks  # noqa: E402,F401  (keeps the dataset tasks importable)
import dataset_loaders as loaders  # noqa: E402
import gold_labeling  # noqa: E402
import gold_sample  # noqa: E402
import project_contract  # noqa: E402
from annotate_client import AnnotationClient, ModelUnavailable  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "plan-a"
CPED_SPLIT_DEFAULT = "valid"


def _sample_path(out: Path, dataset: str, arm: str) -> Path:
    return out / f"sample-{dataset}-{arm}.jsonl"


def _predictions_path(out: Path, dataset: str, arm: str) -> Path:
    return out / f"predictions-{dataset}-{arm}.jsonl"


def _sheet_path(out: Path, dataset: str, arm: str) -> Path:
    return out / f"labeling-sheet-{dataset}-{arm}.csv"


def _write_items(path: Path, items) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for item in items:
            stream.write(json.dumps(asdict(item), ensure_ascii=False, default=str) + "\n")


def _load_sample(path: Path) -> list[loaders.Utterance]:
    """Rebuild Utterance records saved by ``sample``."""
    items = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            payload["context"] = tuple(payload.get("context") or ())
            payload["listener_relations"] = tuple(
                tuple(pair) for pair in payload.get("listener_relations") or ())
            items.append(loaders.Utterance(**payload))
    return items


# --------------------------------------------------------------------------- #
# sample
# --------------------------------------------------------------------------- #
def command_sample(args) -> int:
    out = Path(args.out).resolve()
    manifest = {"direction_policy": {
        "rule": "two speakers: first-appearance speaker = in, other = out, constant "
                "for the whole conversation",
        "multi_speaker": "direction stays null; not forced into a binary value",
        "meaning": "speaker identity / turn structure only; never a real send/receive "
                   "direction",
        "ablation": "the same items are also classified with direction null"},
        "context": {"turns": project_contract.CONTEXT_TURNS,
                    "previous_chars": project_contract.CONTEXT_CHARS,
                    "current_chars": project_contract.CURRENT_CHARS,
                    "following_turns": project_contract.FOLLOWING_TURNS},
        "contract_fingerprint": project_contract.contract_fingerprint(),
        "datasets": {}}

    targets = [("cped", args.cped_split), ("mpdd", None)]
    for dataset, split in targets:
        spec = loaders.resolve(dataset, split, args.data_root)
        print(f"loading {spec.name}/{spec.split} ...", flush=True)
        items = loaders.load(spec, context_turns=project_contract.CONTEXT_TURNS)
        direction_maps = gold_sample.build_direction_maps(items)
        summary = gold_sample.sample_summary(items, direction_maps)
        print(f"  {summary['utterances']} utterances, "
              f"{summary['conversations']} conversations, speaker counts "
              f"{summary['conversation_speaker_counts']}", flush=True)

        sample_spec = gold_sample.SampleSpec(
            n=args.n, min_speakers=2, max_speakers=2,
            stratify_by=args.stratify_by)
        chosen, excluded = gold_sample.sample_items(items, direction_maps, sample_spec)
        chosen = gold_sample.attach_following(chosen, items)
        chosen = gold_sample.direction_for_context(chosen, direction_maps)
        # message_seq is the utterance's real ordinal inside its conversation and
        # is left untouched; the sample position goes in a separate field.
        enriched = []
        for index, item in enumerate(chosen):
            mapped = gold_sample.apply_direction(item, direction_maps[item.conversation_id],
                                                 ablate=False)
            enriched.append(replace(mapped, metadata=dict(mapped.metadata,
                                                          sample_index=index)))
        chosen = enriched
        _write_items(_sample_path(out, dataset, "synthetic"), chosen)
        print(f"  sampled {len(chosen)} items (stratified by {args.stratify_by}), "
              f"excluded {len(excluded)} multi-speaker items", flush=True)

        if dataset == "mpdd" and args.include_multi_speaker:
            multi_spec = gold_sample.SampleSpec(
                n=args.n, min_speakers=3, max_speakers=99,
                stratify_by=args.stratify_by)
            multi, _ = gold_sample.sample_items(items, direction_maps, multi_spec)
            multi = gold_sample.attach_following(multi, items)
            multi = gold_sample.direction_for_context(multi, direction_maps)
            multi = [gold_sample.apply_direction(item, direction_maps[item.conversation_id],
                                                 ablate=False) for item in multi]
            _write_items(_sample_path(out, f"{dataset}-multi", "synthetic"), multi)
            print(f"  separate multi-speaker study: {len(multi)} items, "
                  f"direction=null (not forced to in/out)", flush=True)

        manifest["datasets"][dataset] = {
            "split": spec.split, "source_files": [str(p) for p in spec.files],
            **summary, "sampled": len(chosen), "excluded_multi_speaker": len(excluded),
            "sampled_two_speaker_conversations":
                len({item.conversation_id for item in chosen})}

    gold_sample.write_manifest(out / "sample-manifest.json", manifest)
    print(f"manifest -> {out / 'sample-manifest.json'}", flush=True)
    return 0


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #
def command_classify(args) -> int:
    out = Path(args.out).resolve()
    client = AnnotationClient(project_root=args.project_root, slots=args.slots,
                              context_size=args.context_size, force_cpu=args.cpu)
    fingerprint = project_contract.contract_fingerprint()
    print(f"project contract fingerprint: {fingerprint[:16]}", flush=True)
    try:
        client.start()
        print(f"model: device={client.actual_device} slots={client.slots} "
              f"load={client.load_seconds:.2f}s", flush=True)
        for dataset in args.datasets:
            sample_path = _sample_path(out, dataset, "synthetic")
            if not sample_path.is_file():
                print(f"no sample at {sample_path}; run sample first", flush=True)
                continue
            items = _load_sample(sample_path)
            if args.limit is not None:
                items = items[:args.limit]
            ablate = args.arm == "null"
            print(f"--- {dataset}: {len(items)} items, arm={args.arm} "
                  f"({'direction=null ablation' if ablate else 'synthetic direction'})",
                  flush=True)
            jobs = [gold_labeling.model_job(item, ablate_direction=ablate) for item in items]
            started = time.perf_counter()
            results = client.classify_project_many(jobs)
            elapsed = time.perf_counter() - started
            predictions = {}
            errors = 0
            with _predictions_path(out, dataset, args.arm).open("w", encoding="utf-8") as stream:
                for item, job, result in zip(items, jobs, results):
                    predictions[item.annotation_id] = result
                    if result.get("error"):
                        errors += 1
                    stream.write(json.dumps({
                        "item_id": item.annotation_id,
                        "dataset": item.data_source,
                        "conversation": item.conversation_id,
                        "turn": item.message_seq,
                        "speaker": item.speaker_id,
                        "direction": job["direction"],
                        "arm": args.arm,
                        "contract_fingerprint": fingerprint,
                        "result": result,
                        "model_job": job}, ensure_ascii=False) + "\n")
            sheet = gold_labeling.build_sheet(items, _sheet_path(out, dataset, args.arm),
                                              results, ablate_direction=ablate)
            reviewed = sum(1 for result in results if result.get("needs_review"))
            print(f"    {elapsed:.1f}s  {len(items) / max(elapsed, 1e-9):.1f} it/s  "
                  f"errors={errors}  needs_review={reviewed}/{len(items)}", flush=True)
            print(f"    sheet -> {sheet}", flush=True)
    finally:
        client.close()
    print("The sheet pre-fills the model's own labels. Fill the human_* columns, "
          "then run `score`.", flush=True)
    return 0


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def command_score(args) -> int:
    out = Path(args.out).resolve()
    report = {"arm": args.arm, "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "contract_fingerprint": project_contract.contract_fingerprint(),
              "prefill_disclosure": ("the sheet pre-filled the model's labels, so the "
                                     "human labels are not fully independent of the model"),
              "datasets": {}}
    for dataset in args.datasets:
        sheet = _sheet_path(out, dataset, args.arm)
        if not sheet.is_file():
            print(f"no sheet at {sheet}", flush=True)
            continue
        rows, problems = gold_labeling.read_sheet(sheet, require_complete=not args.partial)
        gold = {row.item_id: row for row in rows}
        print(f"--- {dataset}: {len(rows)} labeled rows, {len(problems)} problems",
              flush=True)
        for problem in problems[:20]:
            print(f"    {problem}", flush=True)
        key_path = gold_labeling.save_answer_key(rows, out / f"answer-key-{dataset}-{args.arm}.jsonl")
        predictions = {}
        predictions_path = _predictions_path(out, dataset, args.arm)
        if predictions_path.is_file():
            with predictions_path.open(encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if line:
                        payload = json.loads(line)
                        predictions[payload["item_id"]] = payload["result"]
        result = gold_labeling.score(predictions, gold)
        result["problems"] = problems
        result["answer_key"] = str(key_path)
        report["datasets"][dataset] = result
        print(f"    scored {result['scored_items']} items", flush=True)
        for field, row in result["per_field"].items():
            if row["accuracy"] is None:
                continue
            print(f"      {field:<11} acc={row['accuracy']:.1%} "
                  f"[{row['wilson_low']:.1%}, {row['wilson_high']:.1%}]  "
                  f"majority={row['majority_class_baseline']:.1%}  n={row['total']}",
                  flush=True)
        if result["all_four_accuracy"] is not None:
            print(f"      all four     {result['all_four_accuracy']:.1%}", flush=True)
    path = out / f"score-{args.arm}.json"
    gold_sample.write_manifest(path, report)
    print(f"score report -> {path}", flush=True)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plan-a", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target):
        target.add_argument("--data-root", default=loaders.DATA_ROOT_DEFAULT)
        target.add_argument("--out", default=str(DEFAULT_OUT))
        target.add_argument("--project-root",
                            default=str(Path(__file__).resolve().parent.parent))

    sample = sub.add_parser("sample", help="build the stratified gold sample")
    common(sample)
    sample.add_argument("--n", type=int, default=100, help="items per dataset")
    sample.add_argument("--cped-split", default=CPED_SPLIT_DEFAULT)
    sample.add_argument("--stratify-by", default="emotion",
                        help="gold field used to spread the sample across classes")
    sample.add_argument("--include-multi-speaker", action="store_true",
                        help="also emit the multi-speaker MPDD study with direction=null")
    sample.set_defaults(func=command_sample)

    classify = sub.add_parser("classify", help="pre-label the sample and write the sheet")
    common(classify)
    classify.add_argument("--arm", choices=["synthetic", "null"], default="synthetic")
    classify.add_argument("--datasets", default="cped,mpdd")
    classify.add_argument("--limit", type=int, default=None)
    classify.add_argument("--slots", type=int, default=None)
    classify.add_argument("--context-size", type=int, default=16384)
    classify.add_argument("--cpu", action="store_true")
    classify.set_defaults(func=command_classify)

    score = sub.add_parser("score", help="score a filled sheet against predictions")
    common(score)
    score.add_argument("--arm", choices=["synthetic", "null"], default="synthetic")
    score.add_argument("--datasets", default="cped,mpdd")
    score.add_argument("--partial", action="store_true",
                       help="do not treat unlabeled rows as problems")
    score.set_defaults(func=command_score)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "datasets") and isinstance(args.datasets, str):
        args.datasets = [name.strip() for name in args.datasets.split(",") if name.strip()]
    try:
        return args.func(args)
    except (loaders.DatasetError, ModelUnavailable, gold_labeling.SheetError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
