import json
import tempfile
from pathlib import Path
import os
import tempfile
from pathlib import Path

from django.conf import settings

import logging

logger = logging.getLogger(__name__)

from .recording_ingest import mux_and_lock_cfr
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from .models import ActivityEvent, Participant, QuestionResponse, Recording, RecordingChunk, StudySession
from .utils import finalize_all_recordings, finalize_recording
from .recording_ingest import mux_and_lock_cfr


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
def log_consent(request):
    """Fires the instant a participant clicks 'Agree and Continue' on the
    consent screen, which now comes after login -- session_key is always
    known by this point, so consent_given_at is written directly onto the
    StudySession created at login, and the consent_given ActivityEvent is
    tied to that same session."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    session, error = get_session_or_error(payload)
    if error:
        return error
    request._study_session = session

    consent_given_at = parse_client_dt(payload.get("consent_given_at")) or timezone.now()
    session.consent_given_at = consent_given_at
    session.save(update_fields=["consent_given_at"])

    ActivityEvent.objects.create(
        session=session,
        event_type="consent_given",
        client_timestamp=consent_given_at,
        detail={
            "ip_address": client_ip(request),
            "user_agent": request.META.get("HTTP_USER_AGENT", ""),
        },
    )
    return JsonResponse({"ok": True})


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


@require_POST
def upload_recording(request):
    """
    WebCodecs flow: the client now streams a raw AVC Annex-B elementary
    stream in small pieces during recording (chunk_index=0,1,2... with
    is_final=true on the last one) rather than uploading one huge file at
    session end -- this avoids single-request size/timeout limits on long
    sessions. Each chunk's raw bytes are appended to a per-session/
    per-stream partial file on disk; each chunk's sidecar frames are
    appended as one line to a partial JSONL file so we never have to hold
    a full session's frame list in memory. Only on the final chunk do we
    assemble stats and call mux_and_lock_cfr -- exactly as before, same
    avg_fps/target_fps handling, same ffmpeg call. Nothing about the fps
    locking changes; only *when* bytes arrive changes.
    """
    session_key = request.POST.get("session_key")
    session, error = get_session_or_error({"session_key": session_key})
    if error:
        return error
    request._study_session = session

    stream_source = request.POST.get("stream_source")
    if stream_source not in dict(RecordingChunk.STREAM_CHOICES):
        return JsonResponse({"error": "stream_source must be 'webcam' or 'screen'"}, status=400)

    # If this stream was already finalized (e.g. a retried final chunk
    # whose earlier response got lost in transit), don't reprocess --
    # just acknowledge so the client stops retrying.
    already_done = Recording.objects.filter(
        session=session, stream_source=stream_source
    ).exclude(finalized_at=None).exists()
    if already_done:
        return JsonResponse({"ok": True, "already_finalized": True})

    raw_file = request.FILES.get("video_raw")
    if raw_file is None:
        return JsonResponse({"error": "video_raw file is required"}, status=400)

    try:
        sidecar = json.loads(request.POST.get("sidecar_json", "{}"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid sidecar_json"}, status=400)

    try:
        chunk_index = int(request.POST.get("chunk_index", "0"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "chunk_index must be an integer"}, status=400)
    is_final = request.POST.get("is_final") == "true"

    session_dir = os.path.join(
        settings.MEDIA_ROOT, "recordings",
        session.participant.participant_code, str(session.session_key),
    )
    partial_dir = os.path.join(session_dir, "_partial")
    os.makedirs(partial_dir, exist_ok=True)

    raw_partial_path = Path(partial_dir) / f"{stream_source}.h264"
    sidecar_partial_path = Path(partial_dir) / f"{stream_source}.sidecar.jsonl"

    # Annex-B NAL units concatenate cleanly in order, so appending across
    # requests reconstructs exactly the same elementary stream a one-shot
    # upload would have produced.
    with open(raw_partial_path, "ab") as f:
        for piece in raw_file.chunks():
            f.write(piece)

    # Kept for the future frame-accurate mux pipeline (per-frame
    # timestamp_us/capture_perf_ms); today only frame_count/avg_fps below
    # feed mux_and_lock_cfr.
    with open(sidecar_partial_path, "a") as f:
        f.write(json.dumps({
            "chunk_index": chunk_index,
            "frame_count": sidecar.get("frame_count", 0),
            "frames": sidecar.get("frames", []),
        }) + "\n")

    if not is_final:
        return JsonResponse({"ok": True, "chunk_index": chunk_index})

    # Final chunk: assemble stats across every chunk line written so far,
    # then run the same mux/CFR-lock step the one-shot flow always used.
    total_frames = 0
    first_ts_us = None
    last_ts_us = None
    with open(sidecar_partial_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            frames = entry.get("frames", [])
            total_frames += entry.get("frame_count", len(frames))
            if frames:
                if first_ts_us is None:
                    first_ts_us = frames[0]["timestamp_us"]
                last_ts_us = frames[-1]["timestamp_us"]

    target_fps = sidecar.get("target_fps") or 30
    if first_ts_us is not None and last_ts_us is not None and last_ts_us > first_ts_us:
        duration_s = (last_ts_us - first_ts_us) / 1e6
        avg_fps = total_frames / duration_s if duration_s > 0 else target_fps
    else:
        avg_fps = sidecar.get("avg_fps") or target_fps

    output_path = os.path.join(session_dir, f"{stream_source}.mp4")

    try:
        mux_and_lock_cfr(raw_partial_path, avg_fps, target_fps, Path(output_path))
    except Exception as e:
        logger.error(
            "ffmpeg mux/CFR-lock failed for %s/%s: %s",
            session.session_key, stream_source, e,
        )
        return JsonResponse({"error": "video processing failed"}, status=500)

    recording, _ = Recording.objects.update_or_create(
        session=session,
        stream_source=stream_source,
        defaults={
            "file": os.path.relpath(output_path, settings.MEDIA_ROOT),
            "chunk_count": total_frames,
            "finalized_at": timezone.now(),
        },
    )

    # Clean up the partial files now that the mp4 is safely written.
    try:
        raw_partial_path.unlink(missing_ok=True)
        sidecar_partial_path.unlink(missing_ok=True)
    except OSError:
        pass

    return JsonResponse({"ok": True, "recording_id": recording.id})

@csrf_exempt
@require_POST
def log_activity_event(request):
    """
    Receives navigator.sendBeacon() payloads from capture_session.js
    (recording_start/recording_stop with precise epoch_ms). Beacon sends
    a Blob body, not multipart form data or a normal fetch — read raw
    request.body as JSON.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    session, error = get_session_or_error(payload)
    if error:
        return error

    ActivityEvent.objects.create(
        participant=session.participant,
        session=session,
        session_key=payload.get("session_key", ""),
        event_type=payload.get("event_type", "other"),
        epoch_ms=payload.get("epoch_ms"),
        stream_source=payload.get("stream_source"),
        meta=payload.get("meta") or {},
    )
    return JsonResponse({"ok": True})