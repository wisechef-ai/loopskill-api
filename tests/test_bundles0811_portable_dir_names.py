"""bundles_0811 — a bundle member's name must be usable as a DIRECTORY.

THE DEFECT
----------
Both installers do ``mkdir -p <dest>/<name>``, so a member's ``name`` becomes a
real directory on the caller's disk. Materialized federated members are slugged
``ext:<source>:<upstream-slug>`` — and a colon is **illegal in a Windows path**
(it is the drive separator).

Measured against prod 2026-08-11 across all 10 public bundles:

    114 / 172 members  carried a name that cannot be a directory on Windows
     58 / 172 members  were safe (the purely-local ones)

Every federated member of every seeded bundle was affected. Reproduced locally:
``loopskill apply terraform-and-kubernetes-ops --write`` created
``ext:hermes-hub:skills-sh-wshobson-agents-k8s-manifest-generator/`` — fine on
Linux, impossible on Windows.

THE SHAPE OF THE FIX
--------------------
``name`` is the WIRE KEY: ``/.well-known/skills/{skill_name}/SKILL.md`` resolves
a member by ``skill.slug == skill_name``. Renaming it would break the download
for every existing client. So ``dir_name`` is a SEPARATE, additive field:
``name`` to fetch, ``dir_name`` to mkdir.
"""

from __future__ import annotations

from app.bundle_wellknown_routes import portable_dir_name

WINDOWS_ILLEGAL = set(':*?"<>|')


class TestPortableDirName:
    def test_federated_slug_loses_its_colons(self):
        out = portable_dir_name("ext:hermes-hub:skills-sh-mindrally-skills-terraform")
        assert ":" not in out
        assert out == "ext-hermes-hub-skills-sh-mindrally-skills-terraform"

    def test_a_plain_local_slug_is_untouched(self):
        """The 58 safe members must not churn."""
        for slug in ("copywriting", "humanizer", "agentic-os", "code-reviewer"):
            assert portable_dir_name(slug) == slug

    def test_no_windows_illegal_character_survives(self):
        nasty = 'ext:a*b?c"d<e>f|g'
        out = portable_dir_name(nasty)
        assert not (WINDOWS_ILLEGAL & set(out)), f"{out!r} still unusable on Windows"

    def test_double_dash_runs_are_collapsed(self):
        """`ext:skills-sh:owner--repo--skill` must not become a `----` mess."""
        out = portable_dir_name("ext:skills-sh:coreyhaines31--marketingskills--seo-audit")
        assert "--" not in out
        assert out == "ext-skills-sh-coreyhaines31-marketingskills-seo-audit"

    def test_never_returns_an_empty_or_dot_name(self):
        """A name of only separators must not yield '' or '.' — both unusable."""
        for pathological in (":::", "...", "   ", "*?<>|"):
            out = portable_dir_name(pathological)
            assert out and out not in (".", ".."), f"{pathological!r} -> {out!r}"

    def test_is_deterministic(self):
        s = "ext:hermes-hub:whatever"
        assert portable_dir_name(s) == portable_dir_name(s)

    def test_no_path_traversal_survives(self):
        """A slug can never escape the destination directory."""
        out = portable_dir_name("ext:../../etc/passwd")
        assert not out.startswith("."), out


class TestIndexPayloadContract:
    def test_name_is_still_the_wire_key(self):
        """dir_name is ADDITIVE. `name` must remain the slug the SKILL.md route
        resolves by, or every existing client's download breaks."""
        import inspect

        from app import bundle_wellknown_routes as m

        src = inspect.getsource(m.cookbook_wellknown_index)
        assert '"name": skill.slug' in src, "name must stay the raw slug (the wire key)"
        assert '"dir_name": portable_dir_name(skill.slug)' in src

    def test_install_script_uses_dir_name_with_a_fallback(self):
        """The shipped installer must mkdir dir_name, but still work against an
        older API build that does not send it."""
        from app.bundle_install_script_routes import _INSTALL_SCRIPT_TEMPLATE

        assert 'dir_name = s.get("dir_name") or name' in _INSTALL_SCRIPT_TEMPLATE
        assert "os.path.join(dest, dir_name)" in _INSTALL_SCRIPT_TEMPLATE
        # …and the FETCH must still use `name`, not dir_name.
        assert '{{base}}/{{name}}/SKILL.md' in _INSTALL_SCRIPT_TEMPLATE
