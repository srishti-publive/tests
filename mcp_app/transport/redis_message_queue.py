"""Redis-backed replacement for the per-session in-process `queue.Queue`.

Each open SSE session gets a capped Redis list acting as its message-delivery
queue: `handle_sse_message` (running on whichever process received the POST)
pushes the JSON-RPC response; the `event_stream` generator (running on whichever
process is holding the GET stream — possibly a different one) blocks on `BLPOP`
to consume it. This is the half of the externalization that actually lets GET
and POST land on different processes.

Chosen over Pub/Sub (drops messages for momentarily-disconnected subscribers —
breaking the no-loss guarantee `queue.Queue` provides today) and over Streams
(consumer-group/XACK/XTRIM machinery for a strictly one-producer/one-consumer
pattern that needs none of it). A capped list + `BLPOP(timeout)` mirrors
`queue.Queue(maxsize).get(timeout=N)` almost exactly.

Timestamps use wall-clock `time.time()`, not `time.perf_counter()`: the
producer and consumer may be different processes, and `perf_counter`'s
reference point is per-process — comparing it across processes yields garbage.
"""
import json
import logging
import time
from typing import Optional

from mcp_app.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_QUEUE_TTL_SECONDS = 24 * 3600  # mirrors the session TTL in redis_session_store


def _queue_key(session_id: str) -> str:
    return f"mcp:session_queue:{session_id}"


def push_message(session_id: str, message: dict, maxsize: int, timeout: float = 30.0) -> bool:
    """Enqueue a message for delivery, waiting up to `timeout` seconds for room
    if the queue is at `maxsize` (mirrors `queue.Queue(maxsize).put(block=True,
    timeout=30)`). Returns False on persistent overflow — the caller logs it and
    records `Custom/MCP/queue_overflow_count`, exactly like the `queue.Full` path
    it replaces.

    Redis has no blocking-push primitive, so this polls `LLEN` with backoff. The
    check-then-push isn't atomic, so concurrent producers can occasionally land
    the list a couple of items over `maxsize` — a soft cap, same tolerance the
    in-process bounded queue had under concurrent producers.
    """
    client  = get_redis_client()
    key     = _queue_key(session_id)
    payload = json.dumps([time.time(), message])

    deadline = time.monotonic() + timeout
    backoff  = 0.05
    while True:
        if client.llen(key) < maxsize:
            with client.pipeline() as pipe:
                pipe.rpush(key, payload)
                pipe.expire(key, _QUEUE_TTL_SECONDS)
                pipe.execute()
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(backoff)
        backoff = min(backoff * 2, 1.0)


def pop_message(session_id: str, timeout: float) -> Optional[tuple[float, dict]]:
    """Block up to `timeout` seconds for the next message. Returns
    `(queue_wait_ms, message)`, or None on timeout (→ caller emits a keepalive,
    mirroring the `queue.Empty` path of `queue.Queue.get(timeout=N)`)."""
    client = get_redis_client()
    result = client.blpop([_queue_key(session_id)], timeout=timeout)
    if result is None:
        return None

    _, raw            = result
    enqueued_at, message = json.loads(raw)
    wait_ms = round((time.time() - enqueued_at) * 1000, 2)
    return wait_ms, message


def delete_queue(session_id: str) -> None:
    """Drop the queue on session close."""
    get_redis_client().delete(_queue_key(session_id))


def queue_depth(session_id: str) -> int:
    """Current queue length — replaces `msg_queue.qsize()` for the
    `mcp.session_queue_depth` attribute / `Custom/MCP/session_queue_depth` metric."""
    return get_redis_client().llen(_queue_key(session_id))
