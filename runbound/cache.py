"""The on-disk policy cache: what a worker enforces when the plane is not there (S7).

The last policy the plane served is written to disk so a restarted worker enforces it **before
its first heartbeat**, and so a plane that dies does not turn every guard off. Best effort
throughout: nothing here ever raises into a tool call.

**Where.** ``init(cache_dir=)`` / ``RUNBOUND_CACHE_DIR``, else the platform cache directory
(``XDG_CACHE_HOME`` or ``~/.cache``; ``~/Library/Caches`` on macOS; ``%LOCALAPPDATA%`` on Windows),
else ``tempfile.gettempdir()``, else memory only (said once). **Never** ``XDG_RUNTIME_DIR``. The
file is ``<dir>/runbound/<sha256(plane_url)[:12]>/<service>.json``, directory ``0700``, file ``0600``.

**How it is written.** ``mkstemp`` in the same directory, write, flush, ``fsync``, ``os.replace``,
``fsync`` of the directory (POSIX), the temp file removed on error. No locks: a reader sees the
old file or the new one, never half of either. A version guard never writes an older
``policy_version`` over a newer one (replicas of a service share the file).

**What is in it.** ``{v, service, policy_version, policy, policy_dry_run, rows_hash, rows,
served_at, written_at, key_id, mac}``. Never the token, an argument or a result. ``key_id`` is
``sha256(token)[:16]``; ``mac`` is ``HMAC-SHA256(HMAC(token, "runbound-cache-v1"), canonical body)``.

**Honest limit.** The MAC is tamper-evidence: a hand-edited, truncated or other-token file is
ignored (as if absent) and said so once. It is not a boundary against anyone who can read the
token, because they can compute the MAC. Rules declared in code never come from the cache.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("runbound")

VERSION = 1

#: Consequential classes trust a cache only this fresh (and only while the link is not yet
#: ``plane_unavailable``); the others enforce a cache of any age.
FRESH_FOR_CONSEQUENTIAL_S = 15 * 60

#: ``plane_status`` reports ``stale`` once the cache this worker runs on is older than this.
STALE_AFTER_S = 24 * 60 * 60

#: A cache that has not changed is rewritten at most this often (it is what ``written_at`` freshness is).
REFRESH_EVERY_S = 60.0

_WARNED: set = set()
_LOCK = threading.Lock()


def _warn_once(key: tuple, message: str, *args: Any) -> None:
    with _LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    _LOG.warning(message, *args)


def reset_for_tests() -> None:
    with _LOCK:
        _WARNED.clear()


def key_id(token: str | None) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


def _mac_key(token: str | None) -> bytes:
    return hmac.new((token or "").encode("utf-8"), b"runbound-cache-v1", hashlib.sha256).digest()


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(body: dict, token: str | None) -> dict:
    """``body`` plus its ``key_id`` and ``mac``."""
    body = {**body, "key_id": key_id(token)}
    return {**body, "mac": hmac.new(_mac_key(token), _canonical(body), hashlib.sha256).hexdigest()}


def unseal(data: Any, token: str | None, service: str) -> dict | None:
    """The cache body if ``data`` is a cache this token wrote for this service, else ``None``."""
    if not isinstance(data, dict) or data.get("v") != VERSION or data.get("service") != service:
        return None
    mac = data.get("mac")
    if not isinstance(mac, str) or data.get("key_id") != key_id(token):
        return None
    body = {k: v for k, v in data.items() if k != "mac"}
    expected = hmac.new(_mac_key(token), _canonical(body), hashlib.sha256).hexdigest()
    return body if hmac.compare_digest(expected, mac) else None


# --- where ---------------------------------------------------------------------------------------


def _platform_cache_dir() -> Path | None:
    try:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA")
            return Path(base) if base else None
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Caches"
        base = os.environ.get("XDG_CACHE_HOME")
        return Path(base) if base else Path.home() / ".cache"
    except Exception:
        return None


def _usable(directory: Path) -> bool:
    """Can a private directory be made here, and written to?"""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        handle, name = tempfile.mkstemp(dir=directory, prefix=".probe-")
        os.close(handle)
        os.unlink(name)
        return True
    except Exception:
        return False


def resolve(cache_dir: str | None, plane_url: str | None, service: str) -> Path | None:
    """The cache file for this plane and service, or ``None`` (memory only; said once)."""
    digest = hashlib.sha256((plane_url or "").encode("utf-8")).hexdigest()[:12]
    explicit = cache_dir or os.environ.get("RUNBOUND_CACHE_DIR")
    candidates = [Path(explicit)] if explicit else []
    candidates += [d for d in (_platform_cache_dir(), Path(tempfile.gettempdir())) if d is not None]
    safe_service = "".join(c if c.isalnum() or c in "-_." else "_" for c in service) or "default"
    for base in candidates:
        directory = base / "runbound" / digest
        if _usable(directory):
            return directory / f"{safe_service}.json"
    _warn_once(("no-dir",), "runbound: no writable cache directory; the policy cache is memory only")
    return None


# --- read and write ---------------------------------------------------------------------------------


def read(path: Path | None, token: str | None, service: str) -> "tuple[dict | None, str]":
    """``(body, status)``; status is ``"ok"``, ``"absent"`` or ``"rejected"`` (edited, truncated, foreign)."""
    if path is None:
        return None, "absent"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "absent"
    except Exception:
        return None, "absent"
    try:
        body = unseal(json.loads(raw.decode("utf-8")), token, service)
    except Exception:
        body = None
    if body is None:
        _warn_once(("rejected", str(path)), "runbound: ignoring the policy cache at %s (edited, truncated or another key's)", path)
        return None, "rejected"
    return body, "ok"


def write(path: Path | None, token: str | None, body: dict) -> bool:
    """Atomically replace the cache with ``body`` (sealed). Best effort: ``False``, never an exception."""
    if path is None:
        return False
    temp = None
    try:
        current, _ = read(path, token, body.get("service", ""))
        if current is not None and int(current.get("policy_version", 0)) > int(body.get("policy_version", 0)):
            return False  # never an older version over a newer one
        data = json.dumps(seal(body, token), sort_keys=True).encode("utf-8")
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".cache-", suffix=".tmp")
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        temp = None
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return True
    except Exception:
        _warn_once(("write", str(path)), "runbound: could not write the policy cache at %s", path)
        return False
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


def body_for(service: str, policy_version: int, policy: dict | None, dry_run: bool,
             rows_hash: str | None = None, rows: Any = None, served_at: float | None = None,
             halt: str | None = None, halted_tools: dict | None = None, service_defaults: dict | None = None) -> dict:
    """The cache body for what the plane just served (``rows`` arrive with T1a).

    ``halt`` is the mode of a fleet halt the plane has served and not lifted (``"stop"`` or ``"narrow"``), else ``None``: a halt holds
    until the plane lifts it, so a worker restarted while the plane is down must still start halted.
    """
    now = time.time()
    return {
        "v": VERSION, "service": service, "policy_version": policy_version, "policy": policy,
        "policy_dry_run": bool(dry_run), "rows_hash": rows_hash, "rows": rows, "halt": halt,
        "halted_tools": halted_tools or {}, "service_defaults": service_defaults,
        "served_at": served_at if served_at is not None else now, "written_at": now,
    }
