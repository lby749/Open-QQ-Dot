"""Validate our label inventories against the dataset authors' own constants.

CPED ships its label vocabularies as Python constants in
``pec_baseline/utils/cped_util.py``. Those constants, not the README prose, are
the authoritative spelling of every label, so this script fetches that file and
compares it with ``annotate_labels``. MPDD declares its vocabularies in the
downloaded ``metadata.json`` and is compared against that file.

Run:
    python dataset_annotation/validate_labels.py [--data-root E:\\qwen35-nlp\\data]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import annotate_labels as labels  # noqa: E402
import dataset_loaders as loaders  # noqa: E402

CPED_UTIL_URL = ("https://raw.githubusercontent.com/scutcyr/CPED/main/"
                 "pec_baseline/utils/cped_util.py")
WANTED = ("CPED_DA_TO_TOKENS", "CPED_EMOTION_TO_TOKENS", "CPED_SENTIMENT_TO_TOKENS",
          "CPED_GENDER_TO_ID", "CPED_BIGFIVE_TO_ID")


def fetch_official_cped() -> dict[str, list[str]]:
    """Extract the authors' label constants from the official module source."""
    request = urllib.request.Request(CPED_UTIL_URL,
                                     headers={"User-Agent": "label-validate/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        source = response.read().decode("utf-8", errors="replace")
    found: dict[str, list[str]] = {}
    for name in WANTED:
        pattern = re.compile(rf"^{name}\s*=\s*(\{{.*?\}})", re.S | re.M)
        match = pattern.search(source)
        if not match:
            raise RuntimeError(f"official constant {name} not found in cped_util.py")
        # Parsing the literal (not exec'ing it) keeps this read-only and safe.
        value = ast.literal_eval(match.group(1))
        found[name] = list(value)
    return found


def compare(name: str, ours, theirs, ok: bool, notes: list[str]) -> None:
    ours_list, theirs_list = list(ours), list(theirs)
    status = "OK  " if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        notes.append(name)
        print(f"         ours   ({len(ours_list)}): {ours_list}")
        print(f"         theirs ({len(theirs_list)}): {theirs_list}")
        print(f"         only ours:   {sorted(set(ours_list) - set(theirs_list))}")
        print(f"         only theirs: {sorted(set(theirs_list) - set(ours_list))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default=loaders.DATA_ROOT_DEFAULT)
    args = parser.parse_args()
    failures: list[str] = []

    print("CPED — comparing against pec_baseline/utils/cped_util.py")
    official = fetch_official_cped()
    compare("CPED_EMOTION_TO_TOKENS", labels.CPED_EMOTION, official["CPED_EMOTION_TO_TOKENS"],
            set(labels.CPED_EMOTION) == set(official["CPED_EMOTION_TO_TOKENS"]), failures)
    compare("CPED_DA_TO_TOKENS", labels.CPED_DA, official["CPED_DA_TO_TOKENS"],
            set(labels.CPED_DA) == set(official["CPED_DA_TO_TOKENS"]), failures)
    compare("CPED_SENTIMENT_TO_TOKENS", labels.CPED_SENTIMENT,
            official["CPED_SENTIMENT_TO_TOKENS"],
            set(labels.CPED_SENTIMENT) == set(official["CPED_SENTIMENT_TO_TOKENS"]), failures)
    compare("CPED_GENDER_TO_ID", labels.CPED_GENDER, official["CPED_GENDER_TO_ID"],
            set(labels.CPED_GENDER) == set(official["CPED_GENDER_TO_ID"]), failures)
    compare("CPED_BIGFIVE_TO_ID", ("low", "unknown", "high"), official["CPED_BIGFIVE_TO_ID"],
            set(official["CPED_BIGFIVE_TO_ID"]) == {"low", "unknown", "high"}, failures)

    print("\nCPED — CSV scan (the values actually present in the three splits)")
    specs = loaders.discover(args.data_root)
    if any(spec.name == "cped" for spec in specs.values()):
        import csv
        import collections
        seen: dict[str, set] = {key: set() for key in ("Emotion", "DA", "Sentiment", "Scene")}
        for key, spec in sorted(specs.items()):
            if spec.name != "cped":
                continue
            with spec.files[0].open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    for column in seen:
                        seen[column].add(row[column])
        compare("CSV Emotion vs official", seen["Emotion"], official["CPED_EMOTION_TO_TOKENS"],
                seen["Emotion"] == set(official["CPED_EMOTION_TO_TOKENS"]), failures)
        compare("CSV DA vs official", seen["DA"], official["CPED_DA_TO_TOKENS"],
                seen["DA"] == set(official["CPED_DA_TO_TOKENS"]), failures)
        compare("CSV Sentiment vs official", seen["Sentiment"],
                official["CPED_SENTIMENT_TO_TOKENS"],
                seen["Sentiment"] == set(official["CPED_SENTIMENT_TO_TOKENS"]), failures)
        compare("CSV Scene vs our list", seen["Scene"], labels.CPED_SCENE,
                seen["Scene"] == set(labels.CPED_SCENE), failures)
    else:
        print("  (no CPED data under the data root; skipped CSV scan)")

    print("\nMPDD — comparing against metadata.json")
    mpdd = loaders.find_mpdd(Path(args.data_root))
    if mpdd:
        metadata = json.loads(mpdd[1].read_text("utf-8"))
        compare("MPDD emotion", labels.MPDD_EMOTION, metadata["emotion"],
                set(labels.MPDD_EMOTION) == set(metadata["emotion"]), failures)
        compare("MPDD relation", labels.MPDD_RELATION, metadata["relation"],
                set(labels.MPDD_RELATION) == set(metadata["relation"]), failures)
        field_ok = all(set(members) <= set(metadata["relation"])
                       for members in metadata["field"].values())
        compare("MPDD field groups are subsets of relation",
                [item for members in metadata["field"].values() for item in members],
                metadata["relation"], field_ok, failures)
        position_ok = all(set(members) <= set(metadata["relation"])
                          for members in metadata["position"].values())
        compare("MPDD position groups are subsets of relation",
                [item for members in metadata["position"].values() for item in members],
                metadata["relation"], position_ok, failures)
    else:
        print("  (no MPDD data under the data root; skipped)")

    print("\nAlias handling (a README spelling must match the CSV spelling)")
    for readme_form, csv_form, universe in (
            ("angry", "anger", labels.CPED_EMOTION),
            ("surprised", "astonished", labels.CPED_EMOTION),
            ("other-positive", "positive-other", labels.CPED_EMOTION),
            ("agreement/acceptance", "agreement", labels.CPED_DA),
            ("other-scene", "other-venue", labels.CPED_SCENE)):
        ok = labels.matched(readme_form, csv_form, universe)
        if not ok:
            failures.append(f"alias {readme_form}->{csv_form}")
        print(f"  [{'OK  ' if ok else 'FAIL'}] {readme_form!r} matches {csv_form!r}")

    print("\nGloss tables are inferred, not authoritative")
    sources = {
        "CPED_EMOTION_ZH": len(labels.CPED_EMOTION_ZH),
        "CPED_DA_ZH": len(labels.CPED_DA_ZH),
        "CPED_SCENE_ZH": len(labels.CPED_SCENE_ZH),
        "MPDD_EMOTION_ZH": len(labels.MPDD_EMOTION_ZH),
        "MPDD_RELATION_ZH": len(labels.MPDD_RELATION_ZH),
    }
    print(f"  {sources} — the dataset repositories ship no Chinese label text,")
    print("  so these are our own readings and must not be presented as official.")

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)}): {failures}")
        return 1
    print("RESULT: all label inventories match the datasets' own definitions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
