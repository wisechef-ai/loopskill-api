"""Root-level conftest.py — runs before ANY test module is imported.

Sets WR_DATABASE_URL to sqlite so that:
  - the global `settings = Settings()` in app/config.py does not trigger the
    production-secrets RuntimeError (secfix_1905 Issue #1 gate)
  - the in-memory SQLite test engine in tests/conftest.py continues to work

This must live at the repo root (not inside tests/) so it is executed BEFORE
pytest begins collecting or importing test modules.
"""
import os

# Must be set before any app.* import so Settings() picks it up.
os.environ.setdefault("WR_DATABASE_URL", "sqlite:///./test_dev.db")
# COOKIES_SECURE defaults to True; in sqlite test env we allow False.
os.environ.setdefault("WR_COOKIES_SECURE", "false")
