import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .models import ActivityEvent, Participant, QuestionResponse, RecordingChunk, StudySession
from .utils import finalize_all_recordings, finalize_recording


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def parse_client_dt(value):
    """Client sends ISO-8601 timestamps (e.g. Date.toISOString()); tolerate
    missing/unparseable values rather than 500ing the request."""
    if not value:
        return None
    return parse_datetime(value)


def get_session_or_error(payload):
    """Resolve a StudySession from a session_key in the request payload.
    Returns (session, error_response). Exactly one is non-None."""
    session_key = payload.get("session_key")
    if not session_key:
        return None, JsonResponse({"error": "session_key is required"}, status=400)
    try:
        session = StudySession.objects.select_related("participant").get(
            session_key=session_key
        )
    except (StudySession.DoesNotExist, ValueError, TypeError):
        return None, JsonResponse({"error": "unknown or invalid session_key"}, status=404)
    return session, None


@ensure_csrf_cookie
def index(request):
    """Serves the single-page study app. ensure_csrf_cookie so the JS can
    read the csrftoken cookie and send it back on every POST."""
    return render(
        request,
        "study/index.html",
        {
            "stimulus_id": getattr(settings, "REALEYE_STIMULUS_ID", ""),
            "realeye_debug_mode": "true" if settings.DEBUG else "false",
            "participant_ids": json.dumps([str(i) for i in range(1, settings.STUDY_PARTICIPANT_COUNT + 1)]),
        },
    )


@require_POST
def login_view(request):
    """Validates participant id + study password server-side (the client
    also does a quick check for instant feedback, but this is the real
    gate) and opens a new StudySession."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    participant_id = str(payload.get("participant_id", "")).strip()
    password = payload.get("password", "")

    valid_ids = {str(i) for i in range(1, settings.STUDY_PARTICIPANT_COUNT + 1)}
    if participant_id not in valid_ids or password != settings.STUDY_PASSWORD:
        return JsonResponse({"error": "invalid participant ID or password"}, status=401)

    participant, _ = Participant.objects.get_or_create(participant_code=participant_id)
    session = StudySession.objects.create(
        participant=participant,
        stimulus_id=payload.get("stimulus_id", "") or getattr(settings, "REALEYE_STIMULUS_ID", ""),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        ip_address=client_ip(request),
    )
    ActivityEvent.objects.create(
        session=session,
        event_type="session_started",
        client_timestamp=parse_client_dt(payload.get("client_timestamp")),
    )
    request._study_session = session

    return JsonResponse({"session_key": str(session.session_key)})


@require_POST
def log_event(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    session, error = get_session_or_error(payload)
    if error:
        return error
    request._study_session = session

    event_type = payload.get("event_type", "other")
    valid_types = {choice for choice, _ in ActivityEvent.EVENT_TYPES}
    if event_type not in valid_types:
        event_type = "other"

    ActivityEvent.objects.create(
        session=session,
        event_type=event_type,
        screen_name=payload.get("screen_name", "") or "",
        detail=payload.get("detail") or {},
        client_timestamp=parse_client_dt(payload.get("client_timestamp")),
        request_path=payload.get("source_path", "") or "",
    )
    return JsonResponse({"ok": True})


@require_POST
def submit_response(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    session, error = get_session_or_error(payload)
    if error:
        return error
    request._study_session = session

    question_id = payload.get("question_id")
    if not question_id:
        return JsonResponse({"error": "question_id is required"}, status=400)

    response, _created = QuestionResponse.objects.update_or_create(
        session=session,
        question_id=question_id,
        defaults={
            "module_id": payload.get("module_id", ""),
            "section_id": payload.get("section_id", ""),
            "answer_value": payload.get("answer_value", ""),
            "effort_rating": payload.get("effort_rating"),
            "presented_at": parse_client_dt(payload.get("presented_at")),
            "answered_at": parse_client_dt(payload.get("answered_at")),
        },
    )
    return JsonResponse({"ok": True, "response_id": response.id})


@require_POST
def upload_chunk(request):
    session_key = request.POST.get("session_key")
    session, error = get_session_or_error({"session_key": session_key})
    if error:
        return error
    request._study_session = session

    stream_source = request.POST.get("stream_source")
    if stream_source not in dict(RecordingChunk.STREAM_CHOICES):
        return JsonResponse({"error": "stream_source must be 'webcam' or 'screen'"}, status=400)

    try:
        sequence = int(request.POST.get("sequence", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "sequence must be an integer"}, status=400)

    is_last = request.POST.get("is_last") == "true"
    video_chunk = request.FILES.get("video_chunk")
    if video_chunk is None:
        return JsonResponse({"error": "video_chunk file is required"}, status=400)

    # Note: the *_recording_started events are logged explicitly by
    # capture_session.js at the moment getUserMedia/getDisplayMedia
    # resolves (see js/capture_session.js), which is a more accurate
    # "recording started" timestamp than "first chunk arrived here" would
    # be (chunk delivery lags behind stream acquisition by the encoder's
    # first-segment buffering time).
    chunk, _created = RecordingChunk.objects.update_or_create(
        session=session,
        stream_source=stream_source,
        sequence=sequence,
        defaults={"file": video_chunk, "is_last": is_last},
    )

    if is_last:
        ActivityEvent.objects.create(
            session=session, event_type=f"{stream_source}_recording_stopped"
        )
        finalize_recording(session, stream_source)

    return JsonResponse({"ok": True, "chunk_id": chunk.id})


@require_POST
def finish_session(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    session, error = get_session_or_error(payload)
    if error:
        return error
    request._study_session = session

    end_reason = payload.get("end_reason", "completed")
    valid_reasons = {choice for choice, _ in StudySession.END_REASON_CHOICES}
    if end_reason not in valid_reasons:
        end_reason = "completed"

    session.ended_at = timezone.now()
    session.end_reason = end_reason
    session.save(update_fields=["ended_at", "end_reason"])

    ActivityEvent.objects.create(
        session=session,
        event_type="session_ended",
        detail={"end_reason": end_reason},
        client_timestamp=parse_client_dt(payload.get("client_timestamp")),
    )
    finalize_all_recordings(session)

    return JsonResponse({"ok": True})
