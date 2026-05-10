"""Tier label helpers — load from config/tiers.yaml.
For any user-facing text (API responses, emails, logs), use display_label(slug)
instead of hardcoding 'Cook' or 'Operator'."""
import yaml
from functools import lru_cache
from pathlib import Path

# config/tiers.yaml lives two levels up from app/
TIERS_YAML = Path(__file__).resolve().parent.parent / 'config' / 'tiers.yaml'


@lru_cache(maxsize=1)
def _tiers() -> dict:
    with open(TIERS_YAML) as f:
        return yaml.safe_load(f)['tiers']


def display_label(db_slug: str) -> str:
    """Return the user-facing display name for a DB tier slug."""
    return _tiers().get(db_slug, {}).get('display_name', db_slug.title())
