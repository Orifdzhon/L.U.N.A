import queue
import threading

from luna.core.events import (
    CLOSED,
    EventBus,
    ProviderSwitched,
    Status,
    StatusChanged,
    TranscriptReady,
)


def drain(sub) -> list:
    out = []
    while True:
        try:
            out.append(sub.queue.get_nowait())
        except queue.Empty:
            return out


def test_two_subscribers_receive_every_event_in_order():
    bus = EventBus()
    s1, s2 = bus.subscribe(), bus.subscribe()
    events = [
        StatusChanged(Status.LISTENING),
        TranscriptReady("r1", "hello"),
        StatusChanged(Status.THINKING),
        ProviderSwitched("groq", "ollama", "rate limited"),
    ]
    for e in events:
        bus.publish(e)
    assert drain(s1) == events
    assert drain(s2) == events


def test_unsubscribed_queue_receives_nothing_but_closed_marker():
    bus = EventBus()
    s = bus.subscribe()
    other = bus.subscribe()
    bus.publish(TranscriptReady("r", "a"))
    s.close()
    bus.publish(TranscriptReady("r", "b"))
    assert drain(s) == [TranscriptReady("r", "a"), CLOSED]
    assert drain(other) == [TranscriptReady("r", "a"), TranscriptReady("r", "b")]


def test_close_is_idempotent_and_unsubscribe_is_alias():
    bus = EventBus()
    s = bus.subscribe()
    s.close()
    s.close()
    bus.unsubscribe(s)
    assert drain(s) == [CLOSED]


def test_context_manager_closes():
    bus = EventBus()
    with bus.subscribe() as s:
        bus.publish(TranscriptReady("r", "a"))
    assert drain(s) == [TranscriptReady("r", "a"), CLOSED]


def test_publish_without_subscribers_is_noop():
    EventBus().publish(TranscriptReady("r", "a"))


def test_event_optional_fields_default():
    e = StatusChanged(Status.ERROR)
    assert e.request_id is None and e.detail is None
    assert StatusChanged(Status.ERROR, "r1", "boom").detail == "boom"
    assert ProviderSwitched("a", "b").request_id is None
    assert ProviderSwitched("a", "b", request_id="r1").request_id == "r1"


def test_late_subscriber_is_seeded_with_latest_status():
    bus = EventBus()
    assert bus.last_status is None
    assert drain(bus.subscribe()) == []
    bus.publish(StatusChanged(Status.LISTENING))
    bus.publish(TranscriptReady("r", "x"))
    bus.publish(StatusChanged(Status.THINKING))
    late = bus.subscribe()
    assert bus.last_status == StatusChanged(Status.THINKING)
    assert drain(late) == [StatusChanged(Status.THINKING)]
    bus.publish(StatusChanged(Status.IDLE))
    assert drain(late) == [StatusChanged(Status.IDLE)]


def test_bus_close_wakes_blocked_consumer_and_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe()
    got: list = []

    def consume() -> None:
        while (item := sub.queue.get(timeout=10)) is not CLOSED:
            got.append(item)

    t = threading.Thread(target=consume)
    t.start()
    bus.publish(TranscriptReady("r", "a"))
    bus.close()
    t.join(timeout=10)
    assert not t.is_alive()
    assert got == [TranscriptReady("r", "a")]
    bus.publish(TranscriptReady("r", "ignored"))
    assert drain(sub) == []
    assert drain(bus.subscribe()) == [CLOSED]


def test_close_wakes_consumer_blocked_in_get():
    bus = EventBus()
    sub = bus.subscribe()
    started = threading.Event()
    result: list = []

    def consume() -> None:
        started.set()
        result.append(sub.queue.get(timeout=10))

    t = threading.Thread(target=consume)
    t.start()
    started.wait(10)
    sub.close()
    t.join(timeout=10)
    assert result == [CLOSED]


def test_publish_from_other_threads_is_complete_and_ordered_per_thread():
    bus = EventBus()
    s1, s2 = bus.subscribe(), bus.subscribe()
    n_threads, per_thread = 8, 200
    barrier = threading.Barrier(n_threads)

    def work(tid: int) -> None:
        barrier.wait()  # start all publishers together
        for i in range(per_thread):
            bus.publish(TranscriptReady(f"t{tid}", str(i)))

    threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    got1, got2 = drain(s1), drain(s2)
    assert len(got1) == len(got2) == n_threads * per_thread
    assert got1 == got2  # same global order for all subscribers
    for tid in range(n_threads):
        seq = [int(e.text) for e in got1 if e.request_id == f"t{tid}"]
        assert seq == list(range(per_thread))


def test_transient_subscribers_see_contiguous_slices_during_publish():
    bus = EventBus()
    cycles = 50
    barrier = threading.Barrier(2)
    done = threading.Event()
    slices: list[list[int]] = []

    def churn() -> None:
        barrier.wait()
        for _ in range(cycles):
            sub = bus.subscribe()
            # Let the subscription collect some events before closing it
            # (the publisher keeps going until all cycles are done).
            while sub.queue.qsize() < 20:
                pass
            sub.close()
            slices.append([int(e.text) for e in drain(sub) if e is not CLOSED])
        done.set()

    t = threading.Thread(target=churn)
    t.start()
    barrier.wait()
    i = 0
    while not done.is_set():
        bus.publish(TranscriptReady("r", str(i)))
        i += 1
    t.join(timeout=30)
    assert not t.is_alive()
    assert len(slices) == cycles
    for seq in slices:
        assert len(seq) >= 20
        # No gaps and no reordering: each is a run of consecutive numbers.
        assert seq == list(range(seq[0], seq[0] + len(seq)))
