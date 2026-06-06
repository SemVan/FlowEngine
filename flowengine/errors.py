"""Typed exception hierarchy.

Keep the tree shallow. Code paths catch *categories* of failure, not specific
codes — the message carries the detail.
"""


class FlowEngineError(Exception):
    """Base class for all flowengine-raised errors."""


class TransportError(FlowEngineError):
    """Anything wrong at the serial/wire layer."""


class TransportClosed(TransportError):
    """The transport is not currently connected."""


class TransportTimeout(TransportError):
    """A command did not produce an `ok` within its deadline."""


class TransportProtocolError(TransportError):
    """The controller sent something we couldn't reconcile (bad checksum, kill `!!`, etc)."""


class ControllerError(FlowEngineError):
    """Marlin returned an `Error:` we can't auto-recover from."""


class StateError(FlowEngineError):
    """Operation attempted from an illegal state-machine state."""


class SoftLimitError(FlowEngineError):
    """A motion request would exceed the configured soft limits."""


class ConfigError(FlowEngineError):
    """The on-disk config didn't parse or didn't validate."""


class ProcedureError(FlowEngineError):
    """A procedure failed to load, lint, or execute."""
