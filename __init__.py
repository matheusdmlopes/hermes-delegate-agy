"""Hermes plugin that delegates work to Antigravity CLI workers."""

from __future__ import annotations

import os


def register(ctx) -> None:
    """Register the delegate_agy tool and its supervised process manager."""
    if __package__:
        from .tools import register_tools
    else:
        plugin_dir = os.path.dirname(__file__)
        if plugin_dir not in os.sys.path:
            os.sys.path.insert(0, plugin_dir)
        from tools import register_tools

    register_tools(ctx)
