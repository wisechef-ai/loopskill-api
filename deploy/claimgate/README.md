# claimgate — the Postiz publish gate for marketing claims

Every post that enters `QUEUE` in the Postiz database (from any producer, the
UI or an unknown actor) is checked against the LoopSkill claims contract
(`GET /api/marketing/claims`, derived from `config/tiers.yaml` +
`config/claims_contract.yaml`). A violating post is quarantined
(`deletedAt = now()`) before any publish workflow can load it.

| File | Runs where | Does |
|---|---|---|
| `install.sql` | Postiz Postgres (wisechef-hq, container `postiz-postgres`) | `claimgate` schema, `normalize()` / `violations()` / `guard()`, trigger `claimgate_guard` on `"Post"`, `state_log`, `override` |
| `claimgate_sync.py` | wisechef-hq crontab, every 10 min | Re-installs `install.sql` when its hash changes or the trigger is missing. Syncs rules transactionally (invalid pattern ⇒ old rules stay). Sweeps QUEUE with both engines. |
| `claimgate_redproof.py` | manual | Five cases inside one ROLLED-BACK transaction on the real DB: retired claim quarantined, clean passes, content edit re-checked, override honoured, broken rule fails closed |

Observation (alerts to #tori) is Tori's `claimgate-watch` cron. Enforcement
never depends on it.

## Deploy / update
```bash
scp deploy/claimgate/{install.sql,claimgate_sync.py,claimgate_redproof.py} wisechef-hq:services/claimgate/
ssh wisechef-hq 'cd ~/services/claimgate && python3 claimgate_sync.py'          # reinstalls + syncs
ssh wisechef-hq 'cd ~/services/claimgate && curl -s https://app.loopskill.io/api/marketing/claims > c.json && python3 claimgate_redproof.py c.json'
```
crontab (wisechef-hq):
`*/10 * * * * cd /home/wisechef/services/claimgate && python3 claimgate_sync.py >> /home/wisechef/logs/claimgate.log 2>&1`

## Operating
- **A post vanished after I queued it?** `SELECT * FROM claimgate.state_log WHERE post_id = '<id>' ORDER BY at;`
- **Confirmed false positive?** First run `INSERT INTO claimgate.override VALUES ('<post_id>', '<who>', '<why>');`, then clear `deletedAt` and re-queue. Also fix the rule or the prose in a PR and add the text to `CLEAN_CORPUS`.
- **A false claim got through?** Add a rule to `config/claims_contract.yaml` and the real text to `REAL_INCIDENTS` in `tests/test_claims_contract.py`. The parity test proves the trigger catches it too.
- **Who queued a post?** `state_log` records every state/`deletedAt` transition, with time, `application_name` and client address.

## Threat model: which text is checked
What a platform receives is Postiz's `stripHtmlValidation()`: **parse5** (HTML5) parse and serialize, then **striptags**, then an entity decode. That is parse5 6.0.1 and striptags 3.2.0, verified in the running container on 2026-10-06. The gate does not re-implement HTML5 tree construction. Instead:

1. **Markup is allowlisted.** `TAGLIKE` finds every token HTML5 treats as markup: `<` followed by an ASCII letter, `/`, `!` or `?`, up to the first `>`. Any token that is not a plain `ALLOWED_TAG` (`p br strong b em i u s ul ol li h1-h3 span` with no attributes, and `<a href="…">`) is itself a violation, **`unsupported-markup`**: comments, `<!`/`<?` constructs, unknown elements, odd quoting, unterminated tags. Production used only `<p>` and `<br>` across all 447 posts (2026-10-06), so legitimate copy never trips it. A `<` that HTML5 keeps as text (`We <3 you`, `<=`) stays text, as in the published post.
2. **For everything that passes the allowlist, the `join` reading equals the published text** in Postiz's plain-text mode. Modes that publish link targets are covered by the `link` and `markdown` readings. This is property-tested against Postiz's REAL converter in `tests/fixtures/postiz_pipeline.json`: 619 curated and seeded-fuzz cases, plus a 20,000-case local fuzz with 0 mismatches. `gen_postiz_pipeline_fixture.js` regenerates it.
3. **Every reading is checked and the violations are unioned** (`claims_normalize.TAG_READINGS`; `claimgate.violations`):
   - **join**: allowed tags are removed with no separator. This is Postiz's plain-text output.
   - **postiz**: opening tags that Postiz's own regexes turn into a line break (`<p…>`, `<li…>`, `<ul>`, and `h1`–`h3` on HTML platforms) become a separator; everything else, `<br>` included, joins.
   - **html**: default browser display. Block and line-break elements separate words; inline elements join them.
   - **space**: every tag separates words.
   - **link** and **markdown**: Postiz's replaceBold and markdown modes. A whole `<a href="U">T</a>` becomes `U` joined to the surrounding text, or `[T](U)`, matched the way Postiz's own regex matches it. Attributes other than `href` on `<a>` are unsupported markup: `data-mention-id` is rewritten differently on each platform, so it cannot be predicted.

Entities are decoded by an explicit table. Anything left undecoded is itself a violation (`unrecognised-html-entity`). Invisible and space-like characters are normalised. Numbers are read as whole runs, and malformed runs are violations.

### Regenerating the pipeline fixture
```bash
ssh wisechef-hq 'docker exec postiz sh -c "cd /app/node_modules && tar czf - parse5 entities striptags tslib"' > pz.tgz
mkdir -p pz/node_modules && tar xzf pz.tgz -C pz/node_modules
ssh wisechef-hq 'docker exec postiz cat /app/apps/backend/dist/libraries/helpers/src/utils/strip.html.validation.js' > pz/strip.js
node deploy/claimgate/gen_postiz_pipeline_fixture.js pz 600 > tests/fixtures/postiz_pipeline.json
```
`postiz_converter.lock.json` pins the converter this fixture came from: the `strip.html.validation.js` sha256 and the parse5 and striptags versions. Every `claimgate_sync.py` run compares the live container against it and records any difference in `claimgate.meta('converter_drift')`, and Tori's `claimgate-watch` reports it. After a Postiz upgrade, regenerate the fixture, run the tests, and update the lock in the same PR. Until then the gate keeps enforcing, but its model of the published text is unproven.

**Deliberately strict:** in plain-text mode Postiz joins list items with no separator (`…2 private bundlesPro: 50 private bundles`). A per-tier list can therefore bind a number to the previous tier and be quarantined. Write one `<p>` per line instead, as every production post already does.

**Tier binding** (`_tier_binding` / `claimgate.tier_binding`): a count binds to a tier attached after it (`50 private bundles on the Free tier`), else to the nearest tier before it in the sentence, else to the nearest tier after it. The exception: a tier after the count that has its own number after it (`50 private bundles (Free gives you 2)`) owns that number. Accepted gap: a trailing tier with no connector that is followed by another number is not bound.

**Out of scope** (accepted gaps, documented):
- Author CSS. Postiz strips tags and styles before publishing, so no platform renders it.
- HTML5 tree-construction effects (foster parenting, raw-text elements). Every element that has them is outside the allowlist and is therefore rejected.
- Spelled-out prices ("twenty dollars a month").
- Look-alike letters such as Cyrillic "Рro+".
- Price phrasings with more than 3 connecting words between tier and price.

Every false claim that ships, or any bypass found in review, becomes a fixture in `tests/test_claims_contract.py`. Fixtures are asserted with **expected verdicts** in the Python API, the SQL functions and the real trigger. Agreement between the two engines alone is not enough.
