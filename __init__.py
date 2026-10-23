"""Quota Gauge: a Hermes desktop pane for subscription quotas and token history.

The agent half registers nothing. It exists so Hermes treats the folder as a regular plugin;
the work happens in ``dashboard/plugin_api.py`` (backend routes) and ``desktop/plugin.js`` (pane).
"""


def register(ctx) -> None:  # noqa: ARG001 - no tools, hooks or commands
    return None
