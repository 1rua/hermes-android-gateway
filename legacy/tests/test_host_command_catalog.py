"""Catalog contracts against the installed Hermes registry and skill scanner."""
from __future__ import annotations

import importlib.util

import pytest

from open_android_intelligence_gateway import host_commands
from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_ID


@pytest.fixture
def native_commands(tmp_path, monkeypatch):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Requires the real Hermes SDK")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(tmp_path))
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    from hermes_cli import commands, plugins
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: manager)
    return commands, plugins, manager


def test_native_registry_aliases_plugins_and_catalog_versions(native_commands, tmp_path):
    from open_android_intelligence_gateway.core import create_gateway_core
    from test_support import make_secret_store

    host, plugins, manager = native_commands
    core = create_gateway_core(tmp_path, secret_store=make_secret_store())
    before = core.command_catalog_response("en")
    entries = {entry["invocation"]: entry for entry in before["commands"]}
    gates = host._resolve_config_gates()
    for command in host.COMMAND_REGISTRY:
        for name in (command.name, *command.aliases):
            assert ("/" + name in entries) == host._is_gateway_available(command, gates)
    assert entries["/reset"]["argumentHint"] == entries["/new"]["argumentHint"]

    context = plugins.PluginContext(plugins.PluginManifest(name="catalog-test"), manager)
    context.register_command("/NEED-FILE", lambda text: text, "Read a file", args_hint="<file>")
    context.register_command("PLAIN", lambda text: text, "Accept free text")
    after = core.command_catalog_response("en")
    entries = {entry["invocation"]: entry for entry in after["commands"]}
    assert entries["/need-file"]["argumentHint"] == "<file>"
    assert entries["/need-file"]["acceptsArguments"] is True
    assert entries["/plain"]["acceptsArguments"] is True
    assert plugins.get_plugin_command_handler(entries["/need-file"]["id"])("report.txt") == "report.txt"
    assert after["catalogVersion"] != before["catalogVersion"]
    assert after == core.command_catalog_response("en")
    manager._plugin_commands.clear()
    assert core.command_catalog_response("en") == before


def test_native_skill_scan_uses_android_platform_and_preserves_callers_context(native_commands, tmp_path, monkeypatch):
    from agent import skill_commands
    from gateway import session_context
    from tools import skills_tool

    root = tmp_path / "skills"
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", root)
    long_name = "skill-command-name-longer-than-telegram-thirty-two-character-limit"
    for name in ("visible-test", "android-hidden-test", "telegram-hidden-test", long_name):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Run {name}\n---\nInstructions\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "skills:\n  platform_disabled:\n"
        f"    {GATEWAY_PLATFORM_ID}: [android-hidden-test]\n"
        "    telegram: [telegram-hidden-test]\n", encoding="utf-8")
    tokens = session_context.set_session_vars(platform="telegram", chat_id="other-adapter")
    try:
        # Populate the other adapter's native cache before requesting Android.
        assert "/telegram-hidden-test" not in skill_commands.scan_skill_commands()
        entries = {entry["invocation"]: entry for entry in host_commands.command_catalog()}
        assert "/visible-test" in entries
        assert "/telegram-hidden-test" in entries
        assert "/android-hidden-test" not in entries
        assert "/" + long_name in entries
        assert "/" + long_name[:32] not in entries
        assert entries["/visible-test"]["acceptsArguments"] is True
        assert session_context.get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
        assert session_context.get_session_env("HERMES_SESSION_CHAT_ID") == "other-adapter"
    finally:
        session_context.clear_session_vars(tokens)


def test_discovery_does_not_hide_broken_installed_runtime(monkeypatch):
    def missing_sdk(name):
        raise ModuleNotFoundError(name="hermes_cli")

    monkeypatch.setattr(host_commands, "import_module", missing_sdk)
    assert host_commands.command_catalog() == []

    def broken_sdk(name):
        raise ModuleNotFoundError("Missing SDK dependency", name="broken_dependency")

    monkeypatch.setattr(host_commands, "import_module", broken_sdk)
    with pytest.raises(ModuleNotFoundError, match="Missing SDK dependency"):
        host_commands.command_catalog()


def test_plugin_and_skill_runtime_errors_are_visible(native_commands, monkeypatch):
    _host, plugins, _manager = native_commands

    def fail():
        raise RuntimeError("host discovery failed")

    monkeypatch.setattr(plugins, "get_plugin_commands", fail)
    with pytest.raises(RuntimeError, match="host discovery failed"):
        host_commands.command_catalog()
    monkeypatch.setattr(plugins, "get_plugin_commands", lambda: {})
    from agent import skill_commands
    monkeypatch.setattr(skill_commands, "get_skill_commands", fail)
    with pytest.raises(RuntimeError, match="host discovery failed"):
        host_commands.command_catalog()
