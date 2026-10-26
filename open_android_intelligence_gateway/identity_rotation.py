"""Legacy import compatibility; implementation is owned by the indicated package."""
from importlib import import_module

_TARGET = 'oai_core.gateway.identity_rotation'


def __getattr__(name):
    return getattr(import_module(_TARGET), name)


def __dir__():
    return dir(import_module(_TARGET))
