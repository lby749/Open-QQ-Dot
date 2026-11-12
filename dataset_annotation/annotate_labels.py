"""Label inventories and enum handling for the two datasets.

Authority
---------
Every ENUM below is verified against the dataset authors' own machine-readable
definition, not against their README prose:

* CPED -> ``pec_baseline/utils/cped_util.py`` constants (CPED_DA_TO_TOKENS,
  CPED_EMOTION_TO_TOKENS, CPED_SENTIMENT_TO_TOKENS, CPED_GENDER_TO_ID,
  CPED_BIGFIVE_TO_ID), cross-checked against a full scan of the three split
  CSVs. Run ``validate_labels.py`` to re-verify.
* MPDD -> ``metadata.json`` inside mpdd.zip (its ``emotion`` and ``relation``
  lists), cross-checked against every utterance in dialogue.json.

The ``*_ZH`` tables are NOT authoritative. Neither repository ships any Chinese
label text, so those strings are our own reading of the English label names.
They are therefore excluded from prompts by default (see
``annotate_tasks.Task.system_prompt(glosses=False)``); pass ``--gloss`` to opt
in, and read any resulting score as a property of that glossed prompt, not of
the official task.
"""
from __future__ import annotations

import re

# The CPED repository ships TWO spellings for some labels. The CSV data (and the
# official constants in pec_baseline/utils/cped_util.py) use the canonical forms
# below; the README table prints looser ones (angry / sad / feared / depressed /
# disgusted / astonished / other-positive / other-negative / agreement-acceptance
# / other-scene). A prediction written in either spelling is the same label.
#
# Only the non-canonical README spelling is a key, so canonical() is idempotent.
# These are exactly the CPED README forms; MPDD needs none of them.
ALIASES = {
    "angry": "anger", "sad": "sadness", "feared": "fear", "depressed": "depress",
    "disgusted": "disgust", "surprised": "astonished",
    "other-positive": "positive-other", "other-negative": "negative-other",
    "agreement/acceptance": "agreement", "other-scene": "other-venue",
}

# --------------------------------------------------------------------------- #
# CPED (scutcyr/CPED)
# --------------------------------------------------------------------------- #
# The values below are the ones actually present in data/CPED/*_split.csv,
# verified by scanning all three splits. The repository README prints a few
# looser names (angry / other-negative / depressed / sad / feared / disgusted /
# other-positive / agreement-acceptance); those are kept as accepted aliases so
# a prediction written either way still matches, but the CSV form is canonical.
CPED_SENTIMENT = ("positive", "neutral", "negative")
CPED_SENTIMENT_ZH = {"positive": "positive", "neutral": "neutral", "negative": "negative"}

CPED_EMOTION = (
    "positive-other", "happy", "grateful", "relaxed", "neutral", "anger", "sadness",
    "fear", "depress", "disgust", "astonished", "worried", "negative-other",
)
# --------------------------------------------------------------------------- #
# Inferred English readings -- NOT part of any official definition.
# --------------------------------------------------------------------------- #
# Both repositories define their labels in English only. These tables are our
# own paraphrase, kept for human-facing reports and for the opt-in --gloss
# prompt variant. They must never be described as the dataset's label meanings,
# and the default run does not send them to the model.
CPED_EMOTION_ZH = {
    "positive-other": "other positive", "happy": "happy", "grateful": "grateful", "relaxed": "relaxed",
    "neutral": "neutral", "anger": "anger", "sadness": "sadness", "fear": "fear",
    "depress": "depressed", "disgust": "disgust", "astonished": "astonished",
    "worried": "worried", "negative-other": "other negative",
}

CPED_DA = (
    "greeting", "question", "answer", "statement-opinion", "statement-non-opinion",
    "apology", "command", "agreement", "disagreement", "acknowledge", "appreciation",
    "interjection", "conventional-closing", "thanking", "quotation", "reject",
    "irony", "comfort", "other",
)
CPED_DA_ZH = {
    "greeting": "greeting", "question": "question", "answer": "answer",
    "statement-opinion": "statement of opinion", "statement-non-opinion": "statement of fact",
    "apology": "apology", "command": "command or request", "agreement": "agreement or acceptance",
    "disagreement": "disagreement", "acknowledge": "acknowledgment", "appreciation": "appreciation",
    "interjection": "interjection", "conventional-closing": "closing", "thanking": "thanking",
    "quotation": "quotation", "reject": "rejection", "irony": "irony", "comfort": "comfort",
    "other": "other",
}
# Short codes printed in the README table, kept only to enrich the prompt.
CPED_DA_CODE = {
    "greeting": "g", "question": "q", "answer": "ans", "statement-opinion": "sv",
    "statement-non-opinion": "sd", "apology": "fa", "command": "c",
    "agreement": "aa", "disagreement": "dag", "acknowledge": "a", "appreciation": "ba",
    "interjection": "ij", "conventional-closing": "fc", "thanking": "ft",
    "quotation": "^q", "reject": "rj", "irony": "ir", "comfort": "cf", "other": "oth",
}
CPED_SCENE = (
    "home", "office", "school", "mall", "hospital", "restaurant", "sports-venue",
    "entertainment-venue", "car", "outdoor", "other-venue",
)
CPED_SCENE_ZH = {
    "home": "home", "office": "office", "school": "school", "mall": "shopping mall",
    "hospital": "hospital", "restaurant": "restaurant", "sports-venue": "sports venue",
    "entertainment-venue": "entertainment venue", "car": "car interior", "outdoor": "outdoors",
    "other-venue": "other venue",
}
CPED_GENDER = ("male", "female", "unknown")
CPED_AGE = ("children", "teenager", "young", "middle-aged", "elderly", "unknown")
CPED_BIG_FIVE = ("high", "low", "unknown")

# --------------------------------------------------------------------------- #
# MPDD (ntunlplab/Dialogue-MPDD)
# --------------------------------------------------------------------------- #
# metadata.json declares exactly these vocabularies; dialogue.json uses all of
# them (verified across the whole corpus). Note the corpus is Traditional
# Chinese and the labels are English words.
MPDD_EMOTION = ("fear", "angry", "disgust", "sadness", "happiness", "surprise", "neutral")
MPDD_EMOTION_ZH = {
    "fear": "fear", "angry": "angry", "disgust": "disgust", "sadness": "sadness",
    "happiness": "happiness", "surprise": "surprise", "neutral": "neutral",
}
MPDD_RELATION = (
    "parent", "parent-in-law", "grandparent", "other superior", "spouse",
    "brothers and sisters", "other peer", "child", "son/daughter-in-law",
    "grandchild", "other inferior", "teacher", "classmate", "student", "boss",
    "colleague", "subordinate", "partner", "couple", "friend", "enemy",
    "consignee", "consignor", "stranger", "unknown",
)
MPDD_RELATION_ZH = {
    "parent": "parent", "parent-in-law": "parent-in-law", "grandparent": "grandparent",
    "other superior": "other superior", "spouse": "spouse", "brothers and sisters": "siblings",
    "other peer": "other peer", "child": "child", "son/daughter-in-law": "son/daughter-in-law",
    "grandchild": "grandchild", "other inferior": "other inferior", "teacher": "teacher",
    "classmate": "classmate", "student": "student", "boss": "boss", "colleague": "colleague",
    "subordinate": "subordinate", "partner": "partner", "couple": "couple", "friend": "friend",
    "enemy": "enemy", "consignee": "consignee", "consignor": "consignor",
    "stranger": "stranger", "unknown": "unknown",
}
# metadata.json also groups relations by field and by relative position.
MPDD_FIELD = {
    "family": ["parent", "parent-in-law", "grandparent", "other superior", "spouse",
               "brothers and sisters", "other peer", "child", "son/daughter-in-law",
               "grandchild", "other inferior"],
    "school": ["teacher", "classmate", "student"],
    "company": ["boss", "colleague", "subordinate", "partner"],
    "others": ["couple", "friend", "enemy", "consignee", "consignor", "stranger", "unknown"],
}
MPDD_POSITION = {
    "superior": ["parent", "parent-in-law", "grandparent", "other superior", "teacher", "boss"],
    "peer": ["spouse", "brothers and sisters", "other peer", "classmate", "colleague",
             "partner", "couple", "friend", "enemy", "consignee", "consignor", "stranger", "unknown"],
    "inferior": ["child", "son/daughter-in-law", "grandchild", "other inferior", "student",
                 "subordinate"],
}



def normalize_label(value: str) -> str:
    """Normalize a dataset label for comparison without changing its meaning."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower().replace("_", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def gloss(label: str, table: dict[str, str]) -> str:
    """English gloss of a dataset label, or the label itself when unknown."""
    normalized = normalize_label(label)
    if normalized in table:
        return table[normalized]
    for key, value in table.items():
        if normalize_label(key) == normalized:
            return value
    return label.strip()


def enum_block(values, descriptions=None, codes=None) -> str:
    """Render an enum as ``value (description)`` items for a prompt."""
    items = []
    for label in values:
        if descriptions:
            items.append(f"{label} ({gloss(label, descriptions)})")
        elif codes and label in codes:
            items.append(f"{label} ({codes[label]})")
        else:
            items.append(str(label))
    return ", ".join(items)


def canonical(value: str) -> str:
    """Normalize a label and fold a dataset alias onto its canonical spelling."""
    text = normalize_label(value)
    return normalize_label(ALIASES.get(text, text))


def matched(prediction, gold, universe=None) -> bool:
    """Compare a prediction with gold, tolerating case/space/underscore noise.

    ``universe`` is the dataset's own label set. When supplied, every member of
    it is indexed under BOTH its canonical and its alias spelling, so a
    prediction written the README way (``angry``) still matches the CSV way
    (``anger``).
    """
    left, right = canonical(prediction), canonical(gold)
    if not left or not right:
        return False
    if left == right:
        return True
    if universe is None:
        return False
    index: dict[str, str] = {}
    for item in universe:
        target = canonical(item)
        index[normalize_label(item)] = target
        index[target] = target
    return bool(index.get(left)) and index.get(left) == index.get(right)


def in_universe(value: str, universe) -> bool:
    """Whether a prediction is expressible in the dataset's label set."""
    text = normalize_label(value)
    if not text:
        return False
    return any(text in (normalize_label(item), canonical(item)) for item in universe)
