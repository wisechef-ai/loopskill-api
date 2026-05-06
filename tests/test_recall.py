"""v7 Phase E — recall endpoint tests.

Covers:
  * unit tests for ranking, tier-gating, install_status, and decoders
  * a 50-query held-out eval set with a hard ≥70% top-3 accuracy gate

Eval methodology
----------------
We seed a fixed 25-skill catalog spanning all 10 canonical categories. For
each skill we author two paraphrased natural-language queries (50 total).
The recall service must return the seed skill in the top-3 hits for at
least 70% of queries — otherwise the test fails AND `RECALL_QUALITY.md`
must explain the divergence.

Both the catalog and the queries are deterministic (committed to this
file, no random seed at run-time).
"""

from __future__ import annotations

import json

import pytest

from app.recall_routes import recall_skills, _decode_embedding
from app.embeddings import embed_skill, embed_text, cosine
from app.ranking import combine, score_bm25, score_vector


# ── Catalog fixture: 25 canonical skills across 10 categories ──────────────

# Each entry: slug, title, description, related_skills (tags), tier, category
SEED_CATALOG = [
    # research
    ("paper-summariser", "Academic paper summariser",
     "Reads PDFs of academic papers and produces structured TLDR summaries with citations.",
     ["research", "summarisation", "pdf"], "free", "research"),
    ("literature-search", "Scholarly literature search",
     "Searches Google Scholar and arXiv for papers matching a topic and ranks by citation count.",
     ["research", "scholar", "arxiv"], "cook", "research"),
    # dev-tools
    ("python-formatter", "Python code formatter",
     "Formats Python source files using black and ruff with project defaults.",
     ["python", "formatter", "lint"], "free", "dev-tools"),
    ("git-rebase-helper", "Git rebase assistant",
     "Walks an interactive git rebase explaining merge conflicts and suggesting resolutions.",
     ["git", "rebase", "vcs"], "free", "dev-tools"),
    # agency
    ("client-onboarding", "Marketing agency client onboarding",
     "Generates a kickoff document, brand brief, and intake survey for a new agency client.",
     ["agency", "onboarding", "client"], "cook", "agency"),
    ("retainer-report", "Monthly retainer report builder",
     "Aggregates marketing channel results into a polished monthly retainer report PDF.",
     ["agency", "report", "retainer"], "operator", "agency"),
    # marketing
    ("seo-audit", "Website SEO audit",
     "Crawls a website and produces an SEO audit covering meta tags, schema, and core web vitals.",
     ["seo", "audit", "marketing"], "free", "marketing"),
    ("ad-copy-generator", "Search ad copy generator",
     "Creates Google and Meta search ad headlines and descriptions from a product brief.",
     ["ads", "copy", "marketing"], "cook", "marketing"),
    # content
    ("blog-outline", "Blog post outline writer",
     "Generates an SEO-aware blog post outline with H2 headings and target keywords.",
     ["content", "blog", "writing"], "free", "content"),
    ("video-script", "YouTube video script writer",
     "Drafts hook-first YouTube video scripts from a topic and target audience.",
     ["content", "video", "youtube"], "cook", "content"),
    # automation
    ("zapier-builder", "Zapier workflow builder",
     "Designs multi-step Zapier zaps from a description of business inputs and outputs.",
     ["automation", "zapier", "workflow"], "cook", "automation"),
    ("email-router", "Inbox email router",
     "Triages incoming emails into folders by sender, intent, and urgency.",
     ["automation", "email", "triage"], "free", "automation"),
    # code-review
    ("pr-reviewer", "Pull request reviewer",
     "Reviews GitHub pull requests for bugs, style violations, and missing tests.",
     ["code-review", "github", "pr"], "cook", "code-review"),
    ("security-scanner", "Source code security scanner",
     "Scans a repository for known security antipatterns (XSS, SQLi, secret leaks).",
     ["code-review", "security", "scan"], "operator", "code-review"),
    # productivity
    ("calendar-cleaner", "Calendar cleaner",
     "Audits a calendar for low-value recurring meetings and suggests deletions.",
     ["productivity", "calendar", "meetings"], "free", "productivity"),
    ("inbox-zero", "Inbox zero coach",
     "Walks a user through processing their inbox to zero with reply, archive, and snooze.",
     ["productivity", "email", "inbox"], "cook", "productivity"),
    # data
    ("web-scraper", "Web scraper",
     "Scrapes structured data from arbitrary websites using CSS selectors and pagination.",
     ["data", "scraping", "web"], "free", "data"),
    ("csv-cleaner", "CSV data cleaner",
     "Cleans and normalises messy CSV files: deduping, type coercion, header repair.",
     ["data", "csv", "cleaning"], "free", "data"),
    ("sql-query-writer", "SQL query writer",
     "Writes parameterised SQL queries for analytic questions against a known schema.",
     ["data", "sql", "analytics"], "cook", "data"),
    # ops
    ("terraform-helper", "Terraform helper",
     "Authors and lints Terraform modules for AWS, GCP, and Azure infrastructure.",
     ["ops", "terraform", "infrastructure"], "operator", "ops"),
    ("docker-builder", "Docker image builder",
     "Builds optimised multi-stage Dockerfiles from a project description.",
     ["ops", "docker", "containers"], "free", "ops"),
    ("k8s-debugger", "Kubernetes debugger",
     "Debugs failing Kubernetes pods by inspecting events, logs, and resource limits.",
     ["ops", "kubernetes", "debug"], "operator", "ops"),
    ("incident-postmortem", "Incident postmortem writer",
     "Drafts a blameless incident postmortem from a Slack timeline and an outage summary.",
     ["ops", "incident", "postmortem"], "operator", "ops"),
    # extras to round to 25
    ("invoice-generator", "Invoice generator",
     "Generates invoices in PDF and tracks unpaid status for freelancers and agencies.",
     ["agency", "invoice", "billing"], "cook", "agency"),
    ("twitter-thread", "Twitter thread writer",
     "Turns a long-form essay into a Twitter / X thread with a strong hook tweet.",
     ["content", "twitter", "social"], "free", "content"),
]


# ── 50-query held-out eval set ─────────────────────────────────────────────

# (query, expected_slug). Two queries per skill — paraphrases that don't
# contain the slug verbatim where possible.
EVAL_QUERIES: list[tuple[str, str]] = [
    ("summarise this academic paper for me", "paper-summariser"),
    ("tldr a research PDF with citations", "paper-summariser"),
    ("find scientific papers about a topic", "literature-search"),
    ("search arxiv for relevant studies", "literature-search"),
    ("format my python source code", "python-formatter"),
    ("run black and ruff on a python project", "python-formatter"),
    ("help me with an interactive git rebase", "git-rebase-helper"),
    ("resolve git merge conflicts step by step", "git-rebase-helper"),
    ("kickoff brief for a new agency client", "client-onboarding"),
    ("intake survey for onboarding a marketing client", "client-onboarding"),
    ("monthly report for a marketing retainer", "retainer-report"),
    ("aggregate channel performance into a report", "retainer-report"),
    ("audit a website for SEO problems", "seo-audit"),
    ("check meta tags and core web vitals on a site", "seo-audit"),
    ("write google ads headlines and descriptions", "ad-copy-generator"),
    ("generate search ad copy from a product brief", "ad-copy-generator"),
    ("draft an outline for a blog post", "blog-outline"),
    ("seo blog post structure with H2s", "blog-outline"),
    ("write a script for a youtube video", "video-script"),
    ("hook-first video script for youtube", "video-script"),
    ("build a multi-step zapier workflow", "zapier-builder"),
    ("design an automation in zapier", "zapier-builder"),
    ("triage my inbox into folders automatically", "email-router"),
    ("route emails by sender and intent", "email-router"),
    ("review a github pull request for bugs", "pr-reviewer"),
    ("check a PR for missing tests and style issues", "pr-reviewer"),
    ("scan code for security vulnerabilities", "security-scanner"),
    ("look for SQL injection and XSS in a repo", "security-scanner"),
    ("find low-value recurring meetings in my calendar", "calendar-cleaner"),
    ("clean up a noisy calendar", "calendar-cleaner"),
    ("get to inbox zero with a coach", "inbox-zero"),
    ("process my email inbox down to zero", "inbox-zero"),
    ("scrape data from a website", "web-scraper"),
    ("extract structured info from arbitrary web pages", "web-scraper"),
    ("clean a messy csv file", "csv-cleaner"),
    ("dedupe and normalise a csv dataset", "csv-cleaner"),
    ("write a SQL query for an analytics question", "sql-query-writer"),
    ("parameterised SQL against a schema", "sql-query-writer"),
    ("write a terraform module for AWS", "terraform-helper"),
    ("lint and author terraform code for cloud infra", "terraform-helper"),
    ("build an optimised Dockerfile for my project", "docker-builder"),
    ("multi-stage docker container build", "docker-builder"),
    ("debug a failing kubernetes pod", "k8s-debugger"),
    ("inspect k8s pod events and logs to find issues", "k8s-debugger"),
    ("draft a blameless postmortem after an outage", "incident-postmortem"),
    ("write up an incident postmortem from a slack timeline", "incident-postmortem"),
    ("generate an invoice for a client", "invoice-generator"),
    ("track unpaid invoices for an agency", "invoice-generator"),
    ("turn an essay into a twitter thread", "twitter-thread"),
    ("write a viral X thread with a hook tweet", "twitter-thread"),
]


@pytest.fixture()
def seeded_db(db_session):
    """Seed the 25-skill catalog with embeddings populated."""
    from tests.conftest import make_skill

    for slug, title, desc, tags, tier, category in SEED_CATALOG:
        sk = make_skill(
            db_session,
            slug=slug,
            title=title,
            description=desc,
            category=category,
            tier=tier,
            related_skills=tags,
            is_public=True,
        )
        sk.embedding = json.dumps(embed_skill(sk))
    db_session.flush()
    return db_session


# ── Unit tests ─────────────────────────────────────────────────────────────


def test_score_vector_is_in_unit_range():
    a = embed_text("scrape websites")
    b = embed_text("web scraping tool")
    s = score_vector(a, b)
    assert 0.0 <= s <= 1.0


def test_score_vector_zero_on_empty():
    assert score_vector([], [1.0] * 384) == 0.0


def test_bm25_prefers_title_overlap():
    class S:
        title = "Web scraper for structured data"
        description = "Scrapes structured data with selectors"
        related_skills = ["scraping"]

    high = score_bm25("web scraper data", S())
    low = score_bm25("invoice billing", S())
    assert high > low


def test_combine_zeroes_on_tier_lock():
    s = combine(0.9, 5.0, tier_match=False, in_cookbook=False)
    assert s == 0.0


def test_combine_boosts_cookbook_membership():
    a = combine(0.5, 1.0, tier_match=True, in_cookbook=False)
    b = combine(0.5, 1.0, tier_match=True, in_cookbook=True)
    assert b > a


def test_decode_embedding_roundtrip():
    v = [0.1, 0.2, 0.3]
    assert _decode_embedding(json.dumps(v)) == v
    assert _decode_embedding(v) == v
    assert _decode_embedding(None) is None


def test_recall_filters_by_tier(seeded_db):
    out = recall_skills(seeded_db, query="terraform module aws",
                        tier_filter=["free"], is_master=False, user_tier="free")
    slugs = [h["slug"] for h in out["hits"]]
    # terraform-helper is operator-tier — must NOT appear when filter is free-only
    assert "terraform-helper" not in slugs


def test_recall_marks_tier_locked(seeded_db):
    # Expand filter to include operator-tier results, but caller is free-tier.
    out = recall_skills(seeded_db, query="terraform module aws",
                        tier_filter=["free", "cook", "operator"],
                        is_master=False, user_tier="free")
    slugs = {h["slug"]: h for h in out["hits"]}
    if "terraform-helper" in slugs:
        assert slugs["terraform-helper"]["install_status"] == "tier_locked"


def test_recall_master_sees_everything(seeded_db):
    out = recall_skills(seeded_db, query="terraform module aws",
                        tier_filter=["free", "cook", "operator"],
                        is_master=True, user_tier=None, limit=5)
    hits = out["hits"]
    assert hits, "master should see at least one hit"
    # No tier_locked because master can install anything.
    assert all(h["install_status"] != "tier_locked" for h in hits)


def test_recall_excludes_non_public(db_session):
    from tests.conftest import make_skill
    sk = make_skill(db_session, slug="hidden-skill", title="Hidden",
                    description="should not appear", tier="free", is_public=False)
    sk.embedding = json.dumps(embed_skill(sk))
    db_session.flush()
    out = recall_skills(db_session, query="hidden", is_master=True)
    assert all(h["slug"] != "hidden-skill" for h in out["hits"])


# ── 50-query eval set — HARD GATE ──────────────────────────────────────────


def test_eval_set_has_50_queries():
    assert len(EVAL_QUERIES) == 50


def test_eval_set_top3_accuracy(seeded_db):
    """≥70% of eval queries return the gold skill in the top-3 hits.

    If this test fails with current ranking, see RECALL_QUALITY.md for the
    documented divergence and BM25 fallback path. The threshold is intentionally
    aggressive — vector + BM25 hybrid should clear it on the seeded catalog.
    """
    hits_top3 = 0
    misses: list[tuple[str, str, list[str]]] = []
    for query, expected in EVAL_QUERIES:
        out = recall_skills(seeded_db, query=query, is_master=True, limit=3)
        slugs = [h["slug"] for h in out["hits"]]
        if expected in slugs:
            hits_top3 += 1
        else:
            misses.append((query, expected, slugs))
    accuracy = hits_top3 / len(EVAL_QUERIES)
    # Assertion message reports misses for diagnosis.
    assert accuracy >= 0.70, (
        f"top-3 accuracy {accuracy:.2%} < 70%. Misses: " +
        "; ".join(f"'{q}' -> expected {e}, got {g}" for q, e, g in misses[:8])
    )
