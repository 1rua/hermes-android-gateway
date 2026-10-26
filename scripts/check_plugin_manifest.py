"""Check this repository's session-only registration contract."""
from pathlib import Path
import tomllib
import yaml

root = Path(__file__).resolve().parents[1]
metadata = tomllib.loads((root / "pyproject.toml").read_text())
manifest = yaml.safe_load((root / "plugin.yaml").read_text())
assert manifest["version"] == metadata["project"]["version"]
assert manifest["provides_tools"] == manifest["provides_hooks"] == []
assert set(manifest["requires_env"]) == {"OAI_HERMES_SESSION_TOKEN", "OAI_HERMES_GATEWAY_ROOT", "OAI_HERMES_ACCOUNTS"}
assert 'from oai_hermes_adapter import register' in (root / "__init__.py").read_text()
print("session-only plugin manifest passed")
