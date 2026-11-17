#!/usr/bin/env python3
"""
In-process pub/sub for Server-Sent Events.

Each subscriber owns a queue.Queue. Broadcasters push events to all queues
without blocking; a subscriber that cannot keep up loses its oldest events
rather than being disconnected.
"""
import queue
import threading

_subscribers = set()
_lock = threading.Lock()

QUEUE_SIZE = 200


def subscribe():
    """Register a new subscriber and return its queue."""
    q = queue.Queue(maxsize=QUEUE_SIZE)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q):
    """Remove a subscriber (safe to call more than once)."""
    with _lock:
        _subscribers.discard(q)


def broadcast(event_type, data):
    """Push an event to every subscriber; never blocks the caller."""
    message = {"type": event_type, "data": data}
    with _lock:
        targets = list(_subscribers)

    for q in targets:
        try:
            q.put_nowait(message)
        except queue.Full:
            # Drop the oldest event to make room for the newest one.
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(message)
            except queue.Full:
                pass


def subscriber_count():
    with _lock:
        return len(_subscribers)
