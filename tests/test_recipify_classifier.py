"""Recipify keyword classifier — v7 Phase G."""
from __future__ import annotations

import pytest

from app.recipify import CANONICAL_CATEGORIES, classify_skill


def test_scraping_classifies_as_data():
    text = "Web scraping pipeline with proxy rotation and ETL into a warehouse."
    out = classify_skill(text)
    assert out["category"] == "data"
    assert 3 <= len(out["tags"]) <= 5


def test_pr_review_classifies_as_code_review():
    text = "GitHub PR review automation with lint and static analysis."
    out = classify_skill(text)
    assert out["category"] == "code-review"


def test_marketing_email_in_marketing_or_content():
    text = "Marketing email generation for SEO lead-gen newsletter campaigns."
    out = classify_skill(text)
    assert out["category"] in {"marketing", "content"}


def test_devops_classifies_as_ops():
    text = "Kubernetes deploy pipeline with terraform and CI/CD monitoring."
    out = classify_skill(text)
    assert out["category"] == "ops"


def test_returned_category_is_always_canonical():
    samples = [
        "completely unrelated text about random nothing",
        "",
        "client deliverable proposal scoping",
        "personal calendar email notes",
        "research literature review of recent papers",
    ]
    for text in samples:
        out = classify_skill(text)
        assert out["category"] in CANONICAL_CATEGORIES, (text, out)


def test_tags_are_3_to_5():
    out = classify_skill("Code review automation tooling for engineering teams.")
    assert 3 <= len(out["tags"]) <= 5
