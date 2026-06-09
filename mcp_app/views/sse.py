from django.views.decorators.csrf import csrf_exempt
import newrelic.agent

from mcp_app.transport.sse import handle_sse_message, open_sse_connection


def sse_open(request, credentials: dict, token_expires_at):
    """GET /mcp — open a long-lived SSE stream. Called by the router after auth."""
    return open_sse_connection(request, credentials, token_expires_at)


@csrf_exempt
@newrelic.agent.function_trace(name="mcp_message", group="Transport")
def sse_message(request):
    """POST /mcp/message?sessionId=<id> — deliver a message into an open SSE session."""
    return handle_sse_message(request)
