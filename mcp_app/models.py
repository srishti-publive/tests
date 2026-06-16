"""Database models backing the MCP transport layer.

These hold the cross-process transport state (session registry, per-session
message queue, session stats, tool sequence). Living in the database means the
data is shared across every gunicorn worker/replica, so `GET /mcp` and
`POST /mcp/message` for the same session can land on different processes and
still find each other.

Most rows live until explicitly deleted (message delivery, code redemption).
`SSESession` is the exception: it carries a `last_seen` heartbeat so streams that
die without a clean close go stale and get pruned (see session_registry), rather
than leaking rows that block the admission gate. Credentials are stored as plain
JSON, matching how they're stored elsewhere (encryption was deliberately removed).
"""
from django.db import models


class SSESession(models.Model):
    """An open SSE session and its live CDS/CMS credentials."""

    session_id       = models.CharField(max_length=64, unique=True, db_index=True)
    credentials      = models.JSONField(default=dict)
    session_trace_id = models.CharField(max_length=128, blank=True, default="")
    created_at       = models.DateTimeField(auto_now_add=True)
    # Heartbeat: refreshed on every keepalive/message so a stream that dies without
    # a clean close (TCP reset, worker kill, redeploy) goes stale and stops counting
    # against the admission gate, instead of leaking a row that blocks new sessions
    # forever. See session_registry.active_count() / prune_stale_sessions().
    last_seen        = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "mcp_sse_session"


class SSEMessage(models.Model):
    """A queued JSON-RPC response awaiting delivery on a session's SSE stream.
    FIFO by auto-increment `id`."""

    session_id  = models.CharField(max_length=64, db_index=True)
    payload     = models.JSONField()
    enqueued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mcp_sse_message"
        ordering = ["id"]


class SessionStats(models.Model):
    """Per-SSE-session telemetry. Counters are updated atomically via F()
    expressions in `protocol/session_stats`."""

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
    """One entry in a session's ordered tool-call sequence."""

    session_id = models.CharField(max_length=64, db_index=True)
    tool_name  = models.CharField(max_length=255)

    class Meta:
        db_table = "mcp_session_tool_event"
        ordering = ["id"]
