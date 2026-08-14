"""Platform discovery and readiness checks."""

from nova_rtl.platform.doctor import DEFAULT_REQUIRED_TOOLS, run_doctor

__all__ = ["DEFAULT_REQUIRED_TOOLS", "run_doctor"]
