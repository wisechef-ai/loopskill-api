"""tests/test_anonymizer.py

30+ fixtures covering all anonymizer rules: ADAM_TOKENS, INFRA_REFS, PATHS,
AGENT_NAMES (user-facing context), and EMAILS.
"""
import pytest
from app.services.anonymizer import anonymize, Finding


# ── Helpers ─────────────────────────────────────────────────────────────

def _text(result) -> str:
    return result[0]

def _findings(result) -> list[Finding]:
    return result[1]

def _has_finding(findings: list[Finding], category: str) -> bool:
    return any(f.category == category for f in findings)


# ── ADAM_TOKENS ──────────────────────────────────────────────────────────

class TestAdamTokens:
    def test_adam_replaced(self):
        out, findings = anonymize("Adam wrote this skill.")
        assert "Adam" not in out
        assert "<USER>" in out
        assert _has_finding(findings, "adam_token")

    def test_bombilla_replaced(self):
        out, findings = anonymize("Bombilla is the project codename.")
        assert "Bombilla" not in out
        assert "<USER>" in out
        assert _has_finding(findings, "adam_token")

    def test_marco_replaced(self):
        out, findings = anonymize("Marco reviewed the PR.")
        assert "Marco" not in out
        assert "<USER>" in out

    def test_karol_replaced(self):
        out, findings = anonymize("Contact Karol for access.")
        assert "Karol" not in out
        assert "<USER>" in out

    def test_olek_replaced(self):
        out, findings = anonymize("Olek deployed this.")
        assert "Olek" not in out
        assert "<USER>" in out

    def test_mariusz_replaced(self):
        out, findings = anonymize("Mariusz approved it.")
        assert "Mariusz" not in out
        assert "<USER>" in out

    def test_multiple_adam_tokens_in_one_text(self):
        out, findings = anonymize("Adam and Karol and Marco met.")
        assert "Adam" not in out
        assert "Karol" not in out
        assert "Marco" not in out
        assert out.count("<USER>") >= 3

    def test_adam_token_case_sensitive_preserved(self):
        out, _ = anonymize("adam is fine but Adam is not.")
        # lowercase 'adam' may or may not match — depends on design
        # Uppercase Adam MUST be replaced
        assert "Adam" not in out

    def test_no_token_unchanged(self):
        out, findings = anonymize("No PII here at all.")
        assert out == "No PII here at all."
        assert findings == []


# ── INFRA_REFS ───────────────────────────────────────────────────────────

class TestInfraRefs:
    def test_wisechef_agents_replaced(self):
        out, findings = anonymize("Runs on wisechef-agents host.")
        assert "wisechef-agents" not in out
        assert "<INFRA>" in out
        assert _has_finding(findings, "infra_ref")

    def test_wisechef_hq_replaced(self):
        out, findings = anonymize("Deploy to wisechef-hq.")
        assert "wisechef-hq" not in out
        assert "<INFRA>" in out

    def test_paperclip_replaced(self):
        out, findings = anonymize("File a ticket in paperclip.")
        assert "paperclip" not in out
        assert "<INFRA>" in out

    def test_obsidian_vault_replaced(self):
        out, findings = anonymize("Notes at obsidian-vault/projects.")
        assert "obsidian-vault" not in out
        assert "<INFRA>" in out

    def test_adam_xps_replaced(self):
        out, findings = anonymize("Tested on adam-xps machine.")
        assert "adam-xps" not in out
        assert "<INFRA>" in out

    def test_wisevision_replaced(self):
        out, findings = anonymize("Wired into wisevision service.")
        assert "wisevision" not in out
        assert "<INFRA>" in out

    def test_multiple_infra_in_one_text(self):
        out, _ = anonymize("wisechef-agents connects to wisechef-hq via paperclip.")
        assert "wisechef-agents" not in out
        assert "wisechef-hq" not in out
        assert "paperclip" not in out


# ── PATHS ────────────────────────────────────────────────────────────────

class TestPaths:
    def test_home_adam_linux_replaced(self):
        out, findings = anonymize("Config at /home/adam/.config/app.yaml")
        assert "/home/adam/" not in out
        assert "<HOME>" in out
        assert _has_finding(findings, "path")

    def test_home_adam_mac_replaced(self):
        out, findings = anonymize("Skills in /Users/adam/projects/skills/")
        assert "/Users/adam/" not in out
        assert "<HOME>" in out

    def test_dollar_home_replaced(self):
        out, findings = anonymize("export PATH=$HOME/bin:$PATH")
        assert "$HOME/" not in out
        assert "<HOME>" in out

    def test_path_in_code_block_replaced(self):
        out, _ = anonymize("Run `cp file /home/adam/dest/`")
        assert "/home/adam/" not in out


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
        out, findings = anonymize("Adam: adam@gmail.com, Marco: marco@outlook.com")
        assert "adam@gmail.com" not in out
        assert "marco@outlook.com" not in out
        assert out.count("<EMAIL>") >= 2


# ── AGENT_NAMES ──────────────────────────────────────────────────────────

class TestAgentNames:
    def test_tori_in_user_context_replaced(self):
        out, findings = anonymize("Tori will handle this task.", user_facing=True)
        assert "Tori" not in out
        assert "<AGENT>" in out
        assert _has_finding(findings, "agent_name")

    def test_wise_in_user_context_replaced(self):
        out, findings = anonymize("Wise processed the request.", user_facing=True)
        assert "Wise" not in out
        assert "<AGENT>" in out

    def test_chef_in_user_context_replaced(self):
        out, findings = anonymize("Chef ran the recipe.", user_facing=True)
        assert "Chef" not in out
        assert "<AGENT>" in out

    def test_agent_names_not_replaced_when_not_user_facing(self):
        out, findings = anonymize("Tori and Chef are agents.", user_facing=False)
        assert "Tori" in out
        assert "Chef" in out
        assert not _has_finding(findings, "agent_name")

    def test_agent_names_default_not_user_facing(self):
        out, findings = anonymize("Tori is the main agent.")
        assert "Tori" in out


# ── COMBINED ─────────────────────────────────────────────────────────────

class TestCombined:
    def test_all_categories_in_one_text(self):
        text = (
            "Adam built wisechef-agents at /home/adam/. "
            "Email: adam@hotmail.com. Tori runs it."
        )
        out, findings = anonymize(text, user_facing=True)
        assert "Adam" not in out
        assert "wisechef-agents" not in out
        assert "/home/adam/" not in out
        assert "adam@hotmail.com" not in out
        assert "Tori" not in out
        categories = {f.category for f in findings}
        assert "adam_token" in categories
        assert "infra_ref" in categories
        assert "path" in categories
        assert "email" in categories
        assert "agent_name" in categories

    def test_finding_has_original_value(self):
        _, findings = anonymize("Adam built this.")
        assert any(f.original == "Adam" for f in findings)

    def test_finding_has_replacement(self):
        _, findings = anonymize("Adam built this.")
        assert any(f.replacement == "<USER>" for f in findings)

    def test_clean_text_returns_empty_findings(self):
        _, findings = anonymize("Nothing to redact here.")
        assert findings == []

    def test_idempotent(self):
        text = "Adam deployed to wisechef-hq."
        out1, _ = anonymize(text)
        out2, _ = anonymize(out1)
        assert out1 == out2

    def test_finding_has_position(self):
        text = "Adam built this."
        _, findings = anonymize(text)
        f = findings[0]
        assert hasattr(f, "start")
        assert hasattr(f, "end")
