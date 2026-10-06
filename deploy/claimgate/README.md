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
The gate checks the post's source `content`, as written by producers or the Postiz editor, under every tag reading a real consumer uses. Tags are located by a port of Postiz's own `striptags@3.2.0` state machine, not by a regex. The port is checked byte for byte against real striptags outputs in `tests/fixtures/striptags_3_2_0.json`, which `gen_striptags_fixture.js` regenerates. A violation in any reading counts (`claims_normalize.TAG_READINGS`; `claimgate.violations`):

- **join**: every tag is removed with no separator. This is what Postiz sends to plain-text platforms (`stripHtmlValidation`: `<p>` becomes a newline, everything else goes through `striptags`, verified in the running container on 2026-10-06).
- **postiz**: exactly what Postiz publishes. Opening tags matched by its regexes (`<p…>`, `<li…>`, `<ul>`, and `h1`–`h3` on HTML platforms) become a line break, and every other tag, `<br>` included, is removed with no separator (`POSTIZ_BREAK`).
- **html**: default browser display. Block and line-break elements (`BLOCK_TAG`) separate words; inline elements join them.
- **space**: every tag separates words.

Entities are decoded by an explicit table. Anything left undecoded is itself a violation (`unrecognised-html-entity`). Invisible and space-like characters are normalised. Numbers are read as whole runs, and malformed runs are violations.

**Out of scope** (accepted gaps, documented):
- Author CSS. Postiz strips tags and styles before publishing, so no platform renders it.
- Spelled-out prices ("twenty dollars a month").
- Look-alike letters such as Cyrillic "Рro+".
- Price phrasings with more than 3 connecting words between tier and price.

Every false claim that ships, or any bypass found in review, becomes a fixture in `tests/test_claims_contract.py`. Fixtures are asserted with **expected verdicts** in the Python API, the SQL functions and the real trigger. Agreement between the two engines alone is not enough.
