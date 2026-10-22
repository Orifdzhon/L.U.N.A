import queue
import threading

from luna.core.events import (
    EventBus,
    ProviderSwitched,
    Status,
    StatusChanged,
    TranscriptReady,
)


def drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_two_subscribers_receive_every_event_in_order():
    bus = EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    events = [
        StatusChanged(Status.LISTENING),
        TranscriptReady("r1", "hello"),
        StatusChanged(Status.THINKING),
        ProviderSwitched("groq", "ollama", "rate limited"),
    ]
    for e in events:
        bus.publish(e)
    assert drain(q1) == events
    assert drain(q2) == events


def test_unsubscribed_queue_receives_nothing():
    bus = EventBus()
    q = bus.subscribe()
    other = bus.subscribe()
    bus.publish(StatusChanged(Status.IDLE))
    bus.unsubscribe(q)
    bus.publish(StatusChanged(Status.ERROR))
    assert drain(q) == [StatusChanged(Status.IDLE)]
    assert drain(other) == [StatusChanged(Status.IDLE), StatusChanged(Status.ERROR)]


def test_unsubscribe_unknown_queue_is_ignored():
    EventBus().unsubscribe(queue.Queue())


def test_publish_without_subscribers_is_noop():
    EventBus().publish(StatusChanged(Status.IDLE))


def test_publish_from_other_threads_is_complete_and_ordered_per_thread():
    bus = EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
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

    got1, got2 = drain(q1), drain(q2)
    assert len(got1) == len(got2) == n_threads * per_thread
    # All subscribers see the same global order.
    assert got1 == got2
    # Within a publishing thread, order is preserved.
    for tid in range(n_threads):
        seq = [int(e.text) for e in got1 if e.request_id == f"t{tid}"]
        assert seq == list(range(per_thread))


def test_consumer_thread_receives_events_published_from_main_thread():
    bus = EventBus()
    q = bus.subscribe()
    received: list = []

    def consume() -> None:
        while True:
            e = q.get(timeout=10)
            if e is None:
                return
            received.append(e)

    t = threading.Thread(target=consume)
    t.start()
    sent = [StatusChanged(Status.THINKING), StatusChanged(Status.SPEAKING)]
    for e in sent:
        bus.publish(e)
    q.put(None)  # sentinel on the subscriber's own queue
    t.join(timeout=10)
    assert not t.is_alive()
    assert received == sent


def test_concurrent_subscribe_unsubscribe_during_publish_does_not_fail():
    bus = EventBus()
    stable = bus.subscribe()
    errors: list[BaseException] = []
    stop = threading.Event()

    def churn() -> None:
        try:
            while not stop.is_set():
                bus.unsubscribe(bus.subscribe())
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=churn)
    t.start()
    for i in range(2000):
        bus.publish(TranscriptReady("r", str(i)))
    stop.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert errors == []
    assert len(drain(stable)) == 2000
