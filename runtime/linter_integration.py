"""Phase A.7 linter wrapper that pulls in the F.1 recipe.yaml validator.

The discipline linter (scripts/skill_discipline_linter.py) does a structural
regex check — `must_declare_compat` — but does not understand the JSON Schema.
This wrapper composes the two so callers get a single ``ok/violations`` blob.

We deliberately do NOT edit skill_discipline_linter.py. Wrapping keeps that
file focused on prose-style rules and lets the schema-shaped validator stay
in `runtime/`. Anyone wanting full discipline + runtime validation calls
``lint_skill_with_runtime`` here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.skill_discipline_linter import lint_skill, lint_tarball_bytes

from runtime.recipe_validator import validate as validate_recipe


def lint_skill_with_runtime(
    readme_text: str,
    recipe_yaml: str | None = None,
    skill_dir: Path | None = None,
) -> dict[str, Any]:
    """Run discipline lint + recipe.yaml schema validation.

    Returns ``{"ok": bool, "violations": [...], "schema_errors": [...]}``.
    ``ok`` is true only if both the discipline rules pass and the schema
    accepts the recipe.yaml (when present).
    """
    discipline = lint_skill(readme_text, recipe_yaml=recipe_yaml, skill_dir=skill_dir)

    schema_errors: list[str] = []
    if recipe_yaml is not None:
        result = validate_recipe(recipe_yaml)
        if not result["ok"]:
            schema_errors = result["errors"]

    ok = discipline["ok"] and not schema_errors
    return {
        "ok": ok,
        "violations": discipline.get("violations", []),
        "schema_errors": schema_errors,
    }


def lint_tarball_with_runtime(tarball_bytes: bytes) -> dict[str, Any]:
    """Tarball variant: run lint_tarball_bytes + extract recipe.yaml for schema check."""
    import io
    import tarfile

    discipline = lint_tarball_bytes(tarball_bytes)

    recipe_yaml: str | None = None
    try:
        tf = tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz")
    except (tarfile.TarError, EOFError, OSError):
        return {
            "ok": discipline["ok"],
            "violations": discipline.get("violations", []),
            "schema_errors": [],
        }

    with tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            if member.name.rsplit("/", 1)[-1] == "recipe.yaml":
                fobj = tf.extractfile(member)
                if fobj is not None:
                    try:
                        recipe_yaml = fobj.read().decode("utf-8", errors="replace")
                    except (OSError, UnicodeDecodeError):
                        recipe_yaml = None
                break

    schema_errors: list[str] = []
    if recipe_yaml is not None:
        result = validate_recipe(recipe_yaml)
        if not result["ok"]:
            schema_errors = result["errors"]

    return {
        "ok": discipline["ok"] and not schema_errors,
        "violations": discipline.get("violations", []),
        "schema_errors": schema_errors,
    }
