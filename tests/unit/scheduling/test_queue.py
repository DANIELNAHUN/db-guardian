import threading
import time

import pytest

from db_guardian.scheduling.queue import WorkQueue


@pytest.fixture
def queue():
    q = WorkQueue()
    q.start()
    yield q
    q.stop(timeout=5)


def test_queue_runs_one_at_a_time(queue):
    intervals = []

    def job():
        start = time.monotonic()
        time.sleep(0.05)
        intervals.append((start, time.monotonic()))

    assert queue.submit("a", job)
    assert queue.submit("b", job)
    assert queue.submit("c", job)
    queue.join()
    intervals.sort()
    assert len(intervals) == 3
    for (_, end), (next_start, _) in zip(intervals, intervals[1:]):
        assert next_start >= end


def test_queue_dedupes_pending_key(queue):
    release = threading.Event()
    ran = []
    queue.submit("blocker", release.wait)
    assert queue.submit("k", lambda: ran.append(1)) is True
    assert queue.submit("k", lambda: ran.append(2)) is False
    release.set()
    queue.join()
    assert ran == [1]


def test_key_can_be_resubmitted_once_started(queue):
    started, release = threading.Event(), threading.Event()

    def long_job():
        started.set()
        release.wait()

    queue.submit("k", long_job)
    started.wait(timeout=5)
    assert queue.submit("k", lambda: None) is True
    release.set()
    queue.join()


def test_queue_survives_job_exception(queue):
    ran = []

    def boom():
        raise RuntimeError("boom")

    queue.submit("bad", boom)
    queue.submit("good", lambda: ran.append("ok"))
    queue.join()
    assert ran == ["ok"]
