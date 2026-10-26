"""Speaker: non-blocking, interruptible, sentence-by-sentence speech with status events.

``speak(reply)`` and ``stop()`` never block the caller. A daemon worker thread cleans and
splits the text (``prepare_speech``), a per-utterance helper thread synthesizes chunk N+1
while the worker plays chunk N (look-ahead of one), and ``StatusChanged`` events are
published on the bus: ``SPEAKING`` when the first audio starts, ``IDLE`` when the utterance
ended (done, stopped or failed), ``ERROR`` (detail ``"tts_failed"``) on a synthesis or
playback failure.

Cancellation uses a generation counter: ``stop()`` / a new ``speak()`` / ``close()`` bump it,
and everything produced under an older generation (queued chunks, a synthesis that was
running at that moment) is dropped instead of played. A small canceller thread calls
``player.stop()`` (repeatedly, until the worker has left ``play()``), so callers never wait
for the player; the worker starts no new utterance until the canceller is done, so a late
``stop()`` cannot cut the next one. Only one ``synthesize()`` runs at a time.

``active`` is true from ``speak()`` (before any audio) until the queue is empty and the
player has returned from ``play()`` (also after stop, barge-in and errors); listeners added
with ``add_activity_listener`` see each change once, under the Speaker lock, so they must be
trivial (lock order FrontEnd -> Speaker -> WakeListener, never reversed).

The UI calls ``speak(reply)`` explicitly (ADR 0005); the Speaker does not subscribe to
``ReplyReady``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from luna.audio.base import AudioClip, AudioPlayer
from luna.config.settings import TTSSettings
from luna.core.clock import Clock
from luna.core.errors import LunaError
from luna.core.events import EventBus, Status, StatusChanged
from luna.core.lang import SUPPORTED_LANGS, detect_lang
from luna.core.models import Reply
from luna.tts.base import TTSProvider
from luna.tts.text import prepare_speech

__all__ = ["Speaker"]

log = logging.getLogger(__name__)

ERROR_DETAIL = "tts_failed"
_POLL_S = 0.05  # how often waiting threads re-check for cancellation
_STOP_RETRY_S = 0.01
_STOP_WAIT_S = 0.5


@dataclass(frozen=True)
class _Job:
    gen: int
    reply: Reply
    request_id: str | None


@dataclass(frozen=True)
class _Synthesized:
    clip: AudioClip


@dataclass(frozen=True)
class _Failed:
    error: BaseException


class _End:
    """Marks the end of a job's chunk stream."""


_END = _End()


class _NotOpen:
    """Type of ``_NOT_OPEN``: no SPEAKING is waiting for its IDLE."""


_NOT_OPEN = _NotOpen()


class Speaker:
    def __init__(
        self,
        tts: TTSProvider,
        player: AudioPlayer,
        bus: EventBus,
        settings: TTSSettings,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self._tts = tts
        self._player = player
        self._bus = bus
        self._settings = settings
        self._clock = clock

        self._cond = threading.Condition()
        self._gen = 0
        self._pending: _Job | None = None
        self._playing = False  # worker is inside (or about to enter) player.play()
        self._cancel_requests = 0  # stop requests the canceller has not finished yet
        self._open: str | _NotOpen | None = _NOT_OPEN  # request_id of a SPEAKING without IDLE
        self._muted = False
        self._closed = False
        self._running = False  # the worker is inside a job (from taking it to finishing it)
        self._active = False
        self._listeners: list[Callable[[bool], None]] = []
        self._synth_lock = threading.Lock()  # at most one synthesize() at a time
        self._synth_threads: list[threading.Thread] = []

        self._worker = threading.Thread(target=self._run, name="luna-speaker", daemon=True)
        self._canceller = threading.Thread(
            target=self._cancel_loop, name="luna-speaker-cancel", daemon=True
        )
        self._worker.start()
        self._canceller.start()

    # ---- public API (thread-safe, never blocks on the player, never raises) -------

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def active(self) -> bool:
        """True while speech is queued or playing (see the module docstring)."""
        return self._active

    def add_activity_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``listener(active)`` on every change; returns an idempotent unsubscribe.

        Runs under the Speaker lock: keep it trivial and never call back into the Speaker.
        """
        with self._cond:
            self._listeners.append(listener)

        def remove() -> None:
            with self._cond:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def speak(self, reply: Reply, request_id: str | None = None) -> None:
        """Speak ``reply`` in the background; any current speech is stopped first."""
        with self._cond:
            if self._muted or self._closed:
                return
            self._cancel()
            self._pending = _Job(self._gen, reply, request_id)
            self._update_active()

    def stop(self) -> None:
        """Cut current speech and drop everything queued. Idempotent."""
        with self._cond:
            self._cancel()
            self._update_active()

    def set_muted(self, muted: bool) -> None:
        with self._cond:
            self._muted = muted
            if muted:
                self._cancel()
                self._update_active()

    def close(self, timeout: float = 2.0) -> None:
        """Stop speaking, join the threads and close the player. Idempotent."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cancel()
            self._update_active()
            threads = [self._worker, self._canceller, *self._synth_threads]
        deadline = self._clock() + timeout
        me = threading.current_thread()
        for t in threads:
            if t is not me:
                t.join(max(0.0, deadline - self._clock()))
        try:
            self._player.close()
        except Exception:
            log.exception("closing the audio player failed")

    # ---- internals ----------------------------------------------------------------

    def _cancel(self) -> None:
        """Invalidate everything in flight (call with ``_cond`` held)."""
        self._gen += 1
        self._cancel_requests += 1
        self._pending = None
        self._cond.notify_all()

    def _update_active(self) -> None:
        """Publish ``active`` to the listeners if it changed (call with ``_cond`` held)."""
        active = self._pending is not None or self._running
        if active == self._active:
            return
        self._active = active
        for listener in list(self._listeners):
            try:
                listener(active)
            except Exception:
                log.exception("speaker activity listener failed")

    def _cancel_loop(self) -> None:
        while True:
            with self._cond:
                while self._cancel_requests == 0 and not self._closed:
                    self._cond.wait()
                if self._cancel_requests == 0:
                    return
                handled = self._cancel_requests
            self._interrupt_player()
            with self._cond:
                self._cancel_requests -= handled
                self._cond.notify_all()

    def _interrupt_player(self) -> None:
        """Call ``player.stop()`` until the worker has left ``play()`` (bounded wait).

        The worker decides to play under the lock and calls ``play()`` outside it, so a
        single ``stop()`` could land just before ``play()`` starts and be a no-op;
        repeating it while the worker is in that window closes the race.
        """
        deadline = time.monotonic() + _STOP_WAIT_S
        while True:
            try:
                self._player.stop()
            except Exception:
                log.exception("stopping the audio player failed")
            with self._cond:
                if not self._playing:
                    return
                self._cond.wait(_STOP_RETRY_S)
                if not self._playing or time.monotonic() >= deadline:
                    return

    def _run(self) -> None:
        while True:
            with self._cond:
                while (self._pending is None or self._cancel_requests) and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job, self._pending = self._pending, None
                if job is not None:
                    self._running = True  # still active: pending -> running, no change
            if job is None:
                continue
            try:
                self._speak_job(job)
            except Exception:
                log.exception("unexpected speaker failure")
            finally:
                self._finish(failed=False)
                with self._cond:
                    self._running = False
                    self._update_active()

    def _current(self, job: _Job) -> bool:
        return self._gen == job.gen

    def _finish(self, *, failed: bool) -> None:
        """Close an open SPEAKING with IDLE, unless a newer utterance is already queued."""
        with self._cond:
            rid = self._open
            if isinstance(rid, _NotOpen):
                return
            if self._pending is not None and not failed:
                return  # superseded: the next SPEAKING replaces it, no IDLE blink
            self._open = _NOT_OPEN
            self._bus.publish(StatusChanged(Status.IDLE, rid))

    def _speak_job(self, job: _Job) -> None:
        reply = job.reply
        lang = reply.lang if reply.lang in SUPPORTED_LANGS else detect_lang(reply.text, reply.lang)
        chunks = prepare_speech(
            reply.text,
            first_max_chars=self._settings.first_chunk_max_chars,
            max_chars=self._settings.max_chars,
        )
        if not chunks:
            return

        results: queue.Queue[_Synthesized | _Failed | _End] = queue.Queue()
        permits = threading.Semaphore(0)  # released when the player starts a chunk
        cancel = threading.Event()  # set when this job is over, so its helper exits
        helper = threading.Thread(
            target=self._synthesize_all,
            args=(job, chunks, lang, results, permits, cancel),
            name="luna-speaker-tts",
            daemon=True,
        )
        with self._cond:
            self._synth_threads = [t for t in self._synth_threads if t.is_alive()]
            self._synth_threads.append(helper)
        helper.start()

        started = self._clock()
        try:
            while True:
                item = self._next_result(job, results)
                if item is None or isinstance(item, _End):
                    break
                if isinstance(item, _Failed):
                    self._report_failure(job, item.error)
                    break
                with self._cond:
                    if not self._current(job):
                        break
                    self._playing = True
                    if isinstance(self._open, _NotOpen) or self._open != job.request_id:
                        self._open = job.request_id
                        self._bus.publish(StatusChanged(Status.SPEAKING, job.request_id))
                        log.debug("first audio after %.2fs", self._clock() - started)
                permits.release()
                try:
                    self._player.play(item.clip)
                except Exception as e:
                    self._report_failure(job, e)
                    break
                finally:
                    with self._cond:
                        self._playing = False
                        self._cond.notify_all()
                if not self._current(job):
                    break
        finally:
            cancel.set()

    def _next_result(
        self, job: _Job, results: queue.Queue[_Synthesized | _Failed | _End]
    ) -> _Synthesized | _Failed | _End | None:
        while True:
            if not self._current(job):
                return None
            try:
                return results.get(timeout=_POLL_S)
            except queue.Empty:
                continue

    def _report_failure(self, job: _Job, error: BaseException) -> None:
        """ERROR then IDLE, unless the failure is a side effect of a stop (stale job)."""
        with self._cond:
            if not self._current(job):
                return
        # Never log the reply text at INFO; exception type and message only.
        if isinstance(error, LunaError):
            log.warning("speech failed: %s: %s", type(error).__name__, error)
        else:
            log.error("speech failed", exc_info=error)
        with self._cond:
            if not self._current(job):
                return
            self._bus.publish(StatusChanged(Status.ERROR, job.request_id, ERROR_DETAIL))
            self._open = job.request_id  # IDLE must follow
        self._finish(failed=True)

    def _synthesize_all(
        self,
        job: _Job,
        chunks: list[str],
        lang: str,
        results: queue.Queue[_Synthesized | _Failed | _End],
        permits: threading.Semaphore,
        cancel: threading.Event,
    ) -> None:
        def stale() -> bool:
            return cancel.is_set() or not self._current(job)

        for index, chunk in enumerate(chunks):
            # Look-ahead of one: chunk k is synthesized once the player started chunk k-1.
            while index > 0 and not permits.acquire(timeout=_POLL_S):
                if stale():
                    return
            while not self._synth_lock.acquire(timeout=_POLL_S):
                if stale():
                    return
            try:
                if stale():
                    return
                try:
                    clip = self._tts.synthesize(chunk, lang)
                except Exception as e:
                    if not stale():
                        results.put(_Failed(e))
                    return
            finally:
                self._synth_lock.release()
            if stale():
                return  # stopped while synthesizing: discard the result
            results.put(_Synthesized(clip))
        results.put(_END)
