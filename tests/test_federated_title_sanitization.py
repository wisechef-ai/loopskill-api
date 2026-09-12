from app.services.hub_snapshot import map_hub_row


PROMPT_BLOB = (
    "1. The input is a Word template document; 2. Analyze the structure of this template, including: "
    "1) Font styles, sizes, etc., for headings, body text, etc.; 2) Content: the general "
).ljust(475, " ")[:475]


def test_ingest_replaces_prompt_blob_with_identifier_and_sanitizes_title():
    row = map_hub_row(
        {
            "name": PROMPT_BLOB,
            "identifier": "templatebased-writing",
            "description": "desc",
            "source": "skills.sh",
        }
    )

    assert row["title"] == "templatebased-writing"
    assert len(row["title"]) <= 120
    assert "\n" not in row["title"]


def test_ingest_collapses_whitespace_and_removes_control_characters():
    row = map_hub_row(
        {
            "name": "  A\n\t useful\u0000 title  ",
            "identifier": "useful-title",
        }
    )

    assert row["title"] == "A useful title"
