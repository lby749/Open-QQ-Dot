"""Checkpointed offline first-label pass for the project's four-label task."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dataset_loaders as loaders
    import project_contract
else:
    from . import dataset_loaders as loaders
    from . import project_contract

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "datasets" / "annotations.sqlite3"
FIELDS = ("intent", "emotion", "tone", "initiative")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_fingerprint(specs) -> tuple[str, list[dict]]:
    sources = [{"path": str(path.resolve()), "sha256": _sha256(path)}
               for spec in specs for path in spec.files]
    payload = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest(), sources


def _model_identity() -> tuple[str, dict]:
    runtime_dir = PROJECT_ROOT / "build" / "message-classifier-runtime"
    runtime = json.loads((runtime_dir / "runtime.json").read_text("utf-8"))
    model_path = runtime_dir / runtime["model"]
    server_path = runtime_dir / runtime["server"]
    identity = {"runtime_version": runtime.get("version"),
                "model_repo": runtime.get("model_repo"),
                "model_revision": runtime.get("model_revision"),
                "base_model": runtime.get("base_model"),
                "base_model_observed_revision": runtime.get("base_model_observed_revision"),
                "model_file": runtime["model"], "model_sha256": _sha256(model_path),
                "server_file": runtime["server"], "server_sha256": _sha256(server_path),
                "generation": project_contract.GENERATION, "max_tokens": 180,
                "context": {"previous": 3, "current": 1, "following": 1}}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), identity


def _prepare(spec):
    items = loaders.load(spec, context_turns=0)
    by_conversation = defaultdict(list)
    for item in items:
        by_conversation[item.conversation_id].append(item)

    prepared = []
    for conversation, turns in by_conversation.items():
        speakers = list(dict.fromkeys(item.speaker_id for item in turns))
        # The corpus has no real in/out field. Keep multi-party and single-speaker
        # direction unknown; two-speaker identity mapping is explicitly synthetic.
        directions = ({speaker: "in" if i == 0 else "out"
                       for i, speaker in enumerate(speakers)} if len(speakers) == 2 else {})
        ordered = sorted(turns, key=lambda item: item.message_seq)
        for index, item in enumerate(ordered):
            context = [{"text": prior.text, "speaker": prior.speaker_id,
                        "seq": prior.message_seq,
                        "direction": directions.get(prior.speaker_id)}
                       for prior in ordered[max(0, index - 3):index]]
            following = []
            if index + 1 < len(ordered):
                nxt = ordered[index + 1]
                following = [{"text": nxt.text, "speaker": nxt.speaker_id,
                              "seq": nxt.message_seq,
                              "direction": directions.get(nxt.speaker_id)}]
            direction = directions.get(item.speaker_id)
            raw = item.prompt_row()
            raw["source_file"] = item.source_file
            raw["direction_policy"] = ("synthetic_two_speaker_identity" if directions
                                       else "unavailable_single_or_multi_speaker")
            prepared.append({"id": item.annotation_id, "conversation": conversation,
                             "split": item.split, "speaker": item.speaker_id,
                             "seq": item.message_seq, "text": item.text,
                             "direction": direction, "context": context,
                             "following": following, "gold": item.gold,
                             "pipeline_review": len(speakers) > 2,
                             "source_file": item.source_file,
                             "source_record_id": item.annotation_id,
                             "raw": raw})
    return prepared


def _init_db(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS runs(
        run_id TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL,
        contract_fingerprint TEXT NOT NULL, model_fingerprint TEXT NOT NULL,
        model_json TEXT NOT NULL, sources_json TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running');
      CREATE TABLE IF NOT EXISTS annotations(
        run_id TEXT NOT NULL, annotation_id TEXT NOT NULL,
        data_source TEXT NOT NULL, split TEXT NOT NULL, source_file TEXT NOT NULL,
        source_record_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        speaker_id TEXT NOT NULL, message_seq INTEGER NOT NULL, text TEXT NOT NULL,
        direction TEXT, direction_policy TEXT NOT NULL,
        context_json TEXT NOT NULL, following_json TEXT NOT NULL,
        gold_json TEXT NOT NULL, original_json TEXT NOT NULL,
        intent TEXT, emotion TEXT, tone TEXT, initiative TEXT,
        needs_review INTEGER, pipeline_needs_review INTEGER NOT NULL DEFAULT 0,
        reason TEXT, updated_at TEXT,
        PRIMARY KEY(run_id, annotation_id),
        FOREIGN KEY(run_id) REFERENCES runs(run_id));
      CREATE INDEX IF NOT EXISTS annotations_review
        ON annotations(run_id, needs_review);
    """)
    conn.commit()


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"another offline_feed process holds {path}") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"another offline_feed process holds {path}") from exc
        yield
    finally:
        lock.close()


def run(data_root=loaders.DATA_ROOT_DEFAULT, db_path=DEFAULT_DB,
        batch_size=32, limit=None, classifier_factory=None, progress=print):
    """Classify all discovered MPDD and CPED splits, or a bounded test prefix."""
    if not 1 <= batch_size <= 64:
        raise ValueError("batch_size must be between 1 and 64")
    specs = sorted(loaders.discover(data_root).values(),
                   key=lambda spec: (spec.name, spec.split))
    if not specs:
        raise loaders.DatasetError(f"no MPDD/CPED sources found under {data_root}")
    source_fp, sources = _source_fingerprint(specs)
    contract_fp = project_contract.contract_fingerprint()
    model_fp, model_info = _model_identity()
    run_id = hashlib.sha256((source_fp + contract_fp + model_fp +
                             f"|context=3+current+following1|direction=v1|limit={limit}").encode()).hexdigest()
    db_path = Path(db_path).resolve()
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if classifier_factory is None:
        sys.path.insert(0, str(PROJECT_ROOT))
        from message_classifier import LocalMessageClassifier
        classifier_factory = lambda: LocalMessageClassifier(project_root=PROJECT_ROOT)

    with _exclusive_lock(lock_path), closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        _init_db(conn)
        existing = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if existing and (existing["source_fingerprint"] != source_fp or
                         existing["contract_fingerprint"] != contract_fp or
                         existing["model_fingerprint"] != model_fp):
            raise RuntimeError("run fingerprint mismatch; refusing resume")
        conn.execute("INSERT OR IGNORE INTO runs"
                     "(run_id,source_fingerprint,contract_fingerprint,model_fingerprint,model_json,sources_json,created_at)"
                     " VALUES(?,?,?,?,?,?,?)", (run_id, source_fp, contract_fp, model_fp,
                                             json.dumps(model_info, ensure_ascii=False),
                                             json.dumps(sources, ensure_ascii=False),
                                             time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        conn.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        conn.commit()
        insert_sql = """INSERT OR IGNORE INTO annotations
          (run_id,annotation_id,data_source,split,source_file,source_record_id,
           conversation_id,speaker_id,message_seq,text,direction,direction_policy,
           context_json,following_json,gold_json,original_json,pipeline_needs_review)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        remaining = limit
        for spec in specs:
            rows = _prepare(spec)
            if remaining is not None:
                rows = rows[:remaining]
            conn.executemany(insert_sql,
          ((run_id, r["id"], r["raw"]["data_source"], r["split"], r["source_file"],
            r["source_record_id"], r["conversation"], r["speaker"], r["seq"], r["text"],
            r["direction"], r["raw"]["direction_policy"],
            json.dumps(r["context"], ensure_ascii=False),
            json.dumps(r["following"], ensure_ascii=False),
            json.dumps(r["gold"], ensure_ascii=False), json.dumps(r["raw"], ensure_ascii=False),
            int(r["pipeline_review"])) for r in rows))
            conn.commit()
            if remaining is not None:
                remaining -= len(rows)
                if remaining <= 0:
                    break
        total = conn.execute("SELECT COUNT(*) FROM annotations WHERE run_id=?", (run_id,)).fetchone()[0]
        processed = conn.execute("SELECT COUNT(*) FROM annotations WHERE run_id=? AND intent IS NOT NULL",
                                 (run_id,)).fetchone()[0]
        uncertain = conn.execute("SELECT COUNT(*) FROM annotations WHERE run_id=? "
                                  "AND intent IS NOT NULL "
                                  "AND (needs_review=1 OR pipeline_needs_review=1)",
                                 (run_id,)).fetchone()[0]
        start = time.monotonic()
        session_processed = 0
        model = classifier_factory()
        try:
            while True:
                batch = conn.execute("""SELECT * FROM annotations
                    WHERE run_id=? AND intent IS NULL ORDER BY data_source,split,conversation_id,message_seq
                    LIMIT ?""", (run_id, batch_size)).fetchall()
                if not batch:
                    break
                results = model.classify_batch_with_review(
                    [r["text"] for r in batch],
                    directions=[r["direction"] for r in batch],
                    contexts=[json.loads(r["context_json"]) for r in batch],
                    following_contexts=[json.loads(r["following_json"]) for r in batch])
                now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                for row, result in zip(batch, results):
                    labels = result["labels"]
                    conn.execute("""UPDATE annotations SET intent=?,emotion=?,tone=?,initiative=?,
                      needs_review=?,reason=?,updated_at=? WHERE run_id=? AND annotation_id=?""",
                                 (labels["intent"], labels["emotion"], labels["tone"],
                                  labels["initiative"], int(result["needs_review"]), result["reason"],
                                  now, run_id, row["annotation_id"]))
                conn.commit()
                processed += len(batch)
                session_processed += len(batch)
                uncertain += sum(bool(result["needs_review"]) or bool(row["pipeline_needs_review"])
                                 for row, result in zip(batch, results))
                elapsed = max(0.001, time.monotonic() - start)
                progress(f"processed={processed}/{total} uncertain={uncertain} "
                         f"rate={session_processed / elapsed:.2f}/s")
        finally:
            model.close()
        done = processed == total
        if done:
            conn.execute("UPDATE runs SET status='complete' WHERE run_id=?", (run_id,))
        conn.commit()
    return {"run_id": run_id, "total": total, "processed": processed,
            "uncertain": uncertain, "db": str(db_path), "fingerprint": contract_fp,
            "model_fingerprint": model_fp, "model": model_info}


def export_review_queue(db_path=DEFAULT_DB, run_id=None, output=None):
    if run_id is None:
        raise ValueError("review export requires an explicit run_id")
    db_path = Path(db_path).resolve()
    output = Path(output or db_path.with_name("review-queue.jsonl"))
    with closing(sqlite3.connect(db_path)) as conn, output.open("w", encoding="utf-8") as stream:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM annotations WHERE run_id=? "
                                    "AND (needs_review=1 OR pipeline_needs_review=1) "
                                    "ORDER BY data_source,split,conversation_id,message_seq", (run_id,)):
            payload = dict(row)
            payload["context"] = json.loads(payload.pop("context_json"))
            payload["following"] = json.loads(payload.pop("following_json"))
            payload["gold"] = json.loads(payload.pop("gold_json"))
            payload["original"] = json.loads(payload.pop("original_json"))
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=loaders.DATA_ROOT_DEFAULT)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int, help="bounded test run only")
    parser.add_argument("--export-review", action="store_true")
    parser.add_argument("--run-id", help="required with --export-review")
    args = parser.parse_args(argv)
    try:
        if args.export_review:
            print(export_review_queue(args.db, run_id=args.run_id), flush=True)
            return 0
        result = run(args.data_root, args.db, args.batch_size, args.limit)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["processed"] == result["total"] and not args.limit:
            output = export_review_queue(args.db, run_id=result["run_id"])
            marker = Path(args.db).resolve().with_name("COMPLETE.json")
            marker.write_text(json.dumps({**result, "review_queue": str(output)},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"complete marker: {marker}", flush=True)
        return 0
    except (RuntimeError, loaders.DatasetError, ValueError) as exc:
        print(f"offline_feed: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
