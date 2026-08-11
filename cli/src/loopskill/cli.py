"""loopskill CLI entry point.

Subcommand dispatch is deliberately manual (not one big argparse tree with
all handlers imported up front): `import` and `diff` import ONLY
loopskill.clients/scanner/lockfile/diff — modules with zero network calls,
statically verified in tests/test_offline_guard.py. `pull`/`apply` lazily
import loopskill.pull, which is the ONLY module in this package that touches
urllib. That import boundary IS the offline guarantee, not a promise in a
docstring.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loopskill import __version__


def _cmd_import(args: argparse.Namespace) -> int:
    from loopskill.lockfile import build_lockfile, dumps
    from loopskill.scanner import scan_all

    home = Path(args.home).expanduser() if args.home else None
    scans = scan_all(home)
    lockfile = build_lockfile(scans)
    text = dumps(lockfile)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        total = sum(c["skill_count"] for c in lockfile["clients"].values())
        present = sum(1 for c in lockfile["clients"].values() if c["installed"])
        print(
            f"loopskill import: wrote {args.output} ({total} skill(s) across {present} client(s))",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


def _load_lockfile_arg(path_str: str, home: Path | None) -> tuple[dict, str]:
    """Resolve a diff positional arg: an existing file path, or '-' for a live scan."""
    from loopskill.lockfile import build_lockfile, load_path
    from loopskill.scanner import scan_all

    if path_str == "-":
        return build_lockfile(scan_all(home)), "<live scan>"
    p = Path(path_str).expanduser()
    if not p.is_file():
        raise SystemExit(f"loopskill diff: no such lockfile: {p}")
    return load_path(p), str(p)


def _cmd_diff(args: argparse.Namespace) -> int:
    from loopskill.diff import diff_lockfiles, format_diff_report

    home = Path(args.home).expanduser() if args.home else None
    lock_a, label_a = _load_lockfile_arg(args.lockfile_a, home)
    if args.lockfile_b:
        lock_b, label_b = _load_lockfile_arg(args.lockfile_b, home)
    else:
        # One-argument form: compare the given lockfile against a live scan
        # of THIS machine — the 30-second single-command demo.
        from loopskill.lockfile import build_lockfile
        from loopskill.scanner import scan_all

        lock_b, label_b = build_lockfile(scan_all(home)), "<this machine>"

    diffs = diff_lockfiles(lock_a, lock_b)
    print(format_diff_report(diffs, label_a=label_a, label_b=label_b))
    return 1 if any(d.has_drift for d in diffs) else 0


def _cmd_pull(args: argparse.Namespace) -> int:
    from loopskill.pull import DEFAULT_API_BASE, pull_bundle

    api_base = args.api_base or DEFAULT_API_BASE
    try:
        skills = pull_bundle(args.slug, api_base=api_base)
    except RuntimeError as exc:
        print(f"loopskill pull: {exc}", file=sys.stderr)
        return 1

    if args.output:
        import json

        payload = [
            {"name": s.name, "locked": s.locked, "content": s.content.decode("utf-8", errors="replace")}
            for s in skills
        ]
        Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"loopskill pull: wrote {len(skills)} skill(s) to {args.output}", file=sys.stderr)
    else:
        locked = sum(1 for s in skills if s.locked)
        print(f"loopskill pull: fetched {len(skills) - locked} skill(s), {locked} locked (paid tier)")
        for s in skills:
            tag = "locked" if s.locked else f"{len(s.content)} bytes"
            print(f"  {s.name}: {tag}")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    from loopskill.apply import execute_apply, format_plan, plan_apply
    from loopskill.pull import DEFAULT_API_BASE, pull_bundle

    api_base = args.api_base or DEFAULT_API_BASE
    try:
        skills = pull_bundle(args.slug, api_base=api_base)
    except RuntimeError as exc:
        print(f"loopskill apply: {exc}", file=sys.stderr)
        return 1

    dest = Path(args.dest).expanduser() if args.dest else Path.home() / ".claude" / "skills"
    actions = plan_apply(skills, dest)
    print(format_plan(actions, dry_run=not args.write))

    if args.write:
        execute_apply(skills, dest, actions)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser."""
    parser = argparse.ArgumentParser(
        prog="loopskill",
        description=(
            "Skill-portability CLI. Discover, diff, pull, and apply AI-agent "
            "skills across clients (Claude, Hermes, Codex, Cursor). "
            "`import`/`diff` never touch the network."
        ),
    )
    parser.add_argument("--version", action="version", version=f"loopskill {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="discover skills already installed across clients (offline)")
    p_import.add_argument("-o", "--output", help="write the lockfile here instead of stdout")
    p_import.add_argument("--home", help="override $HOME for client-path resolution (testing)")
    p_import.set_defaults(func=_cmd_import)

    p_diff = sub.add_parser(
        "diff",
        help="show drift between two lockfiles, or one lockfile and this machine (offline)",
    )
    p_diff.add_argument("lockfile_a", help="path to a lockfile, or '-' for a live scan of this machine")
    p_diff.add_argument(
        "lockfile_b",
        nargs="?",
        default=None,
        help="second lockfile path; omit to diff lockfile_a against a live scan of this machine",
    )
    p_diff.add_argument("--home", help="override $HOME for client-path resolution (testing)")
    p_diff.set_defaults(func=_cmd_diff)

    p_pull = sub.add_parser("pull", help="fetch a public bundle's skills (network; LoopSkill or --api-base)")
    p_pull.add_argument("slug", help="bundle slug, e.g. loopskill-essentials")
    p_pull.add_argument("--api-base", help="registry API origin (default: app.loopskill.io)")
    p_pull.add_argument("-o", "--output", help="write fetched skills as JSON here instead of stdout summary")
    p_pull.set_defaults(func=_cmd_pull)

    p_apply = sub.add_parser(
        "apply",
        help="converge a local client dir to a pulled bundle (dry-run by default; local disk only)",
    )
    p_apply.add_argument("slug", help="bundle slug, e.g. loopskill-essentials")
    p_apply.add_argument("--api-base", help="registry API origin (default: app.loopskill.io)")
    p_apply.add_argument("--dest", help="target skills directory (default: ~/.claude/skills)")
    p_apply.add_argument("--write", action="store_true", help="actually write files (default: dry-run)")
    p_apply.set_defaults(func=_cmd_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (also the `loopskill` console-script target)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
