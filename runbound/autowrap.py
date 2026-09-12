"""Class-level instrumentation of the provider SDKs themselves.

:func:`runbound.wrap` guards one client object. That only helps the code that
remembered to call it — and a client built four layers down inside a framework,
or in a module written before runbound was installed, flows past uncounted
while ``init()`` reports success. This module closes that hole: at ``init()``
time it replaces ``create`` on the SDK's own resource *classes*, so every
client in the process — built before or after, by anyone — is guarded.

What it patches lives with each provider's wrapper, in its ``CLASS_TARGETS``:
this module knows only how to resolve a target, install a guard on it, and take
it off again.

Three rules hold here:

* **Never guess where a module is.** Each target names a full dotted module
  path resolved with :func:`importlib.import_module`, because
  ``openai.resources.responses`` is not an attribute of ``openai.resources``
  until something imports it — attribute-walking would leave that surface
  silently unguarded.
* **Fail-open, per target.** A provider that will not import, a class that has
  moved, a ``create`` that refuses to be replaced: each costs that one surface
  and nothing else.
* **Idempotent and reversible.** A ``create`` already carrying the marker is
  left alone, whether an earlier ``patch_all`` or a ``wrap()`` put it there,
  and :func:`unpatch_all` restores exactly what was there before.

One documented limitation: a provider imported *after* ``init()`` is not
patched. That is what :func:`runbound.coverage` and the silent-zero warning
are for.
"""

import importlib
import logging
import threading
from typing import Any, Callable

from .wrappers import MARKER, ClassTarget, Hooks, Report, anthropic_wrapper, openai_wrapper

_LOG = logging.getLogger("runbound")

#: The method every target patches. One name, on purpose: a provider that grows
#: a second entry point gets a second :class:`ClassTarget`, not a special case.
CREATE = "create"

#: Every class runbound knows how to patch, in the order it reports them.
TARGETS: tuple[ClassTarget, ...] = openai_wrapper.CLASS_TARGETS + anthropic_wrapper.CLASS_TARGETS

#: label -> (class, the original ``create``, was it the class's own attribute).
#: The third field is what makes undoing exact: a ``create`` inherited from a
#: base class is *removed* again rather than left behind as a copy.
_PATCHED: "dict[str, tuple[type, Callable, bool]]" = {}

#: Guards :data:`_PATCHED` only. Never held while third-party code runs.
_LOCK = threading.Lock()


def patch_all(report: Report, hooks: Hooks) -> list[str]:
    """Patch every provider class that is importable, and say which are new.

    Returns the labels patched **by this call** — an empty list when every
    supported SDK is missing, and also when they are all patched already, which
    is what makes a second ``init()`` silent rather than noisy.

    Never raises: a target that cannot be resolved or patched is logged and
    skipped, and the rest are patched anyway.
    """
    installed = []
    for target in TARGETS:
        if _patch(target, report, hooks):
            installed.append(target.label)
    return installed


def patched() -> list[str]:
    """Every label patched right now, in target order."""
    with _LOCK:
        return [target.label for target in TARGETS if target.label in _PATCHED]


def unpatch_all() -> None:
    """Put every patched ``create`` back exactly as it was found.

    For tests and for the rare host that wants runbound's class patches gone
    without restarting. Clients wrapped by :func:`runbound.wrap` keep their
    own instance patches: this undoes what :func:`patch_all` did and nothing
    else. Never raises.
    """
    with _LOCK:
        entries = list(_PATCHED.items())
        _PATCHED.clear()
    for label, (cls, original, owned) in entries:
        try:
            if owned:
                setattr(cls, CREATE, original)
            else:
                delattr(cls, CREATE)
        except Exception:
            _LOG.warning("runbound could not un-patch %s; leaving it guarded", label,
                         exc_info=True)


def _patch(target: ClassTarget, report: Report, hooks: Hooks) -> bool:
    """Patch one target's ``create``; True if this call is what patched it.

    False covers every uninteresting answer — the SDK is not installed, the
    class has moved, the method is already guarded, the patch failed — because
    the caller only wants to know what is newly guarded, and the reasons are
    logged where they happen.
    """
    try:
        cls = _resolve(target)
        if cls is None:
            return False
        original = getattr(cls, CREATE, None)
        if not callable(original):
            _LOG.debug("runbound: %s has no %s to guard", target.label, CREATE)
            return False
        if getattr(original, MARKER, None) is True:
            return False
        owned = CREATE in vars(cls)
        installed = target.guard(original, report, hooks)
        setattr(cls, CREATE, installed)
    except Exception:
        _LOG.warning(
            "runbound could not auto-instrument %s; it will run unguarded",
            target.label,
            exc_info=True,
        )
        return False
    with _LOCK:
        _PATCHED[target.label] = (cls, original, owned)
    return True


def _resolve(target: ClassTarget) -> Any:
    """The class one target names, or ``None`` if it is not there to patch.

    ``sys.modules`` is not consulted separately: :func:`importlib.import_module`
    returns an already-imported module from it and only reaches the filesystem
    for one that is not — which is what makes a provider whose submodule has
    never been imported (``openai.resources.responses``) resolvable at all.
    A missing SDK is an ordinary, quiet answer; anything else is a surprise
    worth a warning.
    """
    try:
        module = importlib.import_module(target.module)
    except ImportError:
        _LOG.debug("runbound: %s is not installed; nothing to auto-instrument", target.module)
        return None
    except Exception:
        _LOG.warning(
            "runbound could not import %s; skipping it", target.module, exc_info=True
        )
        return None
    cls = getattr(module, target.attr, None)
    if cls is None:
        _LOG.debug("runbound: %s has no %s", target.module, target.attr)
    return cls
