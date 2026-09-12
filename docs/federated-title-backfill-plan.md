# Federated title backfill plan

The ingest fix is forward-only. Existing `federation_hub_skills` rows retain their
stored titles until this plan is reviewed and run deliberately against the
intended database; this plan has **not** been executed.

## Dry run / apply command

From a release checkout, with `WR_DATABASE_URL` pointed at the target database:

```sh
python - <<'PY'
from sqlalchemy import create_engine, text
from app.config import get_settings
from app.services.federated_title import sanitize_federated_title

engine = create_engine(get_settings().database_url)
with engine.begin() as conn:
    rows = conn.execute(text("SELECT id, title, identifier FROM federation_hub_skills")).mappings().all()
    updates = []
    for row in rows:
        new_title = sanitize_federated_title(row["title"], row["identifier"])
        if new_title != row["title"]:
            updates.append({"id": row["id"], "title": new_title})
    print(f"would update {len(updates)} of {len(rows)} rows")
    if input("Type APPLY to write these changes: ") == "APPLY":
        conn.execute(
            text("UPDATE federation_hub_skills SET title = :title WHERE id = :id"),
            updates,
        )
        print(f"updated {len(updates)} rows")
PY
```

Before applying, snapshot the table and review the reported count. Afterward,
verify `max(length(title)) <= 120`, that no title contains a control character,
and that prompt-like rows use their identifier fallback. The transaction is
atomic; any exception rolls it back. The command intentionally does not run as
part of deploy or startup.
