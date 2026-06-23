"""Isolated conftest for SkillSpector tests.

This package uses only stdlib + skillspector — it MUST NOT inherit from
the parent tests/conftest.py which requires FastAPI + PostgreSQL.
All SkillSpector tests run in .skillspector-venv; no app imports.
"""
# No imports — intentionally empty to shadow the parent conftest.
