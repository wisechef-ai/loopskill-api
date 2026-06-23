# Follow-up: Cookbook->Bundle class-ref AST rename (deferred, NOT blocking)

The ORM classes are canonical Bundle* with `Cookbook = Bundle` compat aliases.
~418 call-site refs still use `Cookbook`/`CookbookSkill`/etc (resolve via alias —
functionally correct, cosmetically kitchen).

Regex rename attempted twice, broke 222 tests both times due to protected/unprotected
line desync between class-ref edits and import-line edits. Line-filtered regex is the
WRONG tool for this.

Correct approach when prioritized: AST-based rename (LibCST or rope) that renames the
symbol + its imports atomically, leaving only the alias-definition line + external-
contract strings. Until then, the `Cookbook = Bundle` alias is a legitimate permanent
backward-compat seam (standard Python pattern). Not a blocker for OSS readiness —
many mature libs keep such aliases.
