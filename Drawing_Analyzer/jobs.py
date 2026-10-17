# -*- coding: utf-8 -*-
"""Stopping a long step from the page.

Every long request (floor detection, text extraction, boundaries, the
geometry engine, the comparison, areas) carries a job id the page made up,
in the X-DA-Job header. Stop on the page aborts the request and posts that
id to /cancel, which sets the job's flag. The work checks the flag between
floors - jobs.check() - and gives up with Stopped, so the server stops
spending time on a result nobody is waiting for.

What a step finished before it stopped is kept (per-floor results are
cached on disk by the step itself), so Restart picks up where it stopped
rather than starting again.

The flag reaches the work through a context variable set for the request,
so the functions in between do not have to pass it along. Worker
processes do not see it; the parent checks between their results.
"""

from __future__ import annotations

import contextvars
import threading
import time

_FLAGS = {}                      # job id -> threading.Event
_LOCK = threading.Lock()
_CURRENT = contextvars.ContextVar("da_job", default=None)
_MAX_AGE_S = 6 * 3600


class Stopped(BaseException):
    """The user pressed Stop."""


def _flag(job_id):
    with _LOCK:
        now = time.time()
        for k in [k for k, (ev, t) in _FLAGS.items() if now - t > _MAX_AGE_S]:
            _FLAGS.pop(k, None)
        if job_id not in _FLAGS:
            _FLAGS[job_id] = (threading.Event(), now)
        return _FLAGS[job_id][0]


def begin(job_id):
    """Make job_id the current request's job. Returns a token for end()."""
    if not job_id:
        return None
    job_id = str(job_id)[:80]
    ev = _flag(job_id)
    return _CURRENT.set(ev)


def end(token):
    if token is not None:
        try:
            _CURRENT.reset(token)
        except ValueError:
            pass


def cancel(job_id):
    """Ask job_id to stop. Safe when the job has finished or not started:
    a request arriving after Stop is stopped at its first check."""
    if job_id:
        _flag(str(job_id)[:80]).set()


def stopped():
    ev = _CURRENT.get()
    return bool(ev is not None and ev.is_set())


def check():
    """Raise Stopped if the current job was asked to stop."""
    if stopped():
        raise Stopped()


def current_event():
    """The current job's flag, to hand to a thread the request starts."""
    return _CURRENT.get()


def run_with(ev, fn, *args, **kwargs):
    """Call fn with `ev` as the current job (inside a helper thread)."""
    tok = _CURRENT.set(ev)
    try:
        return fn(*args, **kwargs)
    finally:
        _CURRENT.reset(tok)
