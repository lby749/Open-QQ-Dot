"""The clock of one proactive attempt (timing judgment, generation, care round), on the calling thread.

The attempt sets an end time with limit(); every model call checks it before starting (check()) and bounds its own
timeout by what is left (clamp()), so a slow turn cannot run past the attempt's end by a full call. Nested limits keep
the earlier end. Outside a limit nothing changes: clamp() returns the timeout it was given and check() does nothing.
"""
import contextvars
import time
from contextlib import contextmanager

_end = contextvars.ContextVar('round_end', default=None)


@contextmanager
def limit(seconds):
    current = _end.get()
    end = time.monotonic_ns() + int(seconds * 1_000_000_000)
    token = _end.set(end if current is None else min(current, end))
    try:
        yield
    finally:
        _end.reset(token)


def clamp(seconds):
    """How long a model call may run: its own limit, or the time left in the attempt when that is shorter."""
    end = _end.get()
    return seconds if end is None else min(seconds, max(0, end - time.monotonic_ns()) / 1_000_000_000)


def check():
    """Before a model call: past the attempt's end the call is not started and the attempt fails unsent."""
    end = _end.get()
    if end is not None and time.monotonic_ns() >= end:
        raise RuntimeError('主动轮次超过总时限；本轮未发送')
