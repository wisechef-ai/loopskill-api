"""tests/test_anonymizer.py

30+ fixtures covering all anonymizer rules: ADAM_TOKENS, INFRA_REFS, PATHS,
AGENT_NAMES (user-facing context), and EMAILS.

Tokens come from a committed FAKE fixture (tests/fixtures/anonymizer_tokens.test.json)
— never real operator PII. The autouse fixture below points the anonymizer at it
and reloads the module-level token lists so the mechanism is verified without the
open-source tree carrying any real names/hostnames.
"""
from pathlib import Path

import pytest

import app.services.anonymizer as _anon
from app.services.anonymizer import Finding, anonymize

_FIXTURE = str(Path(__file__).parent / "fixtures" / "anonymizer_tokens.test.json")


@pytest.fixture(autouse=True)
def _load_fake_tokens(monkeypatch):
    """Point the anonymizer at the fake test-token config and reload its lists."""
    monkeypatch.setenv("WR_ANONYMIZER_CONFIG", _FIXTURE)
    toks = _anon._load_tokens()
    monkeypatch.setattr(_anon, "ADAM_TOKENS", toks["user_tokens"])
    monkeypatch.setattr(_anon, "INFRA_REFS", toks["infra_refs"])
    monkeypatch.setattr(_anon, "_PATHS", toks["paths"])
    monkeypatch.setattr(_anon, "_AGENT_NAMES", toks["agent_names"])
    yield


# ── Helpers ─────────────────────────────────────────────────────────────

def _text(result) -> str:
    return result[0]

def _findings(result) -> list[Finding]:
    return result[1]

def _has_finding(findings: list[Finding], category: str) -> bool:
    return any(f.category == category for f in findings)


# ── ADAM_TOKENS ──────────────────────────────────────────────────────────

class TestTestuserTokens:
    def test_adam_replaced(self):
        out, findings = anonymize("Testuser wrote this skill.")
        assert "Testuser" not in out
        assert "<USER>" in out
        assert _has_finding(findings, "adam_token")

    def test_bombilla_replaced(self):
        out, findings = anonymize("Codename is the project codename.")
        assert "Codename" not in out
        assert "<USER>" in out
        assert _has_finding(findings, "adam_token")

    def test_marco_replaced(self):
        out, findings = anonymize("Reviewer reviewed the PR.")
        assert "Reviewer" not in out
        assert "<USER>" in out

    def test_karol_replaced(self):
        out, findings = anonymize("Contact Contact for access.")
        assert "Contact" not in out
        assert "<USER>" in out

    def test_olek_replaced(self):
        out, findings = anonymize("Deployer deployed this.")
        assert "Deployer" not in out
        assert "<USER>" in out

    def test_mariusz_replaced(self):
        out, findings = anonymize("Approver approved it.")
        assert "Approver" not in out
        assert "<USER>" in out

    def test_multiple_adam_tokens_in_one_text(self):
        out, findings = anonymize("Testuser and Contact and Reviewer met.")
        assert "Testuser" not in out
        assert "Contact" not in out
        assert "Reviewer" not in out
        assert out.count("<USER>") >= 3

    def test_adam_token_case_sensitive_preserved(self):
        out, _ = anonymize("adam is fine but Testuser is not.")
        # lowercase 'adam' may or may not match — depends on design
        # Uppercase Testuser MUST be replaced
        assert "Testuser" not in out

    def test_no_token_unchanged(self):
        out, findings = anonymize("No PII here at all.")
        assert out == "No PII here at all."
        assert findings == []


# ── INFRA_REFS ───────────────────────────────────────────────────────────

class TestInfraRefs:
    def test_wisechef_agents_replaced(self):
        out, findings = anonymize("Runs on test-agents host.")
        assert "test-agents" not in out
        assert "<INFRA>" in out
        assert _has_finding(findings, "infra_ref")

    def test_wisechef_hq_replaced(self):
        out, findings = anonymize("Deploy to test-hq.")
        assert "test-hq" not in out
        assert "<INFRA>" in out

    def test_ticketboard_replaced(self):
        out, findings = anonymize("File a ticket in ticketboard.")
        assert "ticketboard" not in out
        assert "<INFRA>" in out

    def test_obsidian_vault_replaced(self):
        out, findings = anonymize("Notes at notes-vault/projects.")
        assert "notes-vault" not in out
        assert "<INFRA>" in out

    def test_adam_xps_replaced(self):
        out, findings = anonymize("Tested on test-box machine.")
        assert "test-box" not in out
        assert "<INFRA>" in out

    def test_testvision_replaced(self):
        out, findings = anonymize("Wired into testvision service.")
        assert "testvision" not in out
        assert "<INFRA>" in out

    def test_multiple_infra_in_one_text(self):
        out, _ = anonymize("test-agents connects to test-hq via ticketboard.")
        assert "test-agents" not in out
        assert "test-hq" not in out
        assert "ticketboard" not in out


# ── PATHS ────────────────────────────────────────────────────────────────

class TestPaths:
    def test_home_adam_linux_replaced(self):
        out, findings = anonymize("Config at /home/testuser/.config/app.yaml")
        assert "/home/testuser/" not in out
        assert "<HOME>" in out
        assert _has_finding(findings, "path")

    def test_home_adam_mac_replaced(self):
        out, findings = anonymize("Skills in /Users/testuser/projects/skills/")
        assert "/Users/testuser/" not in out
        assert "<HOME>" in out

    def test_dollar_home_replaced(self):
        out, findings = anonymize("export PATH=$HOME/bin:$PATH")
        assert "$HOME/" not in out
        assert "<HOME>" in out

    def test_path_in_code_block_replaced(self):
        out, _ = anonymize("Run `cp file /home/testuser/dest/`")
        assert "/home/testuser/" not in out


# ── EMAILS ───────────────────────────────────────────────────────────────

class TestEmails:
    def test_personal_email_replaced(self):
        out, findings = anonymize("Contact me at user@gmail.com for details.")
        assert "user@gmail.com" not in out
        assert "<EMAIL>" in out
        assert _has_finding(findings, "email")

    def test_allowed_wisechef_email_preserved(self):
        out, _ = anonymize("Support at support@wisechef.ai")
        assert "support@wisechef.ai" in out

    def test_allowed_example_com_preserved(self):
        out, _ = anonymize("Example: user@example.com")
        assert "user@example.com" in out

    def test_multiple_emails_replaced(self):
        out, findings = anonymize("Testuser: adam@gmail.com, Reviewer: marco@outlook.com")
        assert "adam@gmail.com" not in out
        assert "marco@outlook.com" not in out
        assert out.count("<EMAIL>") >= 2


# ── AGENT_NAMES ──────────────────────────────────────────────────────────

class TestAgentNames:
    def test_tori_in_user_context_replaced(self):
        out, findings = anonymize("AgentAlpha will handle this task.", user_facing=True)
        assert "AgentAlpha" not in out
        assert "<AGENT>" in out
        assert _has_finding(findings, "agent_name")

    def test_wise_in_user_context_replaced(self):
        out, findings = anonymize("AgentBeta processed the request.", user_facing=True)
        assert "AgentBeta" not in out
        assert "<AGENT>" in out

    def test_chef_in_user_context_replaced(self):
        out, findings = anonymize("AgentGamma ran the recipe.", user_facing=True)
        assert "AgentGamma" not in out
        assert "<AGENT>" in out

    def test_agent_names_not_replaced_when_not_user_facing(self):
        out, findings = anonymize("AgentAlpha and AgentGamma are agents.", user_facing=False)
        assert "AgentAlpha" in out
        assert "AgentGamma" in out
        assert not _has_finding(findings, "agent_name")

    def test_agent_names_default_not_user_facing(self):
        out, findings = anonymize("AgentAlpha is the main agent.")
        assert "AgentAlpha" in out


# ── COMBINED ─────────────────────────────────────────────────────────────

class TestCombined:
    def test_all_categories_in_one_text(self):
        text = (
            "Testuser built test-agents at /home/testuser/. "
            "Email: adam@hotmail.com. AgentAlpha runs it."
        )
        out, findings = anonymize(text, user_facing=True)
        assert "Testuser" not in out
        assert "test-agents" not in out
        assert "/home/testuser/" not in out
        assert "adam@hotmail.com" not in out
        assert "AgentAlpha" not in out
        categories = {f.category for f in findings}
        assert "adam_token" in categories
        assert "infra_ref" in categories
        assert "path" in categories
        assert "email" in categories
        assert "agent_name" in categories

    def test_finding_has_original_value(self):
        _, findings = anonymize("Testuser built this.")
        assert any(f.original == "Testuser" for f in findings)

    def test_finding_has_replacement(self):
        _, findings = anonymize("Testuser built this.")
        assert any(f.replacement == "<USER>" for f in findings)

    def test_clean_text_returns_empty_findings(self):
        _, findings = anonymize("Nothing to redact here.")
        assert findings == []

    def test_idempotent(self):
        text = "Testuser deployed to test-hq."
        out1, _ = anonymize(text)
        out2, _ = anonymize(out1)
        assert out1 == out2

    def test_finding_has_position(self):
        text = "Testuser built this."
        _, findings = anonymize(text)
        f = findings[0]
        assert hasattr(f, "start")
        assert hasattr(f, "end")
