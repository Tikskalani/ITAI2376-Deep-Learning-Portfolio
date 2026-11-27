"""Atlas -- connectors.

The uniform interface to external systems. The connector base gives every
integration caching, staleness signalling, retry, and a clean failure
mode. The statement CSV connector is the first concrete one, built for the
CFO's manual statement import; more arrive through Phase 1 and Phase 2.
"""

from core.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorResult,
    TransientConnectorError,
)
from core.connectors.statement import (
    StatementColumns,
    StatementConnector,
    StatementTransaction,
)

__all__ = [
    "Connector",
    "ConnectorResult",
    "ConnectorError",
    "TransientConnectorError",
    "StatementConnector",
    "StatementColumns",
    "StatementTransaction",
]
