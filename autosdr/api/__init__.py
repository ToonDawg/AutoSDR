"""REST API routers.

Every route lives under ``/api/*``. The routers are mounted by
:func:`autosdr.webhook.create_app`.

Each router is a self-contained unit — keeping them split makes it easy to
evolve a single resource without rebuilding the whole surface.
"""

from autosdr.api.campaigns import router as campaigns_router
from autosdr.api.leads import router as leads_router
from autosdr.api.llm_calls import router as llm_calls_router
from autosdr.api.setup import router as setup_router
from autosdr.api.stats import router as stats_router
from autosdr.api.status import router as status_router
from autosdr.api.threads import router as threads_router
from autosdr.api.webhooks import router as webhooks_router
from autosdr.api.workspace import router as workspace_router

ALL_ROUTERS = [
    setup_router,
    workspace_router,
    status_router,
    campaigns_router,
    leads_router,
    threads_router,
    llm_calls_router,
    stats_router,
    webhooks_router,
]

__all__ = ["ALL_ROUTERS"]
