"""Shared mutable state for SSE session statistics.

Imported by both protocol/dispatch.py (reads/writes tool timing) and
transport/sse.py (creates/destroys session entries) to avoid circular imports.
"""
import threading

# session_id → stats dict (see transport/sse.py for the full schema)
session_stats: dict      = {}
session_stats_lock: threading.Lock = threading.Lock()
