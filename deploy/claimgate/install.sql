-- claimgate: the last line of defense before Postiz publishes anything.
--
-- WHY A DATABASE TRIGGER (2026-10-06): a post created as DRAFT by Chef was
-- moved to QUEUE by an actor nobody can name (no agent session references it,
-- Postiz keeps no audit, the API is not logged) and published a retired
-- pricing ladder to X. Gating each producer cannot cover an unknown promoter,
-- the Postiz UI, or the next script someone writes. Every publish path,
-- however, has to put a row into public."Post" with state QUEUE first. This
-- trigger sits exactly there.
--
-- WHY QUARANTINE = deletedAt, NOT state=DRAFT: Postiz's post workflow
-- (post.workflow.v1.1.2) checks state only BEFORE it sleeps until publishDate;
-- after waking it reloads the row with deletedAt IS NULL and does NOT re-check
-- state. Flipping a queued row back to DRAFT does not stop it. A soft-deleted
-- row is invisible to both the workflow (getPost -> "No Post") and the hourly
-- missing-post sweep (WHERE deletedAt IS NULL). Restore = clear deletedAt.
--
-- RULES come from GET /api/marketing/claims (LoopSkill, derived from
-- config/tiers.yaml) and are synced into claimgate.rule by claimgate_sync.py.
-- Python (app/services/claims_contract.py) and this file run the SAME patterns
-- on the SAME normalised text; tests/test_claims_contract_pg_parity.py proves
-- it on the postgres CI leg. If the sync dies, the last synced rules keep
-- enforcing; a rule that errors makes the trigger fail CLOSED.
--
-- Also logs every state / deletedAt transition into claimgate.state_log so
-- "who queued this?" has an answer next time.
--
-- Idempotent: safe to re-run. Without a public."Post" table (CI) only the
-- functions are installed. Remove: DROP TRIGGER claimgate_guard ON "Post";

CREATE SCHEMA IF NOT EXISTS claimgate;

CREATE TABLE IF NOT EXISTS claimgate.rule (
    id           text PRIMARY KEY,
    kind         text NOT NULL,
    pg_pattern   text NOT NULL,
    amount_group int,
    allowed      numeric[],
    reason       text,
    synced_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE claimgate.rule ADD COLUMN IF NOT EXISTS allowed numeric[];
ALTER TABLE claimgate.rule DROP CONSTRAINT IF EXISTS rule_kind_check;
ALTER TABLE claimgate.rule ADD CONSTRAINT rule_kind_check CHECK (kind IN ('retired', 'amount'));
DROP TABLE IF EXISTS claimgate.allowed_price;  -- v1: replaced by rule.allowed (per-rule amounts)

-- Human escape hatch for a confirmed false positive: one row per post id.
CREATE TABLE IF NOT EXISTS claimgate.override (
    post_id     text PRIMARY KEY,
    approved_by text NOT NULL,
    reason      text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claimgate.meta (
    k  text PRIMARY KEY,
    v  text,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claimgate.state_log (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    post_id        text NOT NULL,
    op             text NOT NULL,
    old_state      text,
    new_state      text,
    old_deleted_at timestamptz,
    new_deleted_at timestamptz,
    publish_date   timestamptz,
    integration_id text,
    app_name       text DEFAULT current_setting('application_name', true),
    client_addr    inet DEFAULT inet_client_addr(),
    quarantined    boolean NOT NULL DEFAULT false,
    violations     text,
    alerted        boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS state_log_unalerted ON claimgate.state_log (at) WHERE quarantined AND NOT alerted;

-- Mirror of claims_contract.normalize(): strip tags (no space inserted),
-- decode entities, NBSP -> space, collapse ASCII whitespace, trim.
CREATE OR REPLACE FUNCTION claimgate.normalize(body text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    m text[];
    named constant text[][] := ARRAY[
        ['&nbsp;', ' '], ['&lt;', '<'], ['&gt;', '>'], ['&quot;', '"'], ['&apos;', ''''],
        ['&plus;', '+'], ['&euro;', '€'], ['&dollar;', '$'], ['&ndash;', '–'], ['&mdash;', '—'],
        ['&middot;', '·'], ['&hellip;', '…'], ['&rsquo;', '’'], ['&lsquo;', '‘'],
        ['&ldquo;', '“'], ['&rdquo;', '”']];
    i int;
BEGIN
    body := regexp_replace(coalesce(body, ''), '<[^>]+>', '', 'g');
    FOR m IN SELECT regexp_matches(body, '&#([0-9]{1,7});', 'g') LOOP
        BEGIN
            body := replace(body, '&#' || m[1] || ';', chr(m[1]::int));
        EXCEPTION WHEN others THEN NULL;
        END;
    END LOOP;
    FOR m IN SELECT regexp_matches(body, '&#[xX]([0-9a-fA-F]{1,6});', 'g') LOOP
        BEGIN
            body := replace(body, '&#x' || m[1] || ';', chr(('x' || lpad(m[1], 8, '0'))::bit(32)::int));
            body := replace(body, '&#X' || m[1] || ';', chr(('x' || lpad(m[1], 8, '0'))::bit(32)::int));
        EXCEPTION WHEN others THEN NULL;
        END;
    END LOOP;
    FOR i IN 1 .. array_length(named, 1) LOOP
        body := replace(body, named[i][1], named[i][2]);
    END LOOP;
    body := replace(body, '&amp;', '&');  -- last, so "&amp;lt;" stays "&lt;" like html.unescape
    body := replace(body, chr(160), ' ');
    body := regexp_replace(body, E'[ \\t\\r\\n\\f\\v]+', ' ', 'g');
    RETURN btrim(body);
END;
$fn$;

-- Returns NULL when clean, else a '; '-joined, de-duplicated list of
-- "rule_id" (retired) / "rule_id:amount" (amount) hits.
CREATE OR REPLACE FUNCTION claimgate.violations(body text) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    r    record;
    m    text[];
    amt  numeric;
    hits text[] := '{}';
BEGIN
    body := claimgate.normalize(body);
    IF body = '' THEN
        RETURN NULL;
    END IF;
    FOR r IN SELECT * FROM claimgate.rule WHERE kind = 'retired' LOOP
        IF body ~* r.pg_pattern THEN
            hits := hits || r.id;
        END IF;
    END LOOP;
    FOR r IN SELECT * FROM claimgate.rule WHERE kind = 'amount' LOOP
        FOR m IN SELECT regexp_matches(body, r.pg_pattern, 'gi') LOOP
            amt := round(replace(m[r.amount_group], ',', '.')::numeric, 2);
            IF NOT (amt = ANY (coalesce(r.allowed, '{}'::numeric[]))) THEN
                hits := hits || (r.id || ':' || amt::text);
            END IF;
        END LOOP;
    END LOOP;
    IF array_length(hits, 1) IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN array_to_string(ARRAY(SELECT DISTINCT h FROM unnest(hits) AS h ORDER BY h), '; ');
END;
$fn$;

CREATE OR REPLACE FUNCTION claimgate.guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    v      text;
    entering_queue boolean;
BEGIN
    entering_queue := NEW.state::text = 'QUEUE' AND NEW."deletedAt" IS NULL AND (
        TG_OP = 'INSERT'
        OR OLD.state::text IS DISTINCT FROM 'QUEUE'
        OR OLD."deletedAt" IS NOT NULL
        OR OLD.content IS DISTINCT FROM NEW.content
    );

    IF entering_queue AND NOT EXISTS (SELECT 1 FROM claimgate.override o WHERE o.post_id = NEW.id) THEN
        BEGIN
            v := claimgate.violations(NEW.content);
        EXCEPTION WHEN others THEN
            -- fail CLOSED: a broken rule must block publishing, not wave it through
            v := 'claimgate-error:' || SQLERRM;
        END;
        IF v IS NOT NULL THEN
            NEW."deletedAt" := now();
            INSERT INTO claimgate.state_log (post_id, op, old_state, new_state, old_deleted_at, new_deleted_at,
                                             publish_date, integration_id, quarantined, violations)
            VALUES (NEW.id, TG_OP, CASE WHEN TG_OP = 'UPDATE' THEN OLD.state::text END, NEW.state::text,
                    CASE WHEN TG_OP = 'UPDATE' THEN OLD."deletedAt" END, NEW."deletedAt",
                    NEW."publishDate", NEW."integrationId", true, v);
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP = 'INSERT'
       OR OLD.state IS DISTINCT FROM NEW.state
       OR OLD."deletedAt" IS DISTINCT FROM NEW."deletedAt" THEN
        INSERT INTO claimgate.state_log (post_id, op, old_state, new_state, old_deleted_at, new_deleted_at,
                                         publish_date, integration_id)
        VALUES (NEW.id, TG_OP, CASE WHEN TG_OP = 'UPDATE' THEN OLD.state::text END, NEW.state::text,
                CASE WHEN TG_OP = 'UPDATE' THEN OLD."deletedAt" END, NEW."deletedAt",
                NEW."publishDate", NEW."integrationId");
    END IF;
    RETURN NEW;
END;
$fn$;

DO $do$
BEGIN
    IF to_regclass('public."Post"') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS claimgate_guard ON public."Post";
        CREATE TRIGGER claimgate_guard
            BEFORE INSERT OR UPDATE ON public."Post"
            FOR EACH ROW EXECUTE FUNCTION claimgate.guard();
    END IF;
END;
$do$;

INSERT INTO claimgate.meta (k, v) VALUES ('installed_at', now()::text)
    ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, at = now();
