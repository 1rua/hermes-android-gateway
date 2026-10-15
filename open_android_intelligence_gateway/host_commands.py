"""Discover commands from the installed Hermes runtime on every request."""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def command_catalog() -> list[dict[str, Any]]:
    # The registry owns gateway-vs-CLI availability and config gates. A missing
    # runtime has no commands to advertise; an Android-specific list would lie
    # about both unsupported commands and commands added by host plugins.
    try:
        from hermes_cli import commands as host
    except ImportError:
        return []
    entries: dict[str, dict[str, Any]] = {}

    def add(name: str, description: str, hint: str = "") -> None:
        name = name.removeprefix("/")
        if not re.fullmatch(r"[a-z0-9_-]+", name) or name in entries:
            return
        entries[name] = {
            "id": name, "invocation": "/" + name,
            "title": description or name, "description": description or name,
            "acceptsArguments": bool(hint),
            "availability": "available",
        }
        if hint:
            entries[name]["argumentHint"] = hint

    gates = getattr(host, "_resolve_config_gates", lambda: set())()
    available = getattr(host, "_is_gateway_available", None)
    for cmd in host.COMMAND_REGISTRY:
        if callable(available):
            if not available(cmd, gates):
                continue
        elif getattr(cmd, "cli_only", False):
            continue
        describe = getattr(cmd, "describe", None)
        add(cmd.name, describe() if callable(describe) else cmd.description,
            getattr(cmd, "args_hint", ""))

    plugins = getattr(host, "_iter_plugin_command_entries", None)
    if callable(plugins):
        for name, description, hint in plugins():
            add(name, description, hint)
    else:
        try:
            from hermes_cli.plugins import get_plugin_commands
            for name, info in (get_plugin_commands() or {}).items():
                add(name, info.get("description", name), info.get("args_hint", ""))
        except ImportError:
            pass

    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import get_disabled_skill_names
        from .platform_identity import GATEWAY_PLATFORM_ID
        disabled = get_disabled_skill_names(platform=GATEWAY_PLATFORM_ID)
        for name, info in get_skill_commands().items():
            if info.get("name") in disabled:
                continue
            add(name, info.get("description", name), info.get("args_hint", ""))
    except Exception:
        logger.debug("Host skill command discovery unavailable", exc_info=True)
    return list(entries.values())
