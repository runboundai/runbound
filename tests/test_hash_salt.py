"""Tests for Wave 24, T59d: salted argument hashes.

``_args_hash`` mixes a per-process random salt into every digest, generated
once at import (``secrets.token_bytes(16)``) and kept across :func:`reset`.
The digest stays an equality token good for spotting a repeat *inside this
process* — the loop detector's whole job — and stops being a stable
fingerprint of the arguments across processes: two processes salt
differently, and nobody who only sees a digest can compute or recognize it
from the raw arguments.

No existing test asserted an exact, unsalted digest value (checked with
``grep -rn "sha256\\|hexdigest\\|args_hash ==" tests/`` before writing this
file) — every prior use only compared two digests computed in the same
process for equality, which salting preserves — so none needed updating.
"""

import hashlib
import os
import pathlib
import subprocess
import sys

import pytest

from runbound import api


@pytest.fixture(autouse=True)
def _uninitialized():
    api._teardown_for_tests()
    yield
    api._teardown_for_tests()


def test_hash_salt_is_16_random_bytes():
    assert isinstance(api._HASH_SALT, bytes)
    assert len(api._HASH_SALT) == 16


def test_same_args_hash_the_same_way_in_process():
    first = api._args_hash("search", ("weather",), {"units": "metric"})
    second = api._args_hash("search", ("weather",), {"units": "metric"})
    assert first == second


def test_different_args_hash_differently():
    assert api._args_hash("search", ("weather",), {}) != api._args_hash("search", ("rain",), {})
    assert api._args_hash("search", (), {}) != api._args_hash("email", (), {})


def test_the_hash_differs_from_a_raw_unsalted_sha256():
    canonical = repr(("search", ("weather",), ()))
    raw = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()

    assert api._args_hash("search", ("weather",), {}) != raw


def test_the_hash_is_the_salt_prepended_to_the_canonical_repr():
    canonical = repr(("search", ("weather",), ()))
    expected = hashlib.sha256(
        api._HASH_SALT + canonical.encode("utf-8", "replace")
    ).hexdigest()

    assert api._args_hash("search", ("weather",), {}) == expected


def test_reset_keeps_the_same_salt():
    import runbound

    runbound.init()
    salt_before = api._HASH_SALT
    hash_before = api._args_hash("search", ("weather",), {})

    runbound.reset()

    assert api._HASH_SALT is salt_before
    assert api._args_hash("search", ("weather",), {}) == hash_before


def test_init_keeps_the_same_salt_too():
    """The salt is generated once at import, not re-rolled by init() either."""
    import runbound

    salt_before = api._HASH_SALT
    runbound.init()
    runbound.init(max_steps=5)

    assert api._HASH_SALT is salt_before


CHILD_SCRIPT = """
from runbound import api
print(api._args_hash("search", ("weather",), {}))
"""


def _hash_in_a_fresh_process(repo_root: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT],
        env={**os.environ, "PYTHONPATH": repo_root},
        timeout=30,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_hash_is_not_a_fingerprint_across_processes():
    """Two processes salt the same call differently: the digest is an

    in-process equality token, not something a caller can compute or match
    against from the outside.
    """
    repo_root = str(pathlib.Path(api.__file__).resolve().parent.parent)

    first = _hash_in_a_fresh_process(repo_root)
    second = _hash_in_a_fresh_process(repo_root)

    assert first != second
    assert len(first) == len(second) == 64
