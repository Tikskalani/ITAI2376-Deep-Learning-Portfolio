"""Atlas -- one-stop agent registration.

Both the worker (scripts/run_worker.py) and the web server
(core/web/api.py) need every agent's full registration -- the system
prompt under agents/<name>/system.md AND the tools_factory from
agents/<name>/tools.py. The dispatcher pre-registers bare names at
import time, but those have no tools and no resolved system prompt,
which is fine for routing but not for actually running an agent.

Call register_all() once at process startup. It is idempotent: calling
register_<name>() twice is harmless, the second call overwrites the
first with the same data.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def register_all() -> list[str]:
    """Register every agent. Returns the roster of names actually registered."""
    from agents.academic import register_academic
    from agents.build import register_build
    from agents.career import register_career
    from agents.ceo import register_ceo
    from agents.cfo import register_cfo
    from agents.client_delivery import register_client_delivery
    from agents.coo import register_coo
    from agents.cto import register_cto
    from agents.news_discovery import register_news_discovery
    from agents.opportunity_scout import register_opportunity_scout
    from agents.personal_ops import register_personal_ops
    from agents.property_ops import register_property_ops

    register_ceo()
    register_academic()
    register_build()
    register_career()
    register_cfo()
    register_client_delivery()
    register_coo()
    register_cto()
    register_news_discovery()
    register_opportunity_scout()
    register_personal_ops()
    register_property_ops()

    from core.runtime.dispatcher import registered_agents
    roster = registered_agents()
    log.info("registered %d agents: %s", len(roster), ", ".join(sorted(roster)))
    return roster


__all__ = ["register_all"]
