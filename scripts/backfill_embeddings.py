"""Backfill skill embeddings (v7 Phase E).

Iterates every skill row, computes a 384-dim BAAI/bge-small-en-v1.5 embedding
from `title + description + related_skills`, and stores it on the row.

Storage:
    - Postgres: native pgvector vector(384) via parameterised UPDATE
    - SQLite:   JSON-encoded list[float] in the embedding TEXT column

Idempotency: rows whose embedding is already populated are skipped unless
`--force` is passed.

Usage:
    .venv/bin/python -m scripts.backfill_embeddings           # backfill missing
    .venv/bin/python -m scripts.backfill_embeddings --force   # re-embed all
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from app.database import SessionLocal
from app.embeddings import embed_skill
from app.models import Skill


def _is_postgres(db) -> bool:
    return db.bind.dialect.name == "postgresql"


def _store_embedding(db, skill_id, vec: list[float], use_pgvector: bool) -> None:
    if use_pgvector:
        # pgvector accepts a stringified `[v1,v2,...]` literal cast to vector.
        literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        try:
            db.execute(
                text("UPDATE skills SET embedding = :v WHERE id = :id"),
                {"v": literal, "id": skill_id},
            )
            return
        except Exception:
            # pgvector not actually present — fall through to JSON storage.
            pass
    db.execute(
        text("UPDATE skills SET embedding = :v WHERE id = :id"),
        {"v": json.dumps(vec), "id": skill_id},
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="Re-embed even if already populated")
    p.add_argument("--limit", type=int, default=None, help="Cap rows processed")
    args = p.parse_args(argv)

    db = SessionLocal()
    try:
        use_pgvector = _is_postgres(db)
        q = db.query(Skill)
        if not args.force:
            q = q.filter(Skill.embedding.is_(None))
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        print(f"backfill: {len(rows)} skills to embed (force={args.force})")
        ok = 0
        for i, sk in enumerate(rows, start=1):
            try:
                vec = embed_skill(sk)
                _store_embedding(db, sk.id, vec, use_pgvector)
                ok += 1
                if i % 25 == 0 or i == len(rows):
                    db.commit()
                    print(f"  progress: {i}/{len(rows)}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {sk.slug}: {exc}", file=sys.stderr)
        db.commit()
        print(f"done: {ok}/{len(rows)} embeddings written")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
