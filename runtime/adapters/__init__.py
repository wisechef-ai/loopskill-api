"""Per-OS package/binary adapters (Phase F.2).

Each adapter exposes:
    resolve(binary_spec) -> AdapterPlan
    install(plan)        -> InstallResult
    uninstall(name)      -> bool

All install state is kept under ``~/.recipes/runtime/<skill-slug>/`` so
uninstalls are clean even when underlying package managers leak files.
"""

from .base import AdapterPlan, InstallResult, runtime_root

__all__ = ["AdapterPlan", "InstallResult", "runtime_root"]
