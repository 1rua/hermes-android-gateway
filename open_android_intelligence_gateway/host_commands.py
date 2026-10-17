"""Discover commands from the installed Hermes runtime on every request."""
from __future__ import annotations

from contextvars import copy_context
from importlib import import_module
import re
from types import ModuleType
from typing import Any


def _host_module(name: str) -> ModuleType | None:
    """An absent SDK is optional; a broken installed SDK must remain visible."""
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        if error.name and (name == error.name or name.startswith(error.name + ".")):
            return None
        raise


def command_catalog() -> list[dict[str, Any]]:
    # The registry owns gateway-vs-CLI availability and config gates. A missing
    # runtime has no commands to advertise; an Android-specific list would lie
    # about both unsupported commands and commands added by host plugins.
    host = _host_module("hermes_cli.commands")
    if host is None:
        return []
    entries: dict[str, dict[str, Any]] = {}

    def add(name: str, description: str, hint: str = "", *, accepts_arguments: bool = False) -> None:
        name = name.strip().removeprefix("/").lower()
        if not re.fullmatch(r"[a-z0-9_-]+", name) or name in entries:
            return
        entries[name] = {
            "id": name, "invocation": "/" + name,
            "title": description or name, "description": description or name,
            "acceptsArguments": bool(hint) or accepts_arguments,
            "availability": "available",
        }
        if hint:
            entries[name]["argumentHint"] = hint

    gates = getattr(host, "_resolve_config_gates", lambda: set())()
    available = getattr(host, "_is_gateway_available", None)
    commands = [cmd for cmd in host.COMMAND_REGISTRY
                if (available(cmd, gates) if callable(available) else not getattr(cmd, "cli_only", False))]
    for cmd in commands:
        describe = getattr(cmd, "describe", None)
        add(cmd.name, describe() if callable(describe) else cmd.description,
            getattr(cmd, "args_hint", ""))

    # Canonical names win collisions before aliases or dynamically registered
    # commands, just as the host's built-in command dispatcher does.
    for cmd in commands:
        describe = getattr(cmd, "describe", None)
        for alias in getattr(cmd, "aliases", ()):
            add(alias, describe() if callable(describe) else cmd.description,
                getattr(cmd, "args_hint", ""))

    plugins = _host_module("hermes_cli.plugins")
    if plugins is not None:
        # The host's convenience iterator suppresses all discovery exceptions.
        # Calling the registry directly keeps a broken plugin visible to callers.
        # Both plugin handlers and skill invocations accept trailing user text
        # even when their metadata does not declare a placeholder.
        for name, info in sorted((plugins.get_plugin_commands() or {}).items()):
            add(name, info.get("description") or name, info.get("args_hint") or "", accepts_arguments=True)

    for name, info in _skill_entries(host):
        add(name, info.get("description") or name, info.get("args_hint") or "", accepts_arguments=True)
    return list(entries.values())


def _skill_entries(host: ModuleType) -> list[tuple[str, dict[str, Any]]]:
    from .platform_identity import GATEWAY_PLATFORM_ID

    platforms = _host_module("hermes_cli.commands_platforms")
    iterator = getattr(platforms or host, "_iter_gateway_skills", None)
    context = _host_module("gateway.session_context")
    if context is None:
        return []

    def collect() -> list[tuple[str, dict[str, Any]]]:
        # Skill caches are platform scoped. Keep the HTTP request from inheriting
        # another adapter's disabled-skill view, without changing process env.
        tokens = context.set_session_vars(platform=GATEWAY_PLATFORM_ID)
        try:
            if callable(iterator):
                return [(name, info) for name, info, _relative in iterator(GATEWAY_PLATFORM_ID)]
            # Older supported SDKs expose only the skill registry and a menu
            # collector that truncates names to Telegram's limit. Keep the
            # registry's executable names intact on Android.
            skills = _host_module("agent.skill_commands")
            utils = _host_module("agent.skill_utils")
            if skills is None or utils is None:
                return []
            disabled = utils.get_disabled_skill_names(platform=GATEWAY_PLATFORM_ID)
            return [(name, info) for name, info in sorted(skills.get_skill_commands().items())
                    if info.get("name") not in disabled]
        finally:
            context.clear_session_vars(tokens)

    return copy_context().run(collect)
