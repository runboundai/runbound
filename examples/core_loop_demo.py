#!/usr/bin/env python3
"""The same walkthrough as ``python -m runbound.demo``, from the examples
directory: a runaway, detected, narrowed to a safer posture, a dangerous
action denied, the run stopped -- entirely offline, against a fake provider
transport, no account and no API key required.

    .venv/bin/python examples/core_loop_demo.py

See ``runbound/demo.py`` for the walkthrough itself; this file only calls
its public entry point, the same way a stranger running ``python -m
runbound.demo`` after ``pip install runbound`` would reach it.
"""

import sys

from runbound.demo import main

if __name__ == "__main__":
    sys.exit(main())
