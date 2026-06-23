"""F.1 — schema validator: happy path + 4 fail cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.recipe_validator import validate, load_schema


HAPPY = """
runtime:
  binaries:
    - name: uv
      version: "0.5.0"
      release_source: "github.com/astral-sh/uv"
      check_latest: weekly
  services:
    - name: cognee-api
      type: docker-compose
      compose: "deploys/cognee.yml"
      port: 8100
      health: "GET http://localhost:8100/healthz"
  env:
    required: [COGNEE_DB_URL]
    optional: [COGNEE_LLM_API_KEY]
  cron:
    - name: nightly-sync
      schedule: "0 3 * * *"
      cmd: "python sync.py"
  compatibility:
    os: [linux, macos]
    arch: [x86_64, arm64]
    gpu:
      required: false
      preferred: nvidia
      vram_gb: 8
      cuda: ">=11.8"
    ram_gb:
      minimum: 4
      recommended: 8
    disk_gb: 2
    network: required
"""


def test_schema_loads():
    schema = load_schema()
    assert schema["$id"].startswith("https://")
    assert "runtime" in schema["properties"]


def test_happy_path():
    assert validate(HAPPY) == {"ok": True, "errors": []}


def test_missing_runtime_block():
    res = validate("not_runtime:\n  foo: bar\n")
    assert not res["ok"]
    assert any("missing top-level `runtime:`" in e for e in res["errors"])


def test_missing_compatibility():
    res = validate("runtime:\n  binaries: []\n")
    assert not res["ok"]
    assert any("compatibility is required" in e for e in res["errors"])


def test_invalid_os_value():
    res = validate(
        "runtime:\n"
        "  compatibility:\n"
        "    os: [haiku-os]\n"
        "    arch: [x86_64]\n"
        "    ram_gb: 4\n"
        "    network: required\n"
    )
    assert not res["ok"]
    assert any("invalid value 'haiku-os'" in e for e in res["errors"])


def test_unknown_service_type():
    res = validate(
        "runtime:\n"
        "  services:\n"
        "    - name: foo\n"
        "      type: kubernetes\n"
        "  compatibility:\n"
        "    os: [linux]\n"
        "    arch: [x86_64]\n"
        "    ram_gb: 4\n"
        "    network: required\n"
    )
    assert not res["ok"]
    assert any("services[0].type must be one of" in e for e in res["errors"])


def test_invalid_yaml():
    res = validate("runtime: : :\n")
    assert not res["ok"]
    assert res["errors"][0].startswith("yaml:")


@pytest.mark.parametrize("slug", [
    "cognee", "scrapling-official", "web-scraper-pro", "faster-whisper",
    "kokoro-tts", "manim-video", "ascii-video", "comfyui",
    "llama-cpp", "ollama-low-vram-model-pick",
])
def test_top10_recipes_validate(slug):
    """F.9 deliverable: every authored recipe.yaml must pass F.1 validation."""
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "recipes" / slug / "recipe.yaml"
    assert path.exists(), f"missing recipe.yaml for {slug}"
    res = validate(path.read_text(encoding="utf-8"))
    assert res["ok"], f"{slug}: {res['errors']}"
