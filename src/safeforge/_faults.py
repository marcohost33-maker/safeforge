"""Named fault points ("failpoints") for crash and race testing.

Production code calls :func:`point` at every step boundary of a multi-step
protocol. By default this is a no-op. Tests install a hook that can

* terminate the process at a named point (``os._exit``) to simulate a crash, or
* run a concurrent actor (e.g. the reconciler) at that exact point to make a
  race deterministic.

There is deliberately NO environment-variable switch: a failpoint can only be
armed by code that already runs inside the process, so a deployed service
cannot be crashed through configuration.
"""

from __future__ import annotations

from collections.abc import Callable

_hook: Callable[[str], None] | None = None


def set_hook(hook: Callable[[str], None] | None) -> None:
    """Install (or with ``None`` remove) the failpoint hook."""
    global _hook
    _hook = hook


def point(name: str) -> None:
    """Mark a step boundary. No-op unless a hook is installed."""
    if _hook is not None:
        _hook(name)
