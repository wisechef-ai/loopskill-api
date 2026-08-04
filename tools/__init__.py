"""CLI helpers packaged with the API.

Exists so ``tests/test_recipes_cli.py``'s ``from tools.recipes_cli import ...``
resolves to THIS directory. Every other package dir in the repo (``app/``,
``scripts/``, ``tests/``) already carries an ``__init__.py``; ``tools/`` was
the sole exception, which made it a PEP 420 *namespace* portion rather than a
regular package.

That distinction is the whole bug. Python resolves a *regular* package found
anywhere on ``sys.path`` ahead of a *namespace* portion found earlier — a
namespace package is only assembled when no regular package of that name
exists. On CI some installed dependency ships its own top-level ``tools``
package, so it won outright and ``tools.recipes_cli`` disappeared:

    ModuleNotFoundError: No module named 'tools.recipes_cli'

It never reproduced locally because this venv happens to have no dependency
shipping a ``tools`` package, so the namespace portion was unopposed. Proven
by putting a stand-in regular ``tools`` package second on ``sys.path``: the
import fails without this file and succeeds with it (repo dir is first, and
regular-beats-regular is resolved by path order).

Found via mesh_0408 T0-A. Pre-existing on ``main`` — run 30942954818, commit
4763170, 2026-08-04 19:26 UTC, identical error, before the T0-A branch
existed — and fixed here because it blocks every PR's merge gate.
"""
