"""Database models backing the MCP transport layer.

These replace the former Redis-backed state (session registry, per-session message
queue, session stats, tool sequence). Living in the database means the data is
shared across every gunicorn worker/replica — the property that previously
required Redis — so `GET /mcp` and `POST /mcp/message` for the same session can
land on different processes and still find each other.

Rows live until explicitly deleted (session close, message delivery, code
redemption); there is no expiry/TTL. Credentials are stored as plain JSON, matching
how they're stored elsewhere (encryption was deliberately removed).
"""
from django.db import models


class SSESession(models.Model):
    """An open SSE session and its live CDS/CMS credentials. Replaces the Redis
    `mcp:session:{id}` string + `mcp:active_sessions` set."""

    session_id       = models.CharField(max_length=64, unique=True, db_index=True)
    credentials      = models.JSONField(default=dict)
    session_trace_id = models.CharField(max_length=128, blank=True, default="")
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mcp_sse_session"


class SSEMessage(models.Model):
    """A queued JSON-RPC response awaiting delivery on a session's SSE stream.
    FIFO by auto-increment `id`. Replaces the Redis `mcp:session_queue:{id}` list."""

    session_id  = models.CharField(max_length=64, db_index=True)
    payload     = models.JSONField()
    enqueued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mcp_sse_message"
        ordering = ["id"]


class SessionStats(models.Model):
    """Per-SSE-session telemetry. Replaces the Redis `mcp:session_stats:{id}` hash.
    Counters are updated atomically via F() expressions in `protocol/session_stats`."""

    session_id                    = models.CharField(max_length=64, unique=True, db_index=True)
    tool_count                    = models.IntegerField(default=0)
    error_count                   = models.IntegerField(default=0)
    degraded_count                = models.IntegerField(default=0)
    session_start_time            = models.FloatField(default=0.0)
    total_tool_duration_ms        = models.FloatField(default=0.0)
    total_estimated_input_tokens  = models.BigIntegerField(default=0)
    total_estimated_output_tokens = models.BigIntegerField(default=0)
    last_tool_end_perf            = models.FloatField(null=True, blank=True)
    client_name                   = models.CharField(max_length=255, null=True, blank=True)
    session_trace_id              = models.CharField(max_length=128, blank=True, default="")
    create_op_count               = models.IntegerField(default=0)
    update_delete_op_count        = models.IntegerField(default=0)

    class Meta:
        db_table = "mcp_session_stats"


class SessionToolEvent(models.Model):
    """One entry in a session's ordered tool-call sequence. Replaces the Redis
    `mcp:session_stats:{id}:tool_sequence` list."""

    session_id = models.CharField(max_length=64, db_index=True)
    tool_name  = models.CharField(max_length=255)

    class Meta:
        db_table = "mcp_session_tool_event"
        ordering = ["id"]
