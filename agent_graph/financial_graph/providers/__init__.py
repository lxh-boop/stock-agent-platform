"""Domain-scoped provider adapters behind the public graph provider facade.

These modules translate GraphRefs to existing internal services and normalize
their results. Public Agents continue to depend on ``GraphProviderAdapter``
rather than importing these implementation modules directly.
"""

from .common import ProviderIdentityResolver, records_from_payload, sources_from_payload
from .evidence import EvidenceGraphProvider
from .portfolio import PortfolioGraphProvider
from .risk import PortfolioRiskProvider

__all__ = [
    "EvidenceGraphProvider",
    "PortfolioGraphProvider",
    "PortfolioRiskProvider",
    "ProviderIdentityResolver",
    "records_from_payload",
    "sources_from_payload",
]
