"""Logs a `server_hit` ActivityEvent for every /api/ request that can be
tied to a session, so the admin shows a full trail of "when the server
was hit" alongside the explicit screen/recording events.

Views resolve the StudySession themselves and stash it on
`request._study_session` so this middleware doesn't have to re-parse the
body (which, for JSON views, can only be read once). Views that fail to
resolve a session (bad/missing session_key) simply don't get a hit
logged here — the view's own error response is what matters for those.
"""

from .models import ActivityEvent

# Avoid double-logging: log_event already *is* an explicit event; logging
# a server_hit for hitting the log-event endpoint itself is just noise.
EXCLUDED_PATHS = {"/api/log-event/"}


class ServerHitLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.path.startswith("/api/") and request.path not in EXCLUDED_PATHS:
            session = getattr(request, "_study_session", None)
            if session is not None:
                try:
                    ActivityEvent.objects.create(
                        session=session,
                        event_type="server_hit",
                        request_path=request.path,
                        detail={"method": request.method, "status_code": response.status_code},
                    )
                except Exception:
                    # Never let logging failures break the actual response.
                    pass

        return response
