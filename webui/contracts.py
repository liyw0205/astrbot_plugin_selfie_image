"""Route aliases shared by the standalone and Dashboard Web adapters."""

from __future__ import annotations


# AstrBot has used both the historical bare plugin-page path and the explicit
# ``page/`` path. Both wrappers must register the same handler and semantics.
DASHBOARD_ROUTE_PREFIXES = ("", "page/")

# Legacy task-cancel paths intentionally remain aliases of one operation.
TASK_CANCEL_ROUTE_ALIASES = (
    "/api/tasks/{task_id}/cancel",
    "/api/test-image-channel/tasks/{task_id}/cancel",
    "/api/test-video-channel/tasks/{task_id}/cancel",
    "/api/studio/tasks/{task_id}/cancel",
)

DASHBOARD_TASK_CANCEL_ROUTES = (
    "tasks/<task_id>/cancel",
    "test-image-channel/tasks/<task_id>/cancel",
    "test-video-channel/tasks/<task_id>/cancel",
    "studio/tasks/<task_id>/cancel",
)
