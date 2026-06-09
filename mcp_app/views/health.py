from django.http import JsonResponse
import newrelic.agent

from mcp_app.nr_utils import suppress_apdex, suppress_trace
from mcp_app.protocol.dispatch import PROTOCOL_VERSION


@newrelic.agent.function_trace(name="health_check", group="Transport")
def health_check(request):
    suppress_apdex()
    suppress_trace()
    return JsonResponse({
        "status":   "ok",
        "service":  "publive-cds-mcp",
        "version":  "1.0.0",
        "protocol": PROTOCOL_VERSION,
    })
