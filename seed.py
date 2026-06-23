"""Seed the database with sample data for development and testing.

Creates tables and populates with realistic seed data matching
the WIS-462 spec: users, api_keys, creators, skills, versions,
telemetry, carousel, recipes, api_library, demo_requests, payouts, referrals.
"""

import hashlib
import secrets
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.database import SessionLocal, engine
from app.models import (
    Base, User, APIKey, Creator, Org, Skill, SkillVersion,
    TelemetryEvent, CarouselEntry, InstallEvent, Recipe,
    APILibraryEntry, CreatorPayout, Referral, WiseChefDemoRequest,
)


def generate_api_key() -> tuple[str, str, str]:
    """Generate a rec_-prefixed API key. Returns (raw_key, prefix, sha256_hex)."""
    raw = f"rec_{secrets.token_hex(16)}"
    prefix = raw[:12]
    h = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, h


def seed():
    # Create all tables
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Check if already seeded
        if db.query(Skill).count() > 0:
            print("Database already seeded, skipping.")
            return

        now = datetime.now(timezone.utc)

        # ── Users ──
        user1 = User(
            id=uuid4(), email="team@wisechef.ai", display_name="WiseChef Team",
            avatar_url="https://wisechef.ai/logo.png",
        )
        user2 = User(
            id=uuid4(), email="hello@agentforgelabs.com", display_name="AgentForge Labs",
        )
        user3 = User(
            id=uuid4(), email="chef@wisechef.ai", display_name="Chef (Bot)",
            stripe_connect_id="acct_test_chef",
        )
        db.add_all([user1, user2, user3])
        db.flush()

        # ── API Keys (rec_ prefixed) ──
        # Generate a dev key and print it
        raw_key, key_prefix, key_hash = generate_api_key()
        api_key1 = APIKey(
            id=uuid4(), user_id=user1.id,
            key_prefix=key_prefix, key_hash=key_hash,
            name="development-key",
        )
        raw_key2, prefix2, hash2 = generate_api_key()
        api_key2 = APIKey(
            id=uuid4(), user_id=user2.id,
            key_prefix=prefix2, key_hash=hash2,
            name="agentforge-prod",
        )
        db.add_all([api_key1, api_key2])
        db.flush()

        print(f"  Generated API keys:")
        print(f"    WiseChef Team: {raw_key}")
        print(f"    AgentForge:    {raw_key2}")

        # ── Creators ──
        creator1 = Creator(
            id=uuid4(), user_id=user1.id, name="WiseChef Team", slug="wisechef-team",
            avatar_url="https://wisechef.ai/logo.png",
            bio="Core WiseChef development team", is_founder=True,
        )
        creator2 = Creator(
            id=uuid4(), user_id=user2.id, name="AgentForge Labs", slug="agentforge-labs",
            bio="Building the future of AI agent tooling", is_founder=True,
        )
        db.add_all([creator1, creator2])
        db.flush()

        # ── Orgs ──
        org1 = Org(
            id=uuid4(), name="WiseChef AI", slug="wisechef-ai",
            api_key_hash="dev_hash_placeholder",
        )
        db.add(org1)
        db.flush()

        # ── Skills ──
        skill1 = Skill(
            id=uuid4(), slug="web-scraper-pro", title="Web Scraper Pro",
            description="High-performance web scraping with built-in rate limiting and proxy rotation.",
            category="data-extraction", readme="# Web Scraper Pro\n\nFast, reliable scraping.",
            license="MIT", tier="pro", is_public=True, creator_id=creator2.id, org_id=org1.id,
        )
        skill2 = Skill(
            id=uuid4(), slug="email-composer", title="Smart Email Composer",
            description="AI-powered email drafting with tone control and template library.",
            category="communication", readme="# Smart Email Composer\n\nDraft emails with AI.",
            license="Apache-2.0", tier="pro", is_public=True, creator_id=creator1.id,
        )
        skill3 = Skill(
            id=uuid4(), slug="data-pipeline", title="Data Pipeline Builder",
            description="Visual pipeline builder for ETL workflows with 50+ connectors.",
            category="data-extraction", readme="# Data Pipeline Builder\n\nBuild ETL pipelines.",
            license="MIT", tier="pro_plus", is_public=True, creator_id=creator1.id,
        )
        skill4 = Skill(
            id=uuid4(), slug="code-reviewer", title="Code Review Bot",
            description="Automated code review with security scanning and best practices enforcement.",
            category="development", readme="# Code Review Bot\n\nAutomated code reviews.",
            license="MIT", tier="pro", is_public=True, creator_id=creator2.id,
        )
        skill5 = Skill(
            id=uuid4(), slug="image-generator", title="Image Generator",
            description="Generate images from text prompts using multiple AI models.",
            category="creative", readme="# Image Generator\n\nText-to-image generation.",
            license="MIT", tier="pro_plus", is_public=True, creator_id=creator2.id,
        )
        skill6 = Skill(
            id=uuid4(), slug="client-reporter", title="Client Reporter",
            description="Free skill: generate client-ready PDF reports from your data. No setup required.",
            category="reporting", readme="# Client Reporter\n\nFree viral skill for agencies.",
            license="Apache-2.0", tier="pro", is_public=True, creator_id=creator1.id,
        )
        db.add_all([skill1, skill2, skill3, skill4, skill5, skill6])
        db.flush()

        # ── Versions ──
        versions = [
            SkillVersion(id=uuid4(), skill_id=skill1.id, semver="1.2.0",
                    tarball_path="/storage/skills/web-scraper-pro-1.2.0.tar.gz",
                    tarball_size_bytes=245760, checksum_sha256="a" * 64,
                    changelog="Added proxy rotation support"),
            SkillVersion(id=uuid4(), skill_id=skill1.id, semver="1.1.0",
                    tarball_path="/storage/skills/web-scraper-pro-1.1.0.tar.gz",
                    tarball_size_bytes=204800, checksum_sha256="b" * 64,
                    changelog="Fixed rate limiting bug"),
            SkillVersion(id=uuid4(), skill_id=skill2.id, semver="2.0.1",
                    tarball_path="/storage/skills/email-composer-2.0.1.tar.gz",
                    tarball_size_bytes=184320, checksum_sha256="c" * 64,
                    changelog="New tone control feature"),
            SkillVersion(id=uuid4(), skill_id=skill3.id, semver="0.5.0",
                    tarball_path="/storage/skills/data-pipeline-0.5.0.tar.gz",
                    tarball_size_bytes=327680, checksum_sha256="d" * 64,
                    changelog="Initial public release"),
            SkillVersion(id=uuid4(), skill_id=skill4.id, semver="1.0.0",
                    tarball_path="/storage/skills/code-reviewer-1.0.0.tar.gz",
                    tarball_size_bytes=163840, checksum_sha256="e" * 64,
                    changelog="First stable release"),
            SkillVersion(id=uuid4(), skill_id=skill5.id, semver="0.9.0",
                    tarball_path="/storage/skills/image-generator-0.9.0.tar.gz",
                    tarball_size_bytes=409600, checksum_sha256="f" * 64,
                    changelog="Beta release"),
            SkillVersion(id=uuid4(), skill_id=skill6.id, semver="1.0.0",
                    tarball_path="/storage/skills/client-reporter-1.0.0.tar.gz",
                    tarball_size_bytes=102400,                    checksum_sha256="a1" * 32,
                    changelog="Initial release — free forever"),
        ]
        db.add_all(versions)
        db.flush()

        # ── Install Events ──
        installs = []
        for i in range(25):
            installs.append(InstallEvent(
                id=uuid4(), skill_id=skill1.id, skill_slug=skill1.slug,
                version_semver="1.2.0",
                created_at=now - timedelta(hours=i),
            ))
        for i in range(18):
            installs.append(InstallEvent(
                id=uuid4(), skill_id=skill2.id, skill_slug=skill2.slug,
                version_semver="2.0.1",
                created_at=now - timedelta(hours=i),
            ))
        for i in range(10):
            installs.append(InstallEvent(
                id=uuid4(), skill_id=skill6.id, skill_slug=skill6.slug,
                version_semver="1.0.0",
                created_at=now - timedelta(hours=i),
            ))
        db.add_all(installs)

        # ── Telemetry Events (simulated installs + page views) ──
        telemetry = []
        for i in range(50):
            telemetry.append(TelemetryEvent(
                id=uuid4(), event_type="install", skill_slug="web-scraper-pro",
                payload='{"version": "1.2.0"}',
                created_at=now - timedelta(hours=i),
            ))
        for i in range(35):
            telemetry.append(TelemetryEvent(
                id=uuid4(), event_type="install", skill_slug="email-composer",
                payload='{"version": "2.0.1"}',
                created_at=now - timedelta(hours=i),
            ))
        for i in range(20):
            telemetry.append(TelemetryEvent(
                id=uuid4(), event_type="install", skill_slug="code-reviewer",
                payload='{"version": "1.0.0"}',
                created_at=now - timedelta(days=i),
            ))
        for i in range(15):
            telemetry.append(TelemetryEvent(
                id=uuid4(), event_type="install", skill_slug="client-reporter",
                payload='{"version": "1.0.0", "source": "carousel"}',
                created_at=now - timedelta(hours=i),
            ))
        for i in range(10):
            telemetry.append(TelemetryEvent(
                id=uuid4(), event_type="page_view", skill_slug="data-pipeline",
                payload='{"source": "carousel"}',
                created_at=now - timedelta(hours=i),
            ))
        db.add_all(telemetry)

        # ── Carousel Entries (today + next 6 days = 7-day rotation) ──
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        carousel_data = [
            # Day 1 (today)
            [(skill1.id, "Scrape anything, anywhere", 0),
             (skill2.id, "Write emails that get replies", 1),
             (skill6.id, "Free: Client-ready PDF reports", 2)],
            # Day 2
            [(skill4.id, "Automate your code reviews", 0),
             (skill3.id, "50+ ETL connectors, zero config", 1),
             (skill5.id, "Text to stunning visuals", 2)],
            # Day 3
            [(skill6.id, "Agencies love this free tool", 0),
             (skill1.id, "Proxy rotation built-in", 1),
             (skill4.id, "Ship cleaner code faster", 2)],
            # Day 4
            [(skill2.id, "Tone control for every audience", 0),
             (skill5.id, "Multiple AI models in one skill", 1),
             (skill3.id, "Visual pipeline builder", 2)],
            # Day 5
            [(skill1.id, "Scrape at scale, no blocks", 0),
             (skill6.id, "Free forever — no signup needed", 1),
             (skill2.id, "Template library included", 2)],
            # Day 6
            [(skill4.id, "Security scanning included", 0),
             (skill3.id, "ETL in minutes, not days", 1),
             (skill5.id, "From prompt to image in seconds", 2)],
            # Day 7
            [(skill6.id, "The #1 free agency tool", 0),
             (skill1.id, "Rate limiting that works", 1),
             (skill4.id, "Best practices auto-enforced", 2)],
        ]
        carousel = []
        for day_offset, day_entries in enumerate(carousel_data):
            for skill_id, tagline, position in day_entries:
                carousel.append(CarouselEntry(
                    id=uuid4(), skill_id=skill_id,
                    featured_date=today + timedelta(days=day_offset),
                    tagline=tagline, position=position,
                ))
        db.add_all(carousel)

        # ── Recipes ──
        recipes = [
            Recipe(
                id=uuid4(), slug="build-scraper-pipeline",
                title="Build a Complete Scraping Pipeline",
                description="Combine Web Scraper Pro with Data Pipeline Builder for end-to-end extraction.",
                content="## Step 1: Install skills\n\n```\nwr install web-scraper-pro\nwr install data-pipeline\n```\n\n## Step 2: Configure\n\n...",
                category="tutorial", is_public=True, creator_id=creator1.id,
            ),
            Recipe(
                id=uuid4(), slug="automate-code-review",
                title="Set Up Automated Code Review",
                description="How to integrate Code Review Bot into your CI/CD pipeline.",
                content="## Prerequisites\n\n- GitHub Actions or GitLab CI\n- Code Review Bot v1.0+\n\n...",
                category="tutorial", is_public=True, creator_id=creator2.id,
            ),
        ]
        db.add_all(recipes)

        # ── API Library Entries ──
        api_entries = [
            APILibraryEntry(
                id=uuid4(), slug="wisechef-board-api",
                title="WiseChef Board API",
                description="REST API for managing WiseChef dashboard projects and deployments.",
                content="## Authentication\n\nUse `x-api-key` header...\n\n## Endpoints\n\n...",
                category="platform",
                base_url="https://api.wisechef.ai",
            ),
            APILibraryEntry(
                id=uuid4(), slug="wisechef-agents-api",
                title="WiseChef Agents API",
                description="Manage and orchestrate AI agents via REST API.",
                content="## Overview\n\nThe Agents API allows you to...\n\n...",
                category="platform",
                base_url="https://agents.wisechef.ai",
            ),
        ]
        db.add_all(api_entries)

        # ── Creator Payouts ──
        payout = CreatorPayout(
            id=uuid4(), creator_id=user2.id,
            period_start=now - timedelta(days=30), period_end=now,
            installs_count=85, gross_revenue_cents=50000,
            creator_share_cents=37500, currency="eur",
            status="paid", stripe_transfer_id="tr_test_123",
            paid_at=now,
        )
        db.add(payout)

        # ── Referrals ──
        referral = Referral(
            id=uuid4(), referrer_user_id=user1.id,
            referral_code="WISECHEF-FOUNDER-50",
            status="active",
            reward_cents=2500,
        )
        db.add(referral)

        # ── Demo Requests ──
        demo1 = WiseChefDemoRequest(
            id=uuid4(), email="ceo@acme-agency.com",
            company_name="Acme Digital", company_size="10-20",
            source="recipes_carousel", status="new",
        )
        demo2 = WiseChefDemoRequest(
            id=uuid4(), email="ops@bigmarketing.eu",
            company_name="Big Marketing Co", company_size="20-50",
            source="landing_page", status="contacted",
            contacted_at=now - timedelta(days=2),
        )
        db.add_all([demo1, demo2])

        db.commit()

        # Print summary
        print("Seed data inserted successfully!")
        print(f"  Users:            {db.query(User).count()}")
        print(f"  API Keys:         {db.query(APIKey).count()}")
        print(f"  Creators:         {db.query(Creator).count()}")
        print(f"  Skills:           {db.query(Skill).count()}")
        print(f"  Versions:         {db.query(SkillVersion).count()}")
        print(f"  Install Events:   {db.query(InstallEvent).count()}")
        print(f"  Telemetry Events: {db.query(TelemetryEvent).count()}")
        print(f"  Carousel Entries: {db.query(CarouselEntry).count()}")
        print(f"  Recipes:          {db.query(Recipe).count()}")
        print(f"  API Library:      {db.query(APILibraryEntry).count()}")
        print(f"  Payouts:          {db.query(CreatorPayout).count()}")
        print(f"  Referrals:        {db.query(Referral).count()}")
        print(f"  Demo Requests:    {db.query(WiseChefDemoRequest).count()}")

    except Exception as e:
        db.rollback()
        print(f"Error seeding: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
