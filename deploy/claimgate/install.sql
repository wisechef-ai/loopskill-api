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
-- Fails CLOSED: an empty rule table (contract never loaded) or a rule that
-- errors / captures nothing raises inside violations(), and guard() turns any
-- exception into a quarantine. Never "no rules = everything passes".
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
-- functions are installed. Remove: DROP TRIGGER claimgate_guard ON public."Post";

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
ALTER TABLE claimgate.rule ADD CONSTRAINT rule_kind_check CHECK (kind IN ('retired', 'amount', 'exempt'));
ALTER TABLE claimgate.rule ADD COLUMN IF NOT EXISTS exemptable boolean NOT NULL DEFAULT false;
ALTER TABLE claimgate.rule ADD COLUMN IF NOT EXISTS veto text;
DROP TABLE IF EXISTS claimgate.allowed_price;  -- v1: replaced by rule.allowed (per-rule amounts)

-- Public tiers for the nearest-tier binding check (synced from the contract).
CREATE TABLE IF NOT EXISTS claimgate.tier (
    name       text PRIMARY KEY,
    bundle_cap int,
    key_cap    int
);

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
CREATE INDEX IF NOT EXISTS state_log_post ON claimgate.state_log (post_id, new_state);
CREATE INDEX IF NOT EXISTS state_log_unalerted ON claimgate.state_log (at) WHERE quarantined AND NOT alerted;

-- Mirror of claims_contract.NAMED_ENTITIES + _decode_entity: ONE pass, left
-- to right; numeric (";" optional, matched whole), the named table (";"
-- required) and LEGACY_NO_SEMICOLON names without ";"; code
-- points 0, surrogates and > U+10FFFF stay literal. GENERATED from the Python
-- table: tests/test_claims_contract_pg_parity.py fails if they drift.
CREATE OR REPLACE FUNCTION claimgate.decode_entities(body text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    pat   constant text := '&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]{0,31};|(nbsp|amp|lt|gt|quot|copy|reg|times|divide|middot|cent|pound|yen|shy))';
    named constant text[][] := ARRAY[
        ['amp', '&'],
        ['lt', '<'],
        ['gt', '>'],
        ['quot', '"'],
        ['apos', ''''],
        ['nbsp', ' '],
        ['Tab', '	'],
        ['NewLine', '
'],
        ['plus', '+'],
        ['num', '#'],
        ['percnt', '%'],
        ['excl', '!'],
        ['quest', '?'],
        ['colon', ':'],
        ['semi', ';'],
        ['comma', ','],
        ['period', '.'],
        ['sol', '/'],
        ['bsol', '\'],
        ['lpar', '('],
        ['rpar', ')'],
        ['ast', '*'],
        ['equals', '='],
        ['lowbar', '_'],
        ['dollar', '$'],
        ['euro', '€'],
        ['pound', '£'],
        ['cent', '¢'],
        ['yen', '¥'],
        ['copy', '©'],
        ['reg', '®'],
        ['trade', '™'],
        ['times', '×'],
        ['divide', '÷'],
        ['middot', '·'],
        ['hellip', '…'],
        ['ndash', '–'],
        ['mdash', '—'],
        ['dash', '‐'],
        ['hyphen', '‐'],
        ['lsquo', '‘'],
        ['rsquo', '’'],
        ['ldquo', '“'],
        ['rdquo', '”'],
        ['shy', ''],
        ['ensp', ' '],
        ['emsp', ' '],
        ['thinsp', ' ']];
    pos   int := 1;
    p     int;
    tok   text;
    ent text;
    rep   text;
    cp    bigint;
    i     int;
    -- GENERATED from claims_normalize.C1_REMAP (index = code point - 127)
    c1    constant text[] := ARRAY['€', chr(129), '‚', 'ƒ', '„', '…', '†', '‡', 'ˆ', '‰', 'Š', '‹', 'Œ', chr(141), 'Ž', chr(143), chr(144), '‘', '’', '“', '”', '•', '–', '—', '˜', '™', 'š', '›', 'œ', chr(157), 'ž', 'Ÿ'];
BEGIN
    LOOP
        p := regexp_instr(body, pat, pos);
        EXIT WHEN p = 0;
        tok := regexp_substr(body, pat, pos);
        ent := rtrim(substr(tok, 2), ';');
        rep := tok;
        IF left(ent, 1) = '#' THEN
            IF substr(ent, 2, 1) IN ('x', 'X') THEN
                ent := coalesce(nullif(ltrim(substr(ent, 3), '0'), ''), '0');
                cp := CASE WHEN length(ent) > 6 THEN NULL
                           ELSE ('x' || lpad(ent, 8, '0'))::bit(32)::bigint END;
            ELSE
                ent := coalesce(nullif(ltrim(substr(ent, 2), '0'), ''), '0');
                cp := CASE WHEN length(ent) > 7 THEN NULL ELSE ent::bigint END;
            END IF;
            IF cp IS NOT NULL AND NOT (cp = 0 OR cp BETWEEN 55296 AND 57343 OR cp > 1114111) THEN
                rep := CASE WHEN cp BETWEEN 128 AND 159 THEN c1[cp - 127] ELSE chr(cp::int) END;
            END IF;
        ELSE
            FOR i IN 1 .. array_length(named, 1) LOOP
                IF named[i][1] = ent THEN
                    rep := named[i][2];
                    EXIT;
                END IF;
            END LOOP;
        END IF;
        body := left(body, p - 1) || rep || substr(body, p + length(tok));
        pos := p + length(rep);
    END LOOP;
    RETURN body;
END;
$fn$;

-- Mirror of claims_contract.normalize(): strip tags (no space inserted),
-- decode entities once, NBSP -> space, collapse ASCII whitespace, trim.
-- GENERATED from claims_normalize (TAGLIKE, ALLOWED_TAG, LINK, LI_BLOCK,
-- P_BLOCK, postiz_bold, postiz_markdown, markup_structure_error); every
-- literal is pinned by tests and the readings are property-tested against
-- Postiz's real converter (tests/fixtures/postiz_pipeline.json).
DROP FUNCTION IF EXISTS claimgate.tag_sep(text, text);
CREATE OR REPLACE FUNCTION claimgate.li_flat(html text, pre text, post text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    pos int := 1;
    p   int;
    tok text;
    rep text;
BEGIN
    LOOP
        p := regexp_instr(html, '<li>((([^<]|<+([^/<]|/[^l]|/l[^i]|/li[^>]))*)<*)</li>', pos, 1, 0, 'i');
        EXIT WHEN p = 0;
        tok := regexp_substr(html, '<li>((([^<]|<+([^/<]|/[^l]|/l[^i]|/li[^>]))*)<*)</li>', pos, 1, 'i');
        rep := pre || regexp_replace((regexp_match(tok, '<li>((([^<]|<+([^/<]|/[^l]|/l[^i]|/li[^>]))*)<*)</li>', 'i'))[1], '[\r\n]', '', 'g') || post;
        html := left(html, p - 1) || rep || substr(html, p + length(tok));
        pos := p + length(rep);
    END LOOP;
    RETURN html;
END;
$fn$;

CREATE OR REPLACE FUNCTION claimgate.strip_tags(html text, reading text DEFAULT 'join') RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
BEGIN
    IF reading NOT IN ('join', 'postiz', 'html', 'space', 'link', 'markdown') THEN
        RAISE EXCEPTION 'claimgate: unknown tag reading %', reading;
    END IF;
    IF reading = 'link' THEN  -- claims_normalize.postiz_bold
        html := regexp_replace(html, '^<p>', '', 'i');
        html := regexp_replace(html, '<p>', chr(10), 'gi');
        html := regexp_replace(html, '</p>', '', 'gi');
        html := regexp_replace(html, '<a href="([^"<>]*)">((([^<]|<+([^/<]|/[^a]|/a[^>]))*)<*)</a>', '\1', 'gi');
        html := regexp_replace(html, '<ul>', chr(10) || '<ul>', 'i');
        html := regexp_replace(html, '</ul>' || chr(10), '</ul>', 'i');
        html := claimgate.li_flat(html, '- ', chr(10));
    ELSIF reading = 'markdown' THEN  -- claims_normalize.postiz_markdown
        html := regexp_replace(html, '<h1>((([^<]|<+([^/<]|/[^h]|/h[^1]|/h1[^>]))*)<*)</h1>', '<h1># \1</h1>', 'gi');
        html := regexp_replace(html, '</h1>', '</h1>' || chr(10), 'gi');
        html := regexp_replace(html, '<h2>((([^<]|<+([^/<]|/[^h]|/h[^2]|/h2[^>]))*)<*)</h2>', '<h2>## \1</h2>', 'gi');
        html := regexp_replace(html, '</h2>', '</h2>' || chr(10), 'gi');
        html := regexp_replace(html, '<h3>((([^<]|<+([^/<]|/[^h]|/h[^3]|/h3[^>]))*)<*)</h3>', '<h3>### \1</h3>', 'gi');
        html := regexp_replace(html, '</h3>', '</h3>' || chr(10), 'gi');
        html := regexp_replace(html, '<u>((([^<]|<+([^/<]|/[^u]|/u[^>]))*)<*)</u>', '<u>__\1__</u>', 'gi');
        html := regexp_replace(html, '<strong>((([^<]|<+([^/<]|/[^s]|/s[^t]|/st[^r]|/str[^o]|/stro[^n]|/stron[^g]|/strong[^>]))*)<*)</strong>', '<strong>**\1**</strong>', 'gi');
        html := claimgate.li_flat(html, '<li>- ', '</li>');
        html := regexp_replace(html, '<p>((([^<]|<+([^/<]|/[^p]|/p[^>]))*)<*)</p>', '<p>\1</p>' || chr(10), 'gi');
        html := regexp_replace(html, '<a href="([^"<>]*)">((([^<]|<+([^/<]|/[^a]|/a[^>]))*)<*)</a>', '[\2](\1)', 'gi');
    ELSIF reading = 'space' THEN
        RETURN regexp_replace(html, '<[A-Za-z/!?][^>]*>?', ' ', 'g');
    ELSIF reading = 'postiz' THEN
        html := regexp_replace(html, '<(p|li|ul|h[1-3])[^>]*>?', ' ', 'gi');
    ELSIF reading = 'html' THEN
        html := regexp_replace(html, '</?(address|article|aside|blockquote|br|caption|center|dd|details|dialog|div|dl|dt|fieldset|figcaption|figure|footer|form|h[1-6]|header|hgroup|hr|legend|li|main|menu|nav|ol|p|pre|section|summary|table|tbody|td|tfoot|th|thead|tr|ul)\y[^>]*>?', ' ', 'gi');
    END IF;
    RETURN regexp_replace(html, '<[A-Za-z/!?][^>]*>?', '', 'g');
END;
$fn$;

CREATE OR REPLACE FUNCTION claimgate.markup_structure_error(body text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    st  text[] := '{}';
    tok text;
    m   text[];
    nm  text;
    top text;
BEGIN
    FOR tok IN SELECT x.mm[1] FROM regexp_matches(coalesce(body, ''), '(' || '<[A-Za-z/!?][^>]*>?' || ')', 'g')
                    WITH ORDINALITY AS x(mm, n) ORDER BY x.n LOOP
        m := regexp_match(lower(tok), '^<(/?)([a-z0-9]+)');
        CONTINUE WHEN m IS NULL OR m[2] = 'br';
        nm := m[2];
        top := coalesce(st[array_length(st, 1)], '');
        IF m[1] = '/' THEN
            IF top <> nm THEN
                RETURN '</' || nm || '> closes <' || CASE WHEN top = '' THEN 'nothing' ELSE top END || '>';
            END IF;
            st := st[1:array_length(st, 1) - 1];
            CONTINUE;
        END IF;
        IF nm IN ('p', 'ul', 'ol', 'h1', 'h2', 'h3') AND top NOT IN ('', 'li') THEN
            RETURN '<' || nm || '> inside <' || top || '>';
        END IF;
        IF nm = 'li' AND top NOT IN ('ul', 'ol') THEN
            RETURN '<li> inside <' || CASE WHEN top = '' THEN 'top level' ELSE top END || '>';
        END IF;
        IF nm NOT IN ('p', 'ul', 'ol', 'h1', 'h2', 'h3', 'li') AND (top IN ('ul', 'ol') OR (nm = 'a' AND 'a' = ANY (st))) THEN
            RETURN '<' || nm || '> inside <' || top || '>';
        END IF;
        st := st || nm;
    END LOOP;
    RETURN CASE WHEN array_length(st, 1) > 0 THEN '<' || st[array_length(st, 1)] || '> is never closed' ELSE '' END;
END;
$fn$;

CREATE OR REPLACE FUNCTION claimgate.markup_hits(body text) RETURNS text[]
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM regexp_matches(coalesce(body, ''), '(' || '<[A-Za-z/!?][^>]*>?' || ')', 'g') AS m
         WHERE m[1] !~* '^(</?(p|strong|b|em|i|u|s|ul|ol|li|h[1-3]|span)>|<br ?/?>|<a href="[^"<>]*">|</a>)$')
      OR claimgate.markup_structure_error(body) <> ''
    THEN ARRAY['unsupported-markup'] ELSE '{}'::text[] END
$fn$;

DROP FUNCTION IF EXISTS claimgate.normalize(text);
DROP FUNCTION IF EXISTS claimgate.normalize(text, text);
CREATE OR REPLACE FUNCTION claimgate.normalize(body text, reading text DEFAULT 'join') RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $fn$
BEGIN
    body := claimgate.strip_tags(coalesce(body, ''), reading);
    body := claimgate.decode_entities(body);
    -- GENERATED from claims_contract.ZERO_WIDTH / SPACE_LIKE (parity-tested)
    body := translate(body, chr(173)||chr(8203)||chr(8204)||chr(8205)||chr(8288)||chr(65279), '');
    body := translate(body, chr(133)||chr(160)||chr(5760)||chr(8192)||chr(8193)||chr(8194)||chr(8195)||chr(8196)||chr(8197)||chr(8198)||chr(8199)||chr(8200)||chr(8201)||chr(8202)||chr(8232)||chr(8233)||chr(8239)||chr(8287)||chr(12288), repeat(' ', 19));
    body := regexp_replace(body, E'[ \\t\\r\\n\\f\\v]+', ' ', 'g');
    RETURN btrim(body, ' ');
END;
$fn$;

-- Mirror of claims_contract._tier_binding(): each "N private bundles" /
-- "N API keys" binds to an attached trailing tier ("on the Free tier"),
-- else the NEAREST preceding tier name in its sentence, else the nearest
-- following one
-- (sentence end = ". " / "! " / "? ") and must equal that tier's cap.
CREATE OR REPLACE FUNCTION claimgate.tier_binding(body text) RETURNS text[]
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    unit_pat constant text := '(^|[^0-9,. ]|(^|[^0-9])[,. ])([0-9]+([.,][0-9]+| [0-9]+)*) (private bundles?|((active |scoped |separate |client )?(API )?keys|(active |scoped |separate |client )?API key|(active|scoped|separate|client) key))\y';
    names  text;
    pos    int := 1;
    p      int;
    e      int;
    sstart int;
    tok    text;
    tok2   text;
    m      text[];
    last_name text;
    t      record;
    cap    int;
    unitk  text;
    cpos   int;
    hits   text[] := '{}';
BEGIN
    SELECT string_agg(regexp_replace(name, '([.^$*+?()\[\]{}|\\])', '\\\1', 'g'), '|') INTO names
      FROM claimgate.tier;
    IF names IS NULL THEN
        RAISE EXCEPTION 'claimgate: contract not loaded (tier table empty)';
    END IF;
    LOOP
        p := regexp_instr(body, unit_pat, pos, 1, 0, 'i');
        EXIT WHEN p = 0;
        tok := regexp_substr(body, unit_pat, pos, 1, 'i');
        m := regexp_match(tok, unit_pat, 'i');
        -- the count starts AFTER the guard char m[1] (claims_contract: m.start(2))
        cpos := p + length(coalesce(m[1], ''));
        sstart := 1;
        e := 1;
        LOOP
            e := regexp_instr(body, '[.!?] ', e, 1, 1);
            EXIT WHEN e = 0 OR e > cpos;
            sstart := e;
        END LOOP;
        -- attached > nearest preceding > nearest following (claims_contract._tier_binding)
        last_name := (regexp_match(substr(body, p + length(tok)),
                                   '^,? (on|with|in|for|under) (the |a |an |your |our )?\y(' || names || ')\y', 'i'))[3];
        IF last_name IS NULL THEN
            SELECT x.mm[1] INTO last_name
              FROM regexp_matches(substr(body, sstart, cpos - sstart), '\y(' || names || ')\y', 'gi')
                   WITH ORDINALITY AS x(mm, ord)
             ORDER BY x.ord DESC LIMIT 1;
        END IF;
        IF last_name IS NULL THEN
            e := regexp_instr(body, '[.!?] ', p + length(tok));
            IF e = 0 THEN e := length(body) + 1; END IF;
            tok2 := substr(body, p + length(tok), e - (p + length(tok)));
            last_name := (regexp_match(tok2, '\y(' || names || ')\y', 'i'))[1];
            -- a following name with its own number after it owns that number
            IF last_name IS NOT NULL
               AND substr(tok2, regexp_instr(tok2, '\y(' || names || ')\y', 1, 1, 1, 'i')) ~ '[0-9]' THEN
                last_name := NULL;
            END IF;
        END IF;
        IF last_name IS NOT NULL THEN
            SELECT * INTO t FROM claimgate.tier WHERE lower(name) = lower(last_name);
            unitk := CASE WHEN lower(m[5]) LIKE '%key%' THEN 'key' ELSE 'bundle' END;
            cap := CASE WHEN unitk = 'key' THEN t.key_cap ELSE t.bundle_cap END;
            IF cap IS NOT NULL AND claimgate.parse_amount(m[3]) IS DISTINCT FROM cap THEN
                hits := hits || ('tier-' || unitk || '-cap');
            END IF;
        END IF;
        last_name := NULL;
        pos := p + greatest(length(tok) - 1, 1);  -- claims_contract._scan
    END LOOP;
    RETURN hits;
END;
$fn$;

-- Mirror of claims_contract.parse_amount (THOUSANDS literal pinned by tests).
CREATE OR REPLACE FUNCTION claimgate.parse_amount(t text) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $fn$
BEGIN
    IF t !~ '^(([0-9]{1,3}(,[0-9]{3})+([.][0-9]{1,2})?|[0-9]{1,3}([.][0-9]{3})+(,[0-9]{1,2})?|[0-9]{1,3}( [0-9]{3})+([.,][0-9]{1,2})?)|[0-9]+([.,][0-9]{1,2})?)$' THEN
        RETURN NULL;  -- malformed: the caller treats NULL as a violation
    END IF;
    IF t ~ '^([0-9]{1,3}(,[0-9]{3})+([.][0-9]{1,2})?|[0-9]{1,3}([.][0-9]{3})+(,[0-9]{1,2})?|[0-9]{1,3}( [0-9]{3})+([.,][0-9]{1,2})?)$' THEN
        t := replace(t, substring(t from '[., ]'), '');
    END IF;
    RETURN replace(t, ',', '.')::numeric;
EXCEPTION WHEN others THEN
    RETURN NULL;  -- defensive: never raise from the check (Python: NaN)
END;
$fn$;

-- Returns NULL when clean, else a '; '-joined, de-duplicated list of
-- "rule_id" (retired) / "rule_id:amount" (amount) hits.
CREATE OR REPLACE FUNCTION claimgate.violations_norm(body text) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    r    record;
    m    text[];
    amt  numeric;
    hits text[] := '{}';
    body_ex text;
    src  text;
    pos  int;
    p    int;
    tok  text;
    e    int;
    sstart int;
    send int;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM claimgate.rule WHERE kind = 'retired')
       OR NOT EXISTS (SELECT 1 FROM claimgate.rule WHERE kind = 'amount') THEN
        RAISE EXCEPTION 'claimgate: contract not loaded (rule table empty)';
    END IF;
    IF body = '' THEN
        RETURN NULL;
    END IF;
    FOR r IN SELECT * FROM claimgate.rule WHERE kind = 'retired' LOOP
        IF body ~* r.pg_pattern THEN
            hits := hits || r.id;
        END IF;
    END LOOP;
    -- mirror of check_text(): generic price rules skip other products' prices
    -- stated in their own context; tier-bound rules see the full text
    body_ex := body;
    FOR r IN SELECT * FROM claimgate.rule WHERE kind = 'exempt' ORDER BY id LOOP
        IF r.veto IS NULL THEN
            RAISE EXCEPTION 'claimgate: exempt rule % has no veto', r.id;
        END IF;
        pos := 1;
        LOOP
            p := regexp_instr(body_ex, r.pg_pattern, pos, 1, 0, 'i');
            EXIT WHEN p = 0;
            tok := regexp_substr(body_ex, r.pg_pattern, pos, 1, 'i');
            -- veto checks the WHOLE sentence around the match (claims_contract._apply_exemptions)
            sstart := 1;
            e := 1;
            LOOP
                e := regexp_instr(body_ex, '[.!?] ', e, 1, 1);
                EXIT WHEN e = 0 OR e > p;
                sstart := e;
            END LOOP;
            send := regexp_instr(body_ex, '[.!?] ', p + length(tok));
            IF send = 0 THEN
                send := length(body_ex) + 1;
            END IF;
            IF substr(body_ex, sstart, send - sstart) ~* r.veto THEN
                pos := p + length(tok);
            ELSE
                body_ex := left(body_ex, p - 1) || ' ' || substr(body_ex, p + length(tok));
                pos := p + 1;
            END IF;
        END LOOP;
    END LOOP;
    FOR r IN SELECT * FROM claimgate.rule WHERE kind = 'amount' LOOP
        src := CASE WHEN r.exemptable THEN body_ex ELSE body END;
        pos := 1;
        LOOP
            p := regexp_instr(src, r.pg_pattern, pos, 1, 0, 'i');
            EXIT WHEN p = 0;
            tok := regexp_substr(src, r.pg_pattern, pos, 1, 'i');
            m := regexp_match(tok, r.pg_pattern, 'i');
            -- restart ONE char before the end: the last char of this match may
            -- be the next match's guard (claims_contract._scan)
            pos := p + greatest(length(tok) - 1, 1);
            IF r.amount_group IS NULL OR m[r.amount_group] IS NULL THEN
                RAISE EXCEPTION 'claimgate: rule % captured no amount (group %)', r.id, r.amount_group;
            END IF;
            amt := round(claimgate.parse_amount(m[r.amount_group]), 2);
            IF amt IS NULL OR NOT (amt = ANY (coalesce(r.allowed, '{}'::numeric[]))) THEN
                hits := hits || (r.id || ':' || coalesce(amt::text, 'malformed'));
            END IF;
        END LOOP;
    END LOOP;
    hits := hits || claimgate.tier_binding(body);
    IF array_length(hits, 1) IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN array_to_string(ARRAY(SELECT DISTINCT h FROM unnest(hits) AS h ORDER BY h), '; ');
END;
$fn$;

-- Mirror of claims_contract.check_text: the text is checked under every tag
-- reading (claims_normalize.TAG_READINGS); a violation in any of them counts.
CREATE OR REPLACE FUNCTION claimgate.violations(body text) RETURNS text
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    hits text := '';
    rd   text;
    nb   text;
    done text[] := '{}';
BEGIN
    hits := '; ' || array_to_string(claimgate.markup_hits(body), '; ');
    FOREACH rd IN ARRAY ARRAY['join', 'postiz', 'html', 'space', 'link', 'markdown'] LOOP
        nb := claimgate.normalize(body, rd);
        CONTINUE WHEN nb = ANY (done);  -- identical readings are checked once
        done := done || nb;
        hits := hits || '; ' || coalesce(claimgate.violations_norm(nb), '');
    END LOOP;
    hits := array_to_string(ARRAY(
        SELECT DISTINCT h FROM unnest(string_to_array(hits, '; ')) AS h WHERE h <> '' ORDER BY h), '; ');
    RETURN nullif(hits, '');
END;
$fn$;

CREATE OR REPLACE FUNCTION claimgate.guard() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    v      text;
    entering_queue boolean;
BEGIN
    entering_queue := NEW."deletedAt" IS NULL AND (
        (NEW.state::text = 'QUEUE' AND (
            TG_OP = 'INSERT'
            OR OLD.state::text IS DISTINCT FROM 'QUEUE'
            OR OLD."deletedAt" IS NOT NULL
            OR OLD.content IS DISTINCT FROM NEW.content))
        -- A post that is or ever was queued may still have a sleeping publish
        -- workflow, which reloads the row WITHOUT re-checking state. So for
        -- such a post, a content change or an un-delete is gated in ANY state
        -- (QUEUE -> DRAFT + new content, or "restore" of a quarantined row).
        OR (TG_OP = 'UPDATE'
            AND (OLD.content IS DISTINCT FROM NEW.content OR OLD."deletedAt" IS NOT NULL)
            AND (OLD.state::text = 'QUEUE'
                 OR EXISTS (SELECT 1 FROM claimgate.state_log l
                             WHERE l.post_id = NEW.id AND (l.new_state = 'QUEUE' OR l.old_state = 'QUEUE'))))
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
        -- Rows already queued at install time get a history row, so their
        -- later DRAFT edits / un-deletes are gated like any other ever-queued post.
        INSERT INTO claimgate.state_log (post_id, op, new_state, new_deleted_at, publish_date, integration_id)
        SELECT p.id, 'SNAPSHOT', p.state::text, p."deletedAt", p."publishDate", p."integrationId"
          FROM public."Post" p
         WHERE p.state::text = 'QUEUE' AND p."deletedAt" IS NULL
           AND NOT EXISTS (SELECT 1 FROM claimgate.state_log l
                            WHERE l.post_id = p.id AND (l.new_state = 'QUEUE' OR l.old_state = 'QUEUE'));
    END IF;
END;
$do$;

INSERT INTO claimgate.meta (k, v) VALUES ('installed_at', now()::text)
    ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, at = now();
