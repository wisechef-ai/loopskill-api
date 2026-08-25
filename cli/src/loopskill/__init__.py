"""loopskill — a skill-portability CLI.

Not a client for the LoopSkill registry. This package works on skills a user
ALREADY has, installed anywhere on disk by any client (Claude, Hermes/Cursor,
Codex, ...), and treats LoopSkill (app.loopskill.io) as one OPTIONAL backend
among many for the `pull` command. `import` and `diff` never touch the
network — see loopskill.offline for the structural guarantee.
"""

from __future__ import annotations

__version__ = "0.3.0"
