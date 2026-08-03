"""converge_0208 P4 — SELF_HOST.md must document the LOOP path, not just skills.

The sprint's one-sentence test is:

    A stranger can follow SELF_HOST.md, enroll an agent, receive a bundle, and
    watch a loop run — without a human intervening.

The skill half of that doc is at that standard (exact crontab line, end to end).
The loop half was a single orphan reference to `./loopskill-emit-run.sh`, a
script that did not exist in this repo. This suite pins the doc to the same
standard as the skill path: every hop named, every command runnable, every
script it references actually present.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "SELF_HOST.md"


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text()


@pytest.fixture(scope="module")
def loop_section(doc: str) -> str:
    """The loop-path section only — so a stray mention elsewhere can't pass this."""
    m = re.search(r"^##\s+Running loops.*?(?=^##\s|\Z)", doc, re.MULTILINE | re.DOTALL)
    assert m, "SELF_HOST.md has no '## Running loops' section"
    return m.group(0)


def test_every_script_referenced_by_the_doc_exists(doc: str):
    """The failure that started this phase: the doc pointed at vapour."""
    missing = [
        name
        for name in re.findall(r"scripts/([A-Za-z0-9_.-]+\.(?:sh|py))", doc)
        if not (REPO_ROOT / "scripts" / name).exists()
    ]
    assert not missing, f"SELF_HOST.md references scripts that do not exist: {missing}"


@pytest.mark.parametrize(
    "hop",
    [
        "loopskill_declare_loop",  # 1. declare the loop manifest
        "loopskill_assign",  # 2. place it on a member
        "/api/my/loop-assignments",  # 3. the member reads its assignments
        "app.loop_apply_cli",  # 4. materialize the local cron
        "loopskill/",  # 5. the managed cron namespace
        "loopskill-emit-run.sh",  # 6. the fired loop emits its outcome
        "loopskill-collect-reports.py",  # 7. the batched POST
        "/api/sync-report",  # 8. telemetry lands
    ],
)
def test_loop_path_documents_every_hop(loop_section: str, hop: str):
    assert hop in loop_section, f"loop path never mentions {hop!r}"


def test_loop_path_ships_a_copy_pasteable_installer_command(loop_section: str):
    assert "install-loop-apply.sh" in loop_section


def test_loop_path_states_which_hosts_it_actually_supports(loop_section: str):
    """app/loop_apply.py writes the Hermes scheduler format and only that.

    A stranger on Codex must learn that from the doc, not from a cron that
    silently never materializes anything.
    """
    lowered = loop_section.lower()
    assert "hermes" in lowered
    assert any(w in lowered for w in ("only", "not yet", "does not")), (
        "the doc must state the host limitation plainly"
    )


def test_loop_path_uses_the_member_key_not_the_bundle_key(loop_section: str):
    """loop assignments are a MEMBER surface; the bundle key gets a 403."""
    assert "member" in loop_section.lower()


def test_emitter_signature_in_doc_matches_the_shipped_script(doc: str):
    """The published contract and the script must not drift apart again."""
    m = re.search(r"loopskill-emit-run\.sh\s+<loop_slug>\s+<outcome>[^\n]*", doc)
    assert m, "the documented emit-run signature is missing"
    documented = m.group(0)
    for token in ("[accepted]", "[cost_usd]", "[duration_s]", "[detail]"):
        assert token in documented, f"{token} dropped from the published signature"

    script = (REPO_ROOT / "scripts" / "loopskill-emit-run.sh").read_text()
    assert "<loop_slug> <outcome> [accepted] [cost_usd] [duration_s] [detail]" in script, (
        "the script's own usage line must quote the published signature verbatim"
    )
