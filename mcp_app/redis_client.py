"""Shared Redis client for cross-process MCP state (sessions, queues, stats, rate limits).

Distinct from Django's CACHES (which also uses Redis via django-redis): this module
exposes the raw redis-py client for primitives CACHES doesn't cover — blocking list
ops, hash field increments, atomic counters with TTL. One connection pool, lazily
built from REDIS_URL so importing this module has no side effects at startup.
"""
import os
from typing import Optional

import redis

_client: Optional["redis.Redis"] = None


def get_redis_client() -> "redis.Redis":
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        _client = redis.Redis.from_url(url, decode_responses=True)
    return _client
