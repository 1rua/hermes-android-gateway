"""Hermes plugin registration delegates to the optional session-only package."""
from oai_hermes_adapter import register

HERMES_PLUGIN = "open-android-intelligence-gateway"
HERMES_PLUGIN_MANIFEST = "plugin.yaml"
__all__ = ["register", "HERMES_PLUGIN", "HERMES_PLUGIN_MANIFEST"]
