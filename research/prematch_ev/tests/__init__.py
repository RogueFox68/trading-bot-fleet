"""Shared test helpers."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager


@contextmanager
def machine_timezone(name: str):
    """Run with the MACHINE's local timezone set to `name`, then restore it.

    Anything that calls `datetime.astimezone()` without an argument reads the
    machine's zone, and a suite that only ever runs on UTC -- as CI and the
    sandbox that wrote this do -- cannot see what that code does anywhere
    else. A cache that served 12:00Z for a 17:00Z request on a UTC-5 machine
    passed every run on UTC.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()
