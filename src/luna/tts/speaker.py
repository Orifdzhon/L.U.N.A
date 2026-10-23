"""Speaker: non-blocking, interruptible, sentence-by-sentence speech with status events.

``speak(reply)`` returns at once. A daemon worker thread cleans and splits the text
(``prepare_speech``), a per-utterance helper thread synthesizes chunk N+1 while the worker
plays chunk N (look-ahead of one), and ``StatusChanged`` events are published on the bus:
``SPEAKING`` when the first audio starts, ``IDLE`` when the utterance ended (done, stopped
or failed), ``ERROR`` (detail ``"tts_failed"``) on a synthesis or playback failure.

Cancellation uses a generation counter: ``stop()`` / a new ``speak()`` / ``close()`` bump it,
and everything produced under an older generation (queued chunks, a synthesis that was
running at that moment) is dropped instead of played. The UI calls ``speak(reply)``
explicitly (ADR 0005); the Speaker does not subscribe to ``ReplyReady``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from luna.audio.base import AudioClip, AudioPlayer
from luna.config.settings import TTSSettings
from luna.core.clock import Clock
from luna.core.errors import LunaError
from luna.core.events import EventBus, Status, StatusChanged
from luna.core.models import Reply
from luna.core.router import detect_lang
from luna.tts.base import TTSProvider
from luna.tts.text import prepare_speech

__all__ = ["Speaker"]

log = logging.getLogger(__name__)

ERROR_DETAIL = "tts_failed"
_POLL_S = 0.05  # how often waiting threads re-check for cancellation
_STOP_RETRY_S = 0.01
_STOP_WAIT_S = 0.5
_LANGS = ("ru", "en")


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
        self._interrupting = 0  # callers currently inside _interrupt_player()
        self._muted = False
        self._closed = False
        self._synth_threads: list[threading.Thread] = []

        self._worker = threading.Thread(target=self._run, name="luna-speaker", daemon=True)
        self._worker.start()

    # ---- public API (thread-safe, never raises into the caller) -------------------

    @property
    def muted(self) -> bool:
        return self._muted

    def speak(self, reply: Reply, request_id: str | None = None) -> None:
        """Speak ``reply`` in the background; any current speech is stopped first."""
        with self._cond:
            if self._muted or self._closed:
                return
            self._gen += 1
            self._interrupting += 1
            self._pending = _Job(self._gen, reply, request_id)
            self._cond.notify_all()
        self._interrupt_player()

    def stop(self) -> None:
        """Cut current speech and drop everything queued. Idempotent."""
        with self._cond:
            self._gen += 1
            self._interrupting += 1
            self._pending = None
            self._cond.notify_all()
        self._interrupt_player()

    def set_muted(self, muted: bool) -> None:
        with self._cond:
            self._muted = muted
        if muted:
            self.stop()

    def close(self, timeout: float = 2.0) -> None:
        """Stop speaking, join the threads and close the player. Idempotent."""
        with self._cond:
            already = self._closed
            self._closed = True
            self._gen += 1
            self._interrupting += 1
            self._pending = None
            self._cond.notify_all()
        self._interrupt_player()
        if already:
            return
        deadline = self._clock() + timeout
        self._worker.join(timeout)
        with self._cond:
            helpers = list(self._synth_threads)
        for t in helpers:
            t.join(max(0.0, deadline - self._clock()))
        try:
            self._player.close()
        except Exception:
            log.exception("closing the audio player failed")

    # ---- internals ----------------------------------------------------------------

    def _interrupt_player(self) -> None:
        """Call ``player.stop()`` until the worker has left ``play()``.

        Callers bump ``_interrupting`` together with the generation; the worker starts no new
        utterance until the count is back to zero, so a late ``stop()`` can never hit the
        next utterance. The worker decides to play under the lock and calls ``play()``
        outside it, so a single ``stop()`` could land just before ``play()`` starts and be a
        no-op; repeating it while the worker is inside that window closes the race.
        """
        deadline = time.monotonic() + _STOP_WAIT_S
        try:
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
        finally:
            with self._cond:
                self._interrupting -= 1
                self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while (self._pending is None or self._interrupting) and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job, self._pending = self._pending, None
            if job is None:
                continue
            try:
                self._speak_job(job)
            except Exception:
                log.exception("unexpected speaker failure")

    def _current(self, job: _Job) -> bool:
        return self._gen == job.gen

    def _speak_job(self, job: _Job) -> None:
        reply = job.reply
        lang = reply.lang if reply.lang in _LANGS else detect_lang(reply.text, reply.lang)
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
        speaking = False
        failed = False
        try:
            while True:
                item = self._next_result(job, results)
                if item is None or isinstance(item, _End):
                    break
                if isinstance(item, _Failed):
                    failed = True
                    self._report_failure(job, item.error)
                    break
                with self._cond:
                    if not self._current(job):
                        break
                    self._playing = True
                    if not speaking:
                        speaking = True
                        self._bus.publish(StatusChanged(Status.SPEAKING, job.request_id))
                        log.debug("first audio after %.2fs", self._clock() - started)
                permits.release()
                try:
                    self._player.play(item.clip)
                except Exception as e:
                    if self._current(job):
                        failed = True
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
            if speaking or failed:
                self._bus.publish(StatusChanged(Status.IDLE, job.request_id))

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
        # Never log the reply text at INFO; exception type and message only.
        if isinstance(error, LunaError):
            log.warning("speech failed: %s: %s", type(error).__name__, error)
        else:
            log.error("speech failed", exc_info=error)
        self._bus.publish(StatusChanged(Status.ERROR, job.request_id, ERROR_DETAIL))

    def _synthesize_all(
        self,
        job: _Job,
        chunks: list[str],
        lang: str,
        results: queue.Queue[_Synthesized | _Failed | _End],
        permits: threading.Semaphore,
        cancel: threading.Event,
    ) -> None:
        for index, chunk in enumerate(chunks):
            # Look-ahead of one: chunk k is synthesized once the player started chunk k-1.
            while index > 0 and not permits.acquire(timeout=_POLL_S):
                if cancel.is_set() or not self._current(job):
                    return
            if cancel.is_set() or not self._current(job):
                return
            try:
                clip = self._tts.synthesize(chunk, lang)
            except Exception as e:
                if self._current(job) and not cancel.is_set():
                    results.put(_Failed(e))
                return
            if cancel.is_set() or not self._current(job):
                return  # stopped while synthesizing: discard the result
            results.put(_Synthesized(clip))
        results.put(_END)
