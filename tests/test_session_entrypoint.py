"""The standalone repository is a thin optional host entrypoint."""
import ast
import importlib.util
import inspect
from pathlib import Path
import tomllib

import pytest
import yaml

from oai_core.gateway.core import GatewayCore, DeviceRequestStore
from oai_hermes_adapter import register

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_depends_on_core_and_optional_adapter_only():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = metadata["project"]["dependencies"]
    assert dependencies == ["open-android-intelligence-core>=0.2,<0.3", "oai-hermes-session-adapter>=0.2,<0.3"]
    assert not any("git+" in value for value in dependencies)


def test_old_device_classes_are_the_core_classes():
    from open_android_intelligence_gateway.core import GatewayCore as LegacyCore, DeviceRequestStore as LegacyStore
    assert LegacyCore is GatewayCore
    assert LegacyStore is DeviceRequestStore


def test_no_device_runtime_is_implemented_in_the_compatibility_package():
    for source in (ROOT / "open_android_intelligence_gateway").glob("*.py"):
        tree = ast.parse(source.read_text())
        assert not any(isinstance(node, ast.ClassDef) for node in ast.walk(tree)), source
        assert "web.TCPSite" not in source.read_text()


def test_standard_loader_registers_the_optional_adapter_only():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["provides_tools"] == manifest["provides_hooks"] == []
    spec = importlib.util.spec_from_file_location("oai_hermes_plugin", ROOT / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    assert plugin.register is register


def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("OAI_HERMES_SESSION_TOKEN", raising=False)
    # The platform loader runs this inside Hermes; no synthetic SDK fallback is
    # part of the distributed entrypoint.
    pytest.importorskip("gateway.platforms.base")
    with pytest.raises(ValueError, match="TOKEN"):
        register(object())
