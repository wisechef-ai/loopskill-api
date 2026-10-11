"""Production verdict diff: how a rule change re-judges REAL posts.

Every claims-gate rule change must run this before deploy. It scores an export
of the Postiz Post table with the working tree and with a base git ref, and
prints the posts whose verdict changes (newly flagged / no longer flagged),
grouped by post state and rule, with excerpts.

    psql ... -c "\\copy (SELECT id, status, content FROM \\"Post\\") TO 'posts.csv' CSV"
    python deploy/claimgate/prod_diff.py posts.csv [BASE_REF]   # default HEAD

Newly flagged PUBLISHED posts are history (they were true when published, or
are the stale copy the gate exists to stop); newly flagged DRAFT / QUEUE posts
would be quarantined on their next edit and need a human look first.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SNIP = (
    "import csv,json,sys; sys.path.insert(0,'.');"
    "from app.services import claims_contract as cc;"
    "print(json.dumps({p:[s,sorted({v['rule_id']+'|'+v['match'] for v in cc.check_text(c)})] "
    "for p,s,c in csv.reader(open(sys.argv[1]))}))"
)


def _score(cwd: Path, posts: str) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", SNIP, posts], cwd=cwd, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def main(posts: str, base: str = "HEAD") -> int:
    posts = str(Path(posts).resolve())
    new = _score(REPO, posts)
    tmp = tempfile.mkdtemp()
    subprocess.run(["git", "worktree", "add", "-q", "--detach", tmp, base], cwd=REPO, check=True)
    try:
        old = _score(Path(tmp), posts)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=REPO, check=True)

    def flagged(d: dict) -> set:
        return {p for p, v in d.items() if v[1]}

    added, gone = sorted(flagged(new) - flagged(old)), sorted(flagged(old) - flagged(new))
    print(
        f"posts {len(new)}: flagged {len(flagged(old))} -> {len(flagged(new))}; newly {len(added)}, unflagged {len(gone)}"
    )
    for label, ids, side in (("NEWLY FLAGGED", added, new), ("NO LONGER FLAGGED", gone, old)):
        if not ids:
            continue
        print(f"\n{label} by state:", dict(collections.Counter(side[p][0] for p in ids)))
        rules = collections.Counter(h.split("|")[0] for p in ids for h in side[p][1])
        print(f"{label} by rule:", dict(rules))
        shown: dict[str, int] = collections.defaultdict(int)
        for p in ids:
            for h in side[p][1]:
                rid, match = h.split("|", 1)
                if shown[rid] < 3:
                    shown[rid] += 1
                    print(f"  {side[p][0]:9s} {p} {rid}: {match[:110]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
