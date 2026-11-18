import logging
from time import monotonic
from .observability import context, event, request_id


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = monotonic()
        rid = request_id(request.headers.get("X-Request-ID"))
        token = context.set({"request_id": rid, "source": "api"})
        request.request_id = rid
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = rid
            quiet = request.method in {"GET", "HEAD", "OPTIONS"} and response.status_code < 500 and response.status_code not in {401, 403}
            event("http.request", traffic=quiet,
                  level=logging.ERROR if response.status_code >= 500 else logging.WARNING if response.status_code in {401, 403} else logging.INFO,
                  method=request.method, path=request.path, status=response.status_code,
                  reason_code=getattr(request, "activity_reason", None),
                  duration_ms=round((monotonic() - started) * 1000, 2))
            return response
        finally:
            context.reset(token)
