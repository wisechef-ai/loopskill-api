# Graph Report - /home/adam/repos/loopskill-api  (2026-07-01)

## Corpus Check
- 189 files · ~154,639 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2303 nodes · 5715 edges · 93 communities detected
- Extraction: 40% EXTRACTED · 60% INFERRED · 0% AMBIGUOUS · INFERRED: 3419 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `Skill` - 361 edges
2. `User` - 234 edges
3. `Bundle` - 225 edges
4. `AuthContext` - 210 edges
5. `SkillVersion` - 135 edges
6. `BundleSkill` - 128 edges
7. `Creator` - 105 edges
8. `InstallEvent` - 105 edges
9. `FeedbackSubmission` - 71 edges
10. `FleetSubscription` - 67 edges

## Surprising Connections (you probably didn't know these)
- `Raised on authentication failures.` --uses--> `User`  [INFERRED]
  /home/adam/repos/loopskill-api/app/auth.py → /home/adam/repos/loopskill-api/app/models.py
- `Build the GitHub OAuth authorization URL.` --uses--> `User`  [INFERRED]
  /home/adam/repos/loopskill-api/app/auth.py → /home/adam/repos/loopskill-api/app/models.py
- `Exchange a GitHub OAuth code for user profile data.` --uses--> `User`  [INFERRED]
  /home/adam/repos/loopskill-api/app/auth.py → /home/adam/repos/loopskill-api/app/models.py
- `Build the Google OAuth authorization URL.` --uses--> `User`  [INFERRED]
  /home/adam/repos/loopskill-api/app/auth.py → /home/adam/repos/loopskill-api/app/models.py
- `Exchange a Google OAuth code for user profile data.` --uses--> `User`  [INFERRED]
  /home/adam/repos/loopskill-api/app/auth.py → /home/adam/repos/loopskill-api/app/models.py

## Communities

### Community 0 - "api_key.py"
Cohesion: 0.02
Nodes (252): APIKeyMiddleware, _auth_ctx_from_api_key(), _auth_ctx_from_jwt_cookie(), get_redis(), mark_redis_failed(), APIKeyMiddleware + key validation helpers.  Handles x-api-key header validation:, Opportunistically resolve an ``x-api-key`` header into an AuthContext.      Used, Get Redis client with lazy initialization, health check, and 30s backoff. (+244 more)

### Community 1 - "auth_ctx.py"
Cohesion: 0.03
Nodes (249): AuthContext, Authorization context — frozen dataclass describing the authenticated caller.  P, Immutable authentication context for a single request.      Attributes:, can_call_admin_mcp_tool(), can_install(), can_read_skill(), can_run_sandbox(), can_use_fleet() (+241 more)

### Community 2 - "access_routes.py"
Cohesion: 0.03
Nodes (127): Skills access check route — /api/skills/access.  Extracted from app/routes.py (P, Check whether the calling subscriber can access a skill.      Tier semantics (Pl, skill_access(), BaseModel, BootcampListResponse, BootcampStep, BootcampTrack, BootcampTrackSummary (+119 more)

### Community 3 - "bundle_external.py"
Cohesion: 0.03
Nodes (85): descriptor_source_slug(), external_slug(), install_descriptor_for(), is_external_skill(), known_external_source(), materialize_external_skill(), federation_0604 Unit 2 — cookbooks hold external (federated) skills.  This modul, Materialize a federated skill as a thin, PRIVATE ``Skill`` row.      Idempotent: (+77 more)

### Community 4 - "backfill_carousel_taglines.py"
Cohesion: 0.03
Nodes (98): derive_tagline(), main(), pick_1605 Phase C — backfill carousel tagline from skill.description.  Symptom (, Trim text to max_len at a word boundary and append ellipsis if truncated., Mirror the selector logic exactly — keep these in sync., _word_boundary_trim(), daily_carousel_job(), Carousel cron — daily_carousel_job(db, today).  Writes 7 CarouselEntry rows for (+90 more)

### Community 5 - "bundle_deployment_routes.py"
Cohesion: 0.03
Nodes (107): add_deployment(), apply_cookbook(), bundle_preflight(), cookbook_deploy_manifest(), _cookbook_dict(), cookbook_job_status(), create_deploy_cookbook(), DeployCookbookCreateRequest (+99 more)

### Community 6 - "canary.py"
Cohesion: 0.04
Nodes (83): Engine, get_patch_stats(), MetricsProvider, PropertyGate, B.7 — Canary pipeline.  Six-stage state machine for promoting a drafted patch to, Advance one tick. Returns (new_stage, reason_if_rolled_back).          For STATI, Return patch success/failure stats for the requested time period., A snapshot the metrics provider returns for one stage window. (+75 more)

### Community 7 - "demand_routes.py"
Cohesion: 0.06
Nodes (89): _activation_gap_theme(), build_demand_brief(), _cli(), _coinstall_cluster_themes(), demand_brief(), _norm(), Demand Brief — master-key-gated content-marketing direction feed.  `demandbrief_, Organic co-install clusters → 'send-this-as-a-cookbook' (MRR) themes.      When (+81 more)

### Community 8 - "fork_deploy.py"
Cohesion: 0.06
Nodes (65): _extract_skill_md(), _forks_dir(), recipes_tailor_version + recipes_cookbook_attach — close the MCP tailor loop.  l, Write a tarball under {SKILLS_DIR}/{slug}/{semver}.tar.gz with a     defense-in-, Return an error dict if the caller is not a Pro-tier user, else None.      Maste, Resolve a live fork owned by the caller, or None. Mirrors     forks_routes._reso, Upload a new version tarball to a fork (MCP-native, base64 transport).      Mirr, Deploy a tailored fork's latest version into a cookbook (the bridge).      Promo (+57 more)

### Community 9 - "4 deployable artifact types"
Cohesion: 0.06
Nodes (51): 4 deployable artifact types, Evergreen GitOps Control Plane, Drift Observability (Phase I), cookbook_drift_status(), CookbookDriftStatus, fleet_liveness(), Drift observability surface — evergreen_0206 Phase I.  NO NEW WRITE SURFACE. Thi, Count distinct agents that pinged within the window (from FleetPing).      Fleet (+43 more)

### Community 10 - "admin_routes.py"
Cohesion: 0.08
Nodes (44): admin_get_publish_request_tarball(), admin_pulse(), admin_reindex_all(), admin_update_publish_request_status(), _monthly_cents_from_stripe_sub(), PulseOut, Admin routes — master-key gated operations.  POST /api/admin/reindex-all — catas, Reindex BM25 search_vector for every non-archived skill.      Master-key only (a (+36 more)

### Community 11 - "federation_live.py"
Cohesion: 0.09
Nodes (35): browse_sh_fetch(), browse_sh_indexed_count(), browse_sh_origin_skill_md(), clawhub_fetch(), _clean(), github_oss_fetch(), _github_token(), hermes_hub_fetch() (+27 more)

### Community 12 - "feedback_status_routes.py"
Cohesion: 0.1
Nodes (31): _check_status_rate_limit(), FeedbackStatusOut, get_feedback_status(), Public status endpoint for feedback and recipify-request submissions.  Allows ca, Simple per-IP rate limiter for the public status endpoint., Return the current status and issue_url for a feedback or recipify-request row., FeedbackIn, FeedbackOut (+23 more)

### Community 13 - "BundleShareToken"
Cohesion: 0.11
Nodes (32): BundleShareToken, Share token for scoped delegation of bundle access (Phase 3).      Token format:, _create_service(), create_share_token(), _create_share_token_service(), enforce_cbt_scope(), _generate_token(), _get_cookbook_and_check_scope() (+24 more)

### Community 14 - "seeker.py"
Cohesion: 0.09
Nodes (29): _catalog_version(), _compare_versions(), diff_against_catalog(), installed_to_dict(), InstalledSkill, _linux_paths(), _macos_paths(), _parse_frontmatter() (+21 more)

### Community 15 - "reconcile_cli.py"
Cohesion: 0.09
Nodes (22): main(), _post_reconcile(), ``recipes-reconcile`` — the runnable thin reconcile client CLI (Phase J).  This, Call POST /api/reconcile with conditional If-None-Match.      Returns (status_co, Run one reconcile cycle. Returns a structured result dict.      Pure-ish: all I/, reconcile_once(), ApplyResult, default_health_check() (+14 more)

### Community 16 - "domain_proxy.py"
Cohesion: 0.11
Nodes (14): _domain_matches(), DomainProxy, Domain-filtering HTTPS proxy for sandbox network egress control.  Runs a local C, Process a single proxy connection., Handle a CONNECT (HTTPS) request., Handle a plain HTTP request through the proxy., Bidirectional data pipe between client and upstream., Create and start a domain proxy. Returns the proxy instance. (+6 more)

### Community 17 - "dict"
Cohesion: 0.22
Nodes (22): dict, demo_cta(), _live_mcp_tool_names(), _live_rest_paths(), marketing_counts(), marketing_snapshot(), Marketing surface counts — single source of truth for catalog stats.  Phase A of, Full marketing SSOT — counts merged with config/recipes-marketing.yaml.      Pha (+14 more)

### Community 18 - "BaseHTTPMiddleware"
Cohesion: 0.12
Nodes (15): BaseHTTPMiddleware, CookbookHostMiddleware, CookbookHostMiddleware — white-label custom-domain routing for Pro+ cookbooks., White-label custom-domain routing for Pro+ cookbooks (spotify_0608 Ph A).      W, get_redis(), mark_redis_failed(), Get Redis client with lazy initialization, health check, and 30s backoff.      A, Mark Redis as unavailable so next call retries connection. (+7 more)

### Community 19 - "sync_fanout.py"
Cohesion: 0.12
Nodes (12): emit_cookbook_event(), Fanout, get_fanout(), _is_postgres(), publish_event(), In-process fan-out for cookbook sync events — v7 Phase D.  Two transports:    *, Return the global Fanout singleton, creating it if needed., Test helper — drop all subscribers and reset event id counter. (+4 more)

### Community 20 - "federation_cache.py"
Cohesion: 0.17
Nodes (18): _is_stale(), _now(), Persistent federation index-cache layer — superset_0606 Phase B.  The storage ba, Read every cached source block, keyed by source id., Sum the indexed counts across source blocks, OMITTING null sources.      decisio, Sum the installable counts across source blocks, OMITTING null sources., True when the cache row is older than its TTL (or never walked)., Read one source's cached block, or None if never cached.      Returns a dict mat (+10 more)

### Community 21 - "federation_install.py"
Cohesion: 0.12
Nodes (18): clawhub_origin_skill_md(), get_origin_fetcher(), _github_default_branch(), github_tap_origin_skill_md(), _lobehub_convert_to_skill_md(), lobehub_origin_skill_md(), _parse_github_tree_url(), Federation install-resolution — per-source origin SKILL.md resolvers.  federatio (+10 more)

### Community 22 - "federation_fetch.py"
Cohesion: 0.15
Nodes (17): _canon_license(), guarded_get(), _is_blocked_ip(), is_redistributable(), is_safe_url(), normalize_install_leaf(), Federation security spine — SSRF-guarded HTTP + path-safety + license gate.  sup, Return True iff ``url`` does not target a private/internal/metadata host.      R (+9 more)

### Community 23 - "server.py"
Cohesion: 0.13
Nodes (17): _authenticate(), build_mcp_server(), call_tool_sync(), _ctx_from_caller(), _dispatch(), mcp_healthz(), mcp_messages(), mcp_sse() (+9 more)

### Community 24 - "tier_labels.py"
Cohesion: 0.18
Nodes (16): _canonical(), cookbook_limit(), display_label(), _is_operator_tier(), _is_paid_tier(), _is_pro_plus_tier(), _is_pro_tier(), Tier label helpers — load from config/tiers.yaml. For any user-facing text (API (+8 more)

### Community 25 - "version_staleness.py"
Cohesion: 0.19
Nodes (16): classify(), dispatch(), fetch_latest_github(), flag_publisher(), main(), open_auto_merge_pr(), parse_semver(), Daily version-staleness sweep (Phase F.8).  Walks every ``recipe.yaml`` in the c (+8 more)

### Community 26 - "fleet_routes.py"
Cohesion: 0.18
Nodes (16): create_fleet(), FleetCreateIn, list_fleets(), _raise_for_tool_error(), portal_0610 J3 — HTTP routes for fleet operations.  The fleet logic already exis, POST /api/fleets — create a named fleet. Returns the plaintext fleet_key ONCE., POST /api/fleets/{id}/subscribe — subscribe a cookbook on a channel (idempotent), POST /api/fleets/{id}/sync — sync every subscribed cookbook. dry_run previews. (+8 more)

### Community 27 - "github_taps_live.py"
Cohesion: 0.18
Nodes (15): _github_default_branch_cached(), _github_headers(), github_tap_fetch(), GitHub provider-facet tap reader — superset_0606 Phase C (the big steal).  Extra, Resolve (and cache) a repo's default branch via the REST API., Return the set of dir names under ``prefix`` that contain a SKILL.md.      One r, Walk one tap's skill dirs via the Contents API; resolve license per skill., Build a fetch callable for a GitHub provider-facet source.      Returns a closur (+7 more)

### Community 28 - "feedback_ratelimit.py"
Cohesion: 0.16
Nodes (14): check_and_record(), check_skill_error_backstop(), make_signature(), _purge(), _purge_dedup(), RateLimitResult, Multi-window rate limiter for feedback endpoints.  Windows (all enforced; any on, Check all windows and record the submission if allowed.      Returns a RateLimit (+6 more)

### Community 29 - "credits_routes.py"
Cohesion: 0.18
Nodes (13): CreditOut, list_my_credits(), app/credits_routes.py — subscriber-credit endpoints.  GET /api/me/credits   Requ, Return the authenticated user's subscriber credits, newest first.      Authentic, Contributor-discount credit for pro/pro_plus subscribers.      Granted automatic, SubscriberCredit, apply_credit_to_stripe_coupon(), expire_stale_credits() (+5 more)

### Community 30 - "loop_runner_support.py"
Cohesion: 0.14
Nodes (13): clamp_int(), kill_process_group(), make_rlimit_preexec(), Pure, stdlib-only helpers for the loop runner (split from app.loop_runner).  Ext, Build the MINIMAL env for the script: a clean base + caller-supplied vars., Return a preexec_fn that hardens the forked child before exec.      Applies POSI, Read proc's stdout+stderr with a per-stream byte cap and a wall timeout.      Re, SIGKILL the child's whole process group (it was started in a new session). (+5 more)

### Community 31 - "embeddings.py"
Cohesion: 0.18
Nodes (13): cosine(), embed_skill(), embed_text(), _get_model(), _hash_embed(), is_model_loaded(), Local sentence-transformer embeddings for skill recall.  Uses BAAI/bge-small-en-, True if the real sentence-transformer model is in memory. (+5 more)

### Community 32 - "selector.py"
Cohesion: 0.2
Nodes (13): _assign_role(), _has_same_category_older(), _is_new(), Carousel selector — score(skill, today) + select_top_7(db, today).  Scoring algo, Assign carousel role per contract:      slot 1 — new-capability if created withi, Return a list of up to 7 dicts ready to write as CarouselEntry rows.      Each d, exp(-days_since_created / 30). Returns 1.0 when created_at is None., Compute carousel score for *skill* relative to *today*.      Contract formula (v (+5 more)

### Community 33 - "streaming.py"
Cohesion: 0.2
Nodes (11): _build_streamable_http_mount(), get_http_session_manager(), SSE + StreamableHTTP transport glue for the MCP server.  Contains:   _sse_transp, Async context manager that starts the StreamableHTTP session manager's     task, Run the MCP server on stdio (for Claude Desktop & similar)., Lazy-initialise the StreamableHTTP session manager.      Must be called at app s, Reset the global session manager (for tests only)., Create a Starlette Mount that forwards all requests to the session     manager's (+3 more)

### Community 34 - "reconcile_abuse_ceiling.py"
Cohesion: 0.24
Nodes (11): CeilingResult, _check_memory(), check_reconcile_abuse_ceiling(), _check_redis(), Per-agent reconcile abuse ceiling — evergreen_0206 Phase A.  Decision #20 (locke, Clear the in-memory fallback window (test hygiene)., Outcome of an abuse-ceiling check., Sliding-window check via Redis sorted set. None → Redis unavailable. (+3 more)

### Community 35 - "skill_patch_validation.py"
Cohesion: 0.18
Nodes (11): canonical_hash(), check_size(), _matches_any_glob(), app/skill_patch_validation.py — pure validation module (no DB I/O).  Enforces R1, Scan content for forbidden shell execution patterns.      Returns a list of matc, Check that the patch stays within hard size limits.      files: list of {"path":, Compute a stable dedup hash for a skill-patch submission.      Hash = sha256( sl, Return True if path matches any of the given glob patterns. (+3 more)

### Community 36 - "skill_quality_gate.py"
Cohesion: 0.26
Nodes (9): GateFinding, _is_private_or_example_ip(), skill_quality_gate — importable library form of scripts/skill_quality_gate.py., Scan a tarball given as bytes; return list of finding dicts.      Designed for t, Scan a skill directory on disk; returns findings list.      Convenience wrapper, Returns True for IPs that should NOT be flagged as recon disclosure.      Includ, scan_directory(), scan_tarball_bytes() (+1 more)

### Community 37 - "skill_file_cache.py"
Cohesion: 0.21
Nodes (11): clear_cache(), _evict_if_full(), get_or_build(), _has_single_root_dir(), LRU file-manifest cache for skill tarball contents.  Keyed on (skill_id, version, Return cached tarball data, or build and cache it.      Returns {"manifest": [.., Flush the entire cache.  Test-use only., Pop the least-recently-used entry until under cap. Caller holds _lock. (+3 more)

### Community 38 - "ranking.py"
Cohesion: 0.24
Nodes (10): _bm25_score_text(), combine(), Pluggable scorers for /api/recall — vector + BM25 + signal combiner., Combine signals into a final score in roughly [0, 1+].      Final ≈ 0.6·vec + 0., Cosine similarity in [0, 1] (clamped — negative cosine treated as 0)., Lightweight in-process BM25 over a single document.      Used as the SQLite fall, BM25 score for a single skill row.      On Postgres the route may pre-compute vi, score_bm25() (+2 more)

### Community 39 - "security_scan.py"
Cohesion: 0.31
Nodes (10): _check_requiredenv(), _in_references_dir(), _in_scripts_dir(), _mk(), §7.2 Security Scanner for recipes skill tarballs.  Implements scan_tarball() per, Scan a .tar.gz skill package for the 10 security patterns.      Returns a list o, Flag credential-type env vars declared by unrelated skill categories., Return True if the tarball path is inside a scripts/ directory. (+2 more)

### Community 40 - "bundle_wellknown_routes.py"
Cohesion: 0.27
Nodes (10): cookbook_wellknown_index(), cookbook_wellknown_skill_md(), _is_free(), Well-known skills bridge — serve a public cookbook as an agentskills.io bundle., agentskills.io discovery index for a public cookbook.      Public (no auth). 404, Serve one skill's SKILL.md from a public cookbook bundle.      Public (no auth)., A skill's body is publicly serveable iff it is free., A non-leaking SKILL.md for a PAID skill.      Carries the agentskills.io frontma (+2 more)

### Community 41 - "backfill_skill_titles.py"
Cohesion: 0.27
Nodes (10): _cap_word(), derive_title(), main(), _parse_frontmatter_field(), backfill_skill_titles.py — fix Skill.title where it equals slug.  Symptom (valid, Regex-based frontmatter parser. Returns the value of `field` or None.      PyYAM, Capitalize a word, preserving acronyms., Hyphens → spaces, capitalize per word, but preserve known acronyms     and known (+2 more)

### Community 42 - "carousel_selector.py"
Cohesion: 0.25
Nodes (10): assign_role(), derive_tagline(), main(), Carousel selector — runs daily at 23:55 UTC to pick tomorrow's 7 skills.  Algori, Minimal role assignment. slots 1-5 → new-capability, 6-7 → experimental.      Th, Trim text to max_len at a word boundary and append ellipsis if truncated., Return the tagline a candidate would publish. description-first.      `p` is the, Slot-1 lint per skill `carousel-content-quality-gate`.      Returns (passed, dro (+2 more)

### Community 43 - "conversion_gates.py"
Cohesion: 0.25
Nodes (10): gate_cookbook_create(), gate_daemon_cron_install(), gate_fleet(), gate_manual_sync(), GateOutcome, Maintenance-gated conversion ladder — evergreen_0206 Phase G.  The paid axis is, A manual (human-initiated) reconcile/sync.      Paid tiers: always allowed (Pro, Installing the scheduled reconcile daemon-cron is a PRO capability.      Free ca (+2 more)

### Community 44 - "feedback_cred_vault.py"
Cohesion: 0.24
Nodes (9): decrypt_pat(), encrypt_pat(), _get_fernet(), app/feedback_cred_vault.py — Encrypted PAT storage for user-routable feedback., Return a Fernet instance backed by WR_FEEDBACK_CRED_KEY.      Raises ValueError, Encrypt a GitHub PAT using Fernet.  Returns base64url ciphertext string.      Ra, Decrypt a Fernet-encrypted PAT ciphertext.      Returns the plaintext PAT string, Return a safe log-representation of a token (first 4 chars + ***).      NEVER ca (+1 more)

### Community 45 - "vat.py"
Cohesion: 0.24
Nodes (9): calculate_vat(), generate_vat_moss_report(), EU VAT MOSS calculation for digital services sold cross-border.  VAT MOSS (Mini, Basic EU VAT number format validation.      Production should use VIES (VAT Info, Generate a VAT MOSS report for a given period.      Args:         payouts_by_cou, Result of VAT calculation., Calculate VAT MOSS for a digital service sale.      Args:         gross_amount_c, _validate_vat_number() (+1 more)

### Community 46 - "federation_mcp.py"
Cohesion: 0.27
Nodes (9): build_mcp_server_config(), _is_http_endpoint(), REGISTER_MCP install path — federated MCP-server skills.  Until now ``InstallPat, Derive a stable, client-safe server key from a namespaced slug.      ``github-an, True only for a syntactically valid http(s) URL with a network host.      Guards, Pick the registrable MCP endpoint for a REGISTER_MCP skill.      Preference orde, Build the paste-ready MCP config block for a REGISTER_MCP external skill.      A, resolve_mcp_endpoint() (+1 more)

### Community 47 - "feedback_github.py"
Cohesion: 0.29
Nodes (9): create_issue(), _gh_request(), app/feedback_github.py — GitHub issue dispatcher for user-routable feedback.  Ar, Return True if ``token`` has issues:write access to ``repo``.      Calls GET /re, Create a GitHub issue in ``repo`` and return the issue URL.      Returns the HTM, Raise ValueError if ``repo`` doesn't look like a valid GitHub owner/name.      R, Make a GitHub API request.  Returns (status_code, body_bytes).      Never logs t, _validate_repo() (+1 more)

### Community 48 - "revenue_alerts.py"
Cohesion: 0.27
Nodes (9): _build_embed_payload(), _color_for(), _emoji_for(), post_revenue_event(), Revenue event alerts — ping Discord on subscription state changes.  Wires into t, Construct the Discord-compatible JSON body., Deliver to Discord. Webhook URL preferred; bot fallback only if absent., Fire-and-forget Discord ping for a revenue-relevant event.      ``event_kind`` i (+1 more)

### Community 49 - "semver.py"
Cohesion: 0.24
Nodes (9): latest_semver_for_skills(), latest_version_row_for_skill(), max_semver(), Semantic version comparison — portal_0610 B2 (§6.6).  The bug this closes: every, Return a sortable key for a semver string. Larger key = newer version.      Unpa, Return the semantically-greatest semver in *semvers*, or None if empty.      Ski, Return {skill_id: latest_semver} computed SEMANTICALLY (not lexically).      Rep, Return the semantically-latest SkillVersion row for *skill_id*, or None.      po (+1 more)

### Community 50 - "_registry_k.py"
Cohesion: 0.2
Nodes (9): _fleet_tools(), _publish_tools(), Phase D/E/J/K inline tools — share tokens, fleet, publish, tailor, fork.  Extrac, Phase D share-token management tools (loopskill_* primary names)., Publish-request tool (loopskill_* primary name)., integrator_2905 W1 + loopclose_3005 Phase C/I tailor/fork tools., Phase E fleet tools (loopskill_* primary names)., _share_tools() (+1 more)

### Community 51 - "reconcile_host_detect.py"
Cohesion: 0.28
Nodes (8): cron_template(), detect_hosts(), DetectedHost, Host agent auto-detection + one-command install — evergreen_0206 Phase D.  Decis, Return every agent host whose skills dir exists under *home*.      home defaults, Pick the host to install onto.      prefer (an explicit --host) wins if present, Render a host-appropriate reconcile cron line / unit.      For Hermes: a cron pr, select_host()

### Community 52 - "BaseSettings"
Cohesion: 0.25
Nodes (8): BaseSettings, _assert_production_secrets(), public_origin(), WiseRecipes API — configuration via env vars., Raise RuntimeError if any default change-me secret is present in a non-sqlite en, Resolve the public origin used to build install / download URLs.      Single sea, _run_production_checks(), Settings

### Community 53 - "startup_checks.py"
Cohesion: 0.25
Nodes (8): check_alembic_heads(), post_tori_alert(), Boot-time startup checks for WiseRecipes API.  These checks run during the FastA, Raise RuntimeError if the database is not at alembic head in non-sqlite envs., Post a plain-text alert to the #tori Discord webhook.      Fire-and-forget, sync, Boot-time smoke test: assert the Stripe webhook endpoint is registered correctly, # NOTE: stripe SDK rejects `timeout=` as a per-call kwarg on resource, verify_stripe_webhook_endpoint()

### Community 54 - "__main__.py"
Cohesion: 0.25
Nodes (7): create_app(), lifespan(), main(), ``python -m app.mcp`` — run the Recipes MCP server on stdio., Create and configure the FastAPI application instance., Entry point: run the MCP server over stdio., Start/stop the Discord bot alongside the API.      Bot is a no-op when DISCORD_B

### Community 55 - "sse_routes.py"
Cohesion: 0.36
Nodes (6): cookbook_sync_sse(), _gate_acquire(), _gate_init(), _gate_release(), Server-Sent Events live-sync endpoint — v7 Phase D.  ``GET /api/cookbooks/{id}/s, Stream Server-Sent Events for real-time cookbook sync updates.

### Community 56 - "skill_serve_routes.py"
Cohesion: 0.32
Nodes (7): _canonical_skill_md(), Canonical /skill serve route — loopclose_3005 Phase B.  `/skill` is the install, Remove mirror-bot leak-header comment lines from the served body.      The bot i, Read + clean the canonical SKILL.md once (cached for process lifetime)., Serve the canonical, clean SKILL.md as text/plain (no redirect).      Mounted at, serve_canonical_skill(), _strip_leak_headers()

### Community 57 - "_config_block_formatter.py"
Cohesion: 0.32
Nodes (7): _build_claude_desktop_json(), build_config_blocks(), _build_hermes_yaml(), Config block formatter for cookbook share tokens.  Generates Hermes YAML and Cla, Return Hermes YAML + Claude Desktop JSON snippets for a share token.      Args:, Build a Hermes YAML config snippet for the given share token., Build a Claude Desktop JSON mcpServers snippet for the given share token.

### Community 58 - "_alias_map.py"
Cohesion: 0.29
Nodes (6): make_compat_alias_tools(), normalize_tool_name(), Canonical loopskill_* → back-compat recipes_* alias map for MCP tool dispatch., # NOTE: ``cookbook`` → ``bundle`` applies for tools that operate on bundles., Map a loopskill_* canonical name to its recipes_* dispatch name.      Back-compa, Return ``recipes_*`` compat-alias Tool entries for every ``loopskill_*`` tool.

### Community 59 - "anonymizer.py"
Cohesion: 0.33
Nodes (5): anonymize(), Finding, app/services/anonymizer.py  Regex-based anonymizer for skill content before it e, A single redaction finding., Replace sensitive tokens in *text* and return (cleaned, findings).      Rules ap

### Community 60 - "doctor.py"
Cohesion: 0.38
Nodes (6): _looks_like_remote_path(), recipes_doctor — local install audit.  Walks an install_dir and flags missing fi, Return True if the path shape suggests it lives on a different host.      The se, Audit a server-visible skill install directory.      Returns the standard audit, recipes_doctor(), _scan_file_for_paths()

### Community 61 - "role_sync.py"
Cohesion: 0.4
Nodes (5): Map a user's subscription state → Discord role.  Roles:   pro_plus (active), Return the Discord role(s) for a user.      Default behaviour returns the *base*, Apply the role to the user's Discord account, if any.      `client` must expose, role_for_user(), sync_role_for_user()

### Community 62 - "bundle_loader.py"
Cohesion: 0.4
Nodes (5): load_cookbook_file(), Bundle-definition loader (spotify_0608 Ph A — re-homed from bucket_loader).  Bun, Recursively drop dict keys that start with '_'.      Lists are walked; non-conta, Parse a cookbook JSON file and strip comment keys., strip_comments()

### Community 63 - "heartbeat_client.py"
Cohesion: 0.4
Nodes (5): Heartbeat client — opt-OUT via RECIPES_TELEMETRY env.  This module ships in the, Return True if the RECIPES_TELEMETRY env var is set to a disabled value., Post a heartbeat. Returns {"skipped": True} when opt-out is set,     otherwise {, send_heartbeat(), telemetry_disabled()

### Community 64 - "client_ip.py"
Cohesion: 0.4
Nodes (5): _is_trusted(), Trusted-proxy-aware real-client-IP extraction (Issue #12).  Only honour CF-Conne, Return the real visitor IP, honouring proxy headers only from trusted CIDR peers, Return True if *host* falls inside any of *trusted_cidrs*., _real_client_ip()

### Community 65 - "search_index.py"
Cohesion: 0.4
Nodes (5): BM25 search index — pure Postgres tsvector, no embeddings.  Embeddings deferred, Rebuild the BM25 search_vector for a single skill.      On Postgres this uses ``, Reindex every non-archived skill.  Returns the count reindexed.      For catastr, reindex_all(), reindex_bm25()

### Community 66 - "bot.py"
Cohesion: 0.4
Nodes (4): Phase D — Discord bot lifespan + slash command stubs.  If DISCORD_BOT_TOKEN is u, Start the Discord bot if a token is configured; otherwise no-op.      Returns th, _resolve_token(), start_bot()

### Community 67 - "github_dispatch.py"
Cohesion: 0.33
Nodes (5): dispatch_event(), dispatch_issue(), GitHub repository_dispatch + issue-creation helper.  Default path: POST reposito, POST repository_dispatch to wisechef-ai/recipes-api.      Returns True on succes, Create a GitHub issue in ``repo`` using ``token``.      Phase J — user-routable

### Community 68 - "_registry_d.py"
Cohesion: 0.33
Nodes (5): _phase_d_tools(), _phase_e_tools(), spotify_0608 Ph D tool definitions — streaming cookbook-composition verbs.  Spli, spotify_0608 Ph E tool definitions — provenance-aware feedback surface.      Mov, Return the spotify_0608 Ph D (streaming composition) tool definitions.

### Community 69 - "channel_select.py"
Cohesion: 0.4
Nodes (3): latest_version_for_channel(), Channel-aware version selection — evergreen_0206 Phase C.  A fleet subscribes a, Return the target semver for *skill_id* on *channel*, or None.      canary → max

### Community 70 - "registry.py"
Cohesion: 0.5
Nodes (4): _core_tools(), MCP tool registry — _tool_definitions() returns the advertised types.Tool list., Core loopskill_* tools: search, install, bundle-install, list, recall, etc., _tool_definitions()

### Community 71 - "bundle_status.py"
Cohesion: 0.8
Nodes (4): _cache_set(), get_bundle_status(), invalidate_bundle_status(), _redis_client()

### Community 72 - "github_taps.py"
Cohesion: 0.4
Nodes (4): GitHubTap, GitHub tap-list: the 6 provider facets + curated github-oss allowlist.  superset, One curated GitHub tap = a facet source id + its {repo, path} + trust tier., NamedTuple

### Community 73 - "search.py"
Cohesion: 0.5
Nodes (3): recipes_search — full-text catalog search with hybrid recall fallback.  Backed b, Search the public catalog by keyword, with hybrid fallback when sparse.      Ret, recipes_search()

### Community 74 - "subrecipe_resolve.py"
Cohesion: 0.5
Nodes (3): recipes_subrecipe_resolve — Phase C (sub-recipe key minting).  Phase A always re, Phase C stub: resolve a sub-recipe key to a scope.      Phase G update: returns, recipes_subrecipe_resolve()

### Community 75 - "_registry_j.py"
Cohesion: 0.5
Nodes (3): _phase_j_tools(), Phase J tool definitions — split out to keep registry.py under 600 lines.  Post, Return the Phase J (loopclose_3005) tool definitions.

### Community 76 - "recall.py"
Cohesion: 0.5
Nodes (3): recipes_recall — Phase E hybrid recall (vector + BM25).  Wraps :func:`app.recall, Hybrid BM25 + vector skill recall ranked for the caller's tier., recipes_recall()

### Community 77 - "client_singleton.py"
Cohesion: 0.5
Nodes (1): Process-global accessor for the Discord role client.  The bot lifespan stamps a

### Community 78 - "carousel_today.py"
Cohesion: 0.5
Nodes (3): recipes_carousel_today — proxy for today's curated carousel., Return today's curated carousel of skills., recipes_carousel_today()

### Community 79 - "auth_propagate.py"
Cohesion: 0.5
Nodes (3): _caller_from_request_context(), AuthContext propagation for MCP SSE/StreamableHTTP transports.  Contains _caller, Return the caller dict stashed on the active request, or a stdio fallback.

### Community 80 - "_registry_loopskill.py"
Cohesion: 0.5
Nodes (3): _loopskill_catalog_tools(), LoopSkill Phase 8 tool definitions — split out to keep registry.py under 600 lin, MCP discovery tools for the runnable catalog types (loops, personalities).

### Community 81 - "database.py"
Cohesion: 0.5
Nodes (3): get_db(), Database engine and session management., Yield a SQLAlchemy session and close it after the request.

### Community 82 - "utm_redirects.py"
Cohesion: 0.67
Nodes (1): UTM short-link redirectors — /x/, /li/, /ig/, /yt/, /fb/.  Extracted from app/ro

### Community 83 - "carousel_verdict.py"
Cohesion: 0.67
Nodes (1): Day-7 verdict cron — judge skills that exited the carousel 7 days ago.  Verdict

### Community 84 - "_registry_bundle.py"
Cohesion: 0.67
Nodes (1): Phase 3+4 bundle-vocabulary MCP tools (new canonical names).  Extracted here to

### Community 85 - "_public_paths.py"
Cohesion: 1.0
Nodes (1): Public (no-auth) path allowlists for APIKeyMiddleware.  Extracted from ``api_key

### Community 86 - "dispatch.py"
Cohesion: 1.0
Nodes (1): MCP dispatch shim — re-exports from app.mcp.server for backward compat.  The act

### Community 87 - "Return an anonymous (unauthenticated) context."
Cohesion: 1.0
Nodes (1): Return an anonymous (unauthenticated) context.

### Community 88 - "Run all production-safety checks after all fields are resolved."
Cohesion: 1.0
Nodes (1): Run all production-safety checks after all fields are resolved.

### Community 89 - "Get the actual visitor IP, respecting trusted-proxy CIDRs (Issue #12)."
Cohesion: 1.0
Nodes (1): Get the actual visitor IP, respecting trusted-proxy CIDRs (Issue #12).

### Community 90 - "Parse a skill.toml string and extract the [sandbox] block."
Cohesion: 1.0
Nodes (1): Parse a skill.toml string and extract the [sandbox] block.

### Community 91 - "Conservative default profile — no network, no writes, 256MB, 60s."
Cohesion: 1.0
Nodes (1): Conservative default profile — no network, no writes, 256MB, 60s.

### Community 92 - "Reconstruct from a ``to_dict()`` payload (the cached first_page shape)."
Cohesion: 1.0
Nodes (1): Reconstruct from a ``to_dict()`` payload (the cached first_page shape).

## Knowledge Gaps
- **343 isolated node(s):** `Config block formatter for cookbook share tokens.  Generates Hermes YAML and Cla`, `Return Hermes YAML + Claude Desktop JSON snippets for a share token.      Args:`, `Build a Hermes YAML config snippet for the given share token.`, `Build a Claude Desktop JSON mcpServers snippet for the given share token.`, `Authorization context — frozen dataclass describing the authenticated caller.  P` (+338 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `_public_paths.py`** (2 nodes): `_public_paths.py`, `Public (no-auth) path allowlists for APIKeyMiddleware.  Extracted from ``api_key`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `dispatch.py`** (2 nodes): `dispatch.py`, `MCP dispatch shim — re-exports from app.mcp.server for backward compat.  The act`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Return an anonymous (unauthenticated) context.`** (1 nodes): `Return an anonymous (unauthenticated) context.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Run all production-safety checks after all fields are resolved.`** (1 nodes): `Run all production-safety checks after all fields are resolved.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Get the actual visitor IP, respecting trusted-proxy CIDRs (Issue #12).`** (1 nodes): `Get the actual visitor IP, respecting trusted-proxy CIDRs (Issue #12).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Parse a skill.toml string and extract the [sandbox] block.`** (1 nodes): `Parse a skill.toml string and extract the [sandbox] block.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Conservative default profile — no network, no writes, 256MB, 60s.`** (1 nodes): `Conservative default profile — no network, no writes, 256MB, 60s.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Reconstruct from a ``to_dict()`` payload (the cached first_page shape).`** (1 nodes): `Reconstruct from a ``to_dict()`` payload (the cached first_page shape).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Skill` connect `auth_ctx.py` to `api_key.py`, `selector.py`, `access_routes.py`, `search_index.py`, `backfill_carousel_taglines.py`, `bundle_deployment_routes.py`, `canary.py`, `demand_routes.py`, `fork_deploy.py`, `search.py`, `admin_routes.py`, `backfill_skill_titles.py`, `bundle_external.py`, `4 deployable artifact types`, `seeker.py`, `dict`?**
  _High betweenness centrality (0.262) - this node is a cross-community bridge._
- **Why does `User` connect `api_key.py` to `auth_ctx.py`, `access_routes.py`, `bundle_deployment_routes.py`, `fork_deploy.py`, `admin_routes.py`, `fleet_routes.py`, `credits_routes.py`?**
  _High betweenness centrality (0.107) - this node is a cross-community bridge._
- **Why does `Bundle` connect `auth_ctx.py` to `api_key.py`, `access_routes.py`, `bundle_deployment_routes.py`, `demand_routes.py`, `bundle_wellknown_routes.py`, `fork_deploy.py`, `4 deployable artifact types`, `BundleShareToken`, `dict`, `BaseHTTPMiddleware`?**
  _High betweenness centrality (0.066) - this node is a cross-community bridge._
- **Are the 358 inferred relationships involving `Skill` (e.g. with `Shared helper functions for skill-related route handlers.  Extracted from app/ro` and `F-API-14: Build manifest dict from skill.toml for install response.`) actually correct?**
  _`Skill` has 358 INFERRED edges - model-reasoned connections that need verification._
- **Are the 232 inferred relationships involving `User` (e.g. with `Shared creator-resolution helper used by publisher_routes and recipify.  Extract` and `Return an existing Creator row for ctx.user_id, or create one on the fly.      R`) actually correct?**
  _`User` has 232 INFERRED edges - model-reasoned connections that need verification._
- **Are the 221 inferred relationships involving `Bundle` (e.g. with `CreateKeyIn` and `API key management routes — generate, list, revoke.  Phase C (top1pct_1105): - M`) actually correct?**
  _`Bundle` has 221 INFERRED edges - model-reasoned connections that need verification._
- **Are the 208 inferred relationships involving `AuthContext` (e.g. with `Shared creator-resolution helper used by publisher_routes and recipify.  Extract` and `Return an existing Creator row for ctx.user_id, or create one on the fly.      R`) actually correct?**
  _`AuthContext` has 208 INFERRED edges - model-reasoned connections that need verification._