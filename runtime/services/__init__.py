"""Service provisioners (Phase F.3).

Contract for each backend:
    provision(service_spec, *, skill_slug) -> ServiceHandle
    health(handle)                         -> HealthStatus
    teardown(handle)                       -> bool
"""

from .base import HealthStatus, ServiceHandle

__all__ = ["HealthStatus", "ServiceHandle"]
