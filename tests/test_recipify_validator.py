"""Frontmatter validator — v7 Phase G."""
from __future__ import annotations

import pytest

from app.recipify import ValidationError, validate_frontmatter


_GOOD = """---
name: hello-world
description: A friendly greeter that prints hello.
---
# body content here
"""


def test_happy_path_returns_dict():
    meta = validate_frontmatter(_GOOD)
    assert meta["name"] == "hello-world"
    assert meta["description"].startswith("A friendly")


def test_missing_name_raises():
    text = """---
description: just a description
---
body"""
    with pytest.raises(ValidationError, match="name"):
        validate_frontmatter(text)


def test_missing_description_raises():
    text = """---
name: ok-slug
---
body"""
    with pytest.raises(ValidationError, match="description"):
        validate_frontmatter(text)


@pytest.mark.parametrize(
    "bad_name",
    [
        "UpperCase",          # uppercase letters
        "has spaces",         # whitespace
        "x" * 65,             # too long
        "bad!char",           # special chars
    ],
)
def test_bad_slug_format_raises(bad_name):
    text = f"""---
name: {bad_name!r}
description: ok
---
body"""
    with pytest.raises(ValidationError):
        validate_frontmatter(text)


def test_non_string_description_raises():
    text = """---
name: ok-slug
description: 42
---
body"""
    with pytest.raises(ValidationError, match="description"):
        validate_frontmatter(text)


def test_yaml_parse_error_raises():
    text = """---
name: ok-slug
description: "unterminated string
---
body"""
    with pytest.raises(ValidationError):
        validate_frontmatter(text)


def test_no_frontmatter_at_all_raises():
    with pytest.raises(ValidationError, match="frontmatter"):
        validate_frontmatter("just some markdown without dashes")


def test_empty_string_raises():
    with pytest.raises(ValidationError):
        validate_frontmatter("")
