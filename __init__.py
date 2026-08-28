"""Hermes plugin that delegates work to Antigravity CLI workers."""

from __future__ import annotations

import os


def register(ctx) -> None:
    """Register the delegate_agy tool and its supervised process manager."""
    try:
        # When loaded as a proper package (plugin system)
        from .tools import register_tools
    except ImportError:
        # Fallback for when the module is loaded as a script (e.g., by pytest)
        # Add the plugin directory to sys.path so `tools` can be imported
        plugin_dir = os.path.dirname(__file__)
        if plugin_dir not in os.sys.path:
            os.sys.path.insert(0, plugin_dir)
        from tools import register_tools

    register_tools(ctx)