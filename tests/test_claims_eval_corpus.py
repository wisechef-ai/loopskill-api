"""Measured claims-gate accuracy on an independent, labelled corpus (ratchet).

tests/fixtures/claims_eval_corpus.jsonl holds natural marketing sentences
written by two different models (gpt-6-luna, claude-sonnet) from the public
ground truth, each labelled true / false with a claim category, then audited
by hand. Adversarial review finds bypasses one sentence at a time; this corpus
measures whole categories (recall on false claims, false-positive rate on
true copy).

RATCHET: every row must get the verdict its label demands, EXCEPT the rows in
claims_eval_known_gaps.json. That list may only shrink:
- a new wrong verdict (a regression, or a new corpus row the gate misses) fails;
- a known gap the gate now gets right fails until its row is removed, so a fix
  can never silently regress later.
Grow the corpus with every incident: add the real sentence with its label.

HELD-OUT: each round, generate a fresh batch (tests/fixtures/
claims_eval_gen_prompt_holdout.md, different models), score it ONCE before
any fix and report that number, then append it here with its "batch" tag.
holdout-1 (gpt-6-luna + claude-haiku, 418 rows) scored recall 85.3% /
false positives 4.6% against rules tuned on corpus-1 (97.0% / 0.9%).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.services import claims_contract as cc

_FIX = Path(__file__).parent / "fixtures"
ROWS = [
    json.loads(line) for line in (_FIX / "claims_eval_corpus.jsonl").read_text().splitlines() if line.strip()
]
KNOWN_GAPS = set(json.loads((_FIX / "claims_eval_known_gaps.json").read_text())["known_gaps"])

# Floors a little under the measurement; raise them as gaps close.
RECALL_FLOOR = 0.90
FP_CEILING = 0.03


def _flagged(text: str) -> bool:
    return bool(cc.check_text(text))


def _wrong() -> list[dict]:
    return [r for r in ROWS if _flagged(r["text"]) != (r["label"] == "false")]


def test_corpus_is_well_formed() -> None:
    assert len(ROWS) >= 600
    assert {r["label"] for r in ROWS} == {"true", "false"}
    texts = [r["text"] for r in ROWS]
    assert len(texts) == len(set(texts)), "duplicate corpus rows"
    assert KNOWN_GAPS <= set(texts), "known gap that is not a corpus row"


def test_no_new_wrong_verdicts() -> None:
    new = [f"{r['label']}/{r['category']}: {r['text']}" for r in _wrong() if r["text"] not in KNOWN_GAPS]
    assert not new, "gate verdict regressed or a new row is missed:\n" + "\n".join(new)


def test_known_gaps_only_shrink() -> None:
    wrong = {r["text"] for r in _wrong()}
    fixed = sorted(KNOWN_GAPS - wrong)
    assert not fixed, "now handled: remove from claims_eval_known_gaps.json:\n" + "\n".join(fixed)


def test_accuracy_floor() -> None:
    false_rows = [r for r in ROWS if r["label"] == "false"]
    true_rows = [r for r in ROWS if r["label"] == "true"]
    recall = sum(_flagged(r["text"]) for r in false_rows) / len(false_rows)
    fp_rate = sum(_flagged(r["text"]) for r in true_rows) / len(true_rows)
    by_cat = Counter(r["category"] for r in false_rows if not _flagged(r["text"]))
    assert recall >= RECALL_FLOOR, (
        f"recall {recall:.1%} < {RECALL_FLOOR:.0%}; misses by category {dict(by_cat)}"
    )
    assert fp_rate <= FP_CEILING, f"false-positive rate {fp_rate:.1%} > {FP_CEILING:.0%}"


def test_prefilter_never_changes_a_verdict(monkeypatch) -> None:
    """The amount-rule "requires" prefilter is a pure speed-up."""
    on = [sorted((v["rule_id"], v["match"]) for v in cc.check_text(r["text"])) for r in ROWS]
    monkeypatch.setattr(cc, "PREFILTER", False)
    off = [sorted((v["rule_id"], v["match"]) for v in cc.check_text(r["text"])) for r in ROWS]
    assert on == off
