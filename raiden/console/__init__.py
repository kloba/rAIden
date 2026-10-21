"""rAIden Console: the web control plane for a fleet of PostgreSQL servers.

Monitoring (any PostgreSQL: on-prem, VMs, Kubernetes, AWS RDS, Cloud SQL, Azure), an incident board whose stages run on
autopilot or wait for a person, scheduled routines, ad-hoc investigations in chat, and the AI endpoints that pay for it all.
Run it with `raiden console` (or `raiden console --demo` for a simulated fleet).
"""

from .. import __version__  # one version for the host agent and the console

__all__ = ["__version__"]
