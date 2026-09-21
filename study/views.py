import json
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
import os

from django.conf import settings
from django.db import connection

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
from .utils import finalize_all_recordings, finalize_recording, resolved_epoch_ms, to_epoch_ms
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

def to_epoch_ms(dt):
    """Convert an aware/naive datetime to Unix epoch milliseconds."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def resolved_epoch_ms(payload, client_dt=None):
    """Best available epoch_ms for an ActivityEvent. An explicit epoch_ms
    in the client payload (already unix ms, e.g. from Date.now()) wins,
    since it's captured at the moment the event actually happened on the
    client. Falls back to the parsed client_timestamp, then to server
    receipt time -- so epoch_ms is never left null. payload may be an
    empty dict for server-only events (e.g. ServerHitLoggingMiddleware)
    that have no client-sent data to prefer."""
    explicit = (payload or {}).get("epoch_ms")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    if client_dt is not None:
        return to_epoch_ms(client_dt)
    return to_epoch_ms(timezone.now())

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
    client_dt = parse_client_dt(payload.get("client_timestamp"))
    ActivityEvent.objects.create(
        session=session,
        event_type="session_started",
        client_timestamp=client_dt,
        epoch_ms=resolved_epoch_ms(payload, client_dt),
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
        epoch_ms=resolved_epoch_ms(payload, consent_given_at),
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

    client_dt = parse_client_dt(payload.get("client_timestamp"))
    ActivityEvent.objects.create(
        session=session,
        event_type=event_type,
        screen_name=payload.get("screen_name", "") or "",
        detail=payload.get("detail") or {},
        client_timestamp=client_dt,
        epoch_ms=resolved_epoch_ms(payload, client_dt),
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
        epoch_raw = request.POST.get("epoch_ms")
        try:
            epoch_ms_val = int(epoch_raw) if epoch_raw else to_epoch_ms(timezone.now())
        except (TypeError, ValueError):
            epoch_ms_val = to_epoch_ms(timezone.now())
        ActivityEvent.objects.create(
            session=session, event_type=f"{stream_source}_recording_stopped",
            epoch_ms=epoch_ms_val,
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

    client_dt = parse_client_dt(payload.get("client_timestamp"))
    ActivityEvent.objects.create(
        session=session,
        event_type="session_ended",
        detail={"end_reason": end_reason},
        client_timestamp=client_dt,
        epoch_ms=resolved_epoch_ms(payload, client_dt),
    )
    finalize_all_recordings(session)

    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Chunked recording upload helpers
# ---------------------------------------------------------------------------

# Keep the assembled raw H.264 stream (<stream>.raw.h264) next to the mp4.
# The mp4 is CFR-locked; the raw stream + <stream>.sidecar.jsonl (per-frame
# timestamps) are the untouched originals for time synchronisation.
KEEP_RAW_STREAMS = True

# Only one ffmpeg mux at a time per Gunicorn worker process, so a webcam and a
# screen finalize that land together don't fight over the VM's CPU.
_MUX_LOCK = threading.Lock()


def _session_dir(session):
    return os.path.join(
        settings.MEDIA_ROOT, "recordings",
        session.participant.participant_code, str(session.session_key),
    )


def _write_atomic(path, pieces, mode="wb"):
    """Write to a unique temp file, then os.replace() it into place. A retried
    or duplicated request therefore just overwrites the same chunk file with
    identical bytes -- it can never append twice or leave a half-written file."""
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, mode) as f:
        for piece in pieces:
            f.write(piece)
    os.replace(tmp, path)


def _chunk_indices(chunk_dir):
    """Sorted chunk indices that have a raw file on disk."""
    indices = set()
    if not os.path.isdir(chunk_dir):
        return []
    for name in os.listdir(chunk_dir):
        stem, ext = os.path.splitext(name)
        if ext == ".h264" and stem.isdigit():
            indices.add(int(stem))
    return sorted(indices)


def _read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _assemble_stream(chunk_dir, last_index, raw_out_path, sidecar_out_path):
    """Concatenate chunks 0..last_index (in index order) into raw_out_path and
    write every chunk's sidecar frames, in order, to sidecar_out_path (JSONL).
    Returns (total_frames, first_ts_us, last_ts_us, missing, received_chunks)."""
    total_frames = 0
    first_ts_us = None
    last_ts_us = None
    missing = []
    received = 0
    sidecar_tmp = f"{sidecar_out_path}.{uuid.uuid4().hex}.tmp"
    with open(raw_out_path, "wb") as out, open(sidecar_tmp, "w") as side_out:
        for idx in range(last_index + 1):
            raw_path = os.path.join(chunk_dir, f"{idx:06d}.h264")
            side_path = os.path.join(chunk_dir, f"{idx:06d}.json")
            if not os.path.exists(raw_path):
                missing.append(idx)
                continue
            received += 1
            with open(raw_path, "rb") as f:
                shutil.copyfileobj(f, out, 1024 * 1024)
            entry = _read_json(side_path)
            frames = entry.get("frames", [])
            total_frames += entry.get("frame_count", len(frames))
            if frames:
                if first_ts_us is None:
                    first_ts_us = frames[0]["timestamp_us"]
                last_ts_us = frames[-1]["timestamp_us"]
            side_out.write(json.dumps({
                "chunk_index": idx,
                "frame_count": entry.get("frame_count", len(frames)),
                "frames": frames,
            }) + "\n")
    os.replace(sidecar_tmp, sidecar_out_path)
    return total_frames, first_ts_us, last_ts_us, missing, received


def finalize_stream_from_chunks(session, stream_source):
    """Assemble a stream from its chunk files, verify completeness, mux it with
    ffmpeg and create the Recording row. Everything it needs is read from disk,
    so it works both for the background thread and for the
    `finalize_pending` management command. Raises on failure; the chunk files
    are left in place so nothing is lost."""
    session_dir = _session_dir(session)
    partial_dir = os.path.join(session_dir, "_partial")
    chunk_dir = os.path.join(partial_dir, stream_source)

    indices = _chunk_indices(chunk_dir)
    if not indices:
        raise RuntimeError(f"no chunk files found in {chunk_dir}")
    last_index = indices[-1]
    meta = _read_json(os.path.join(chunk_dir, f"{last_index:06d}.json"))
    saw_final = bool(meta.get("is_final"))

    raw_partial_path = Path(partial_dir) / f"{stream_source}.h264"
    sidecar_path = os.path.join(session_dir, f"{stream_source}.sidecar.jsonl")
    total_frames, first_ts_us, last_ts_us, missing, received = _assemble_stream(
        chunk_dir, last_index, raw_partial_path, sidecar_path
    )

    expected_chunks = meta.get("total_chunks")
    expected_frames = meta.get("total_frames")
    complete = (
        saw_final
        and not missing
        and (expected_chunks is None or expected_chunks == received)
        and (expected_frames is None or expected_frames == total_frames)
    )
    if not complete:
        logger.error(
            "INCOMPLETE recording %s/%s: final_seen=%s missing_chunks=%s "
            "chunks=%s/%s frames=%s/%s",
            session.session_key, stream_source, saw_final, missing,
            received, expected_chunks, total_frames, expected_frames,
        )

    target_fps = meta.get("target_fps") or 30
    if first_ts_us is not None and last_ts_us is not None and last_ts_us > first_ts_us:
        duration_s = (last_ts_us - first_ts_us) / 1e6
        avg_fps = total_frames / duration_s if duration_s > 0 else target_fps
    else:
        avg_fps = meta.get("avg_fps") or target_fps

    # Proof of completeness, kept next to the video. Written BEFORE the mux so
    # it exists even if ffmpeg fails.
    manifest = {
        "session_key": str(session.session_key),
        "stream_source": stream_source,
        "complete": complete,
        "final_chunk_seen": saw_final,
        "expected_chunks": expected_chunks,
        "received_chunks": received,
        "missing_chunks": missing,
        "expected_frames": expected_frames,
        "received_frames": total_frames,
        "target_fps": target_fps,
        "avg_fps": avg_fps,
        "assembled_at": timezone.now().isoformat(),
    }
    _write_atomic(
        os.path.join(session_dir, f"{stream_source}.manifest.json"),
        [json.dumps(manifest, indent=2)], "w",
    )

    output_path = os.path.join(session_dir, f"{stream_source}.mp4")
    with _MUX_LOCK:
        mux_and_lock_cfr(raw_partial_path, avg_fps, target_fps, Path(output_path))

    Recording.objects.update_or_create(
        session=session,
        stream_source=stream_source,
        defaults={
            "file": os.path.relpath(output_path, settings.MEDIA_ROOT),
            "chunk_count": total_frames,
            "finalized_at": timezone.now(),
        },
    )

    # The mp4, sidecar and manifest are safely on disk -- tidy up.
    try:
        if KEEP_RAW_STREAMS:
            os.replace(raw_partial_path, os.path.join(session_dir, f"{stream_source}.raw.h264"))
        else:
            raw_partial_path.unlink(missing_ok=True)
        shutil.rmtree(chunk_dir, ignore_errors=True)
    except OSError:
        logger.exception("Cleanup after finalize failed for %s/%s", session.session_key, stream_source)

    logger.info(
        "Finalized %s/%s: %s frames, complete=%s",
        session.session_key, stream_source, total_frames, complete,
    )
    return manifest


def _finalize_stream(session_pk, stream_source, lock_path):
    """Background-thread wrapper: runs AFTER the final chunk has been
    acknowledged to the browser, so a slow ffmpeg mux can no longer block a
    Gunicorn worker or trip nginx's upstream timeout."""
    try:
        session = StudySession.objects.select_related("participant").get(pk=session_pk)
        finalize_stream_from_chunks(session, stream_source)
    except Exception:
        # Chunk files are deliberately left on disk so nothing is lost;
        # `python manage.py finalize_pending` can re-run it.
        logger.exception(
            "Background finalize failed for session pk=%s stream=%s",
            session_pk, stream_source,
        )
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass
        connection.close()  # this thread's own DB connection


@require_POST
def upload_recording(request):
    """
    WebCodecs flow: the client streams a raw AVC Annex-B elementary stream in
    small pieces during recording (chunk_index=0,1,2... with is_final=true on
    the last one).

    Each chunk is stored as its own file (_partial/<stream>/<index>.h264 plus
    <index>.json for its sidecar), written atomically. That makes the endpoint
    idempotent: a chunk the browser re-sends after a timeout simply overwrites
    itself instead of being appended twice. The browser declares raw_bytes so
    a truncated upload is rejected (409) and re-sent.

    The final chunk only *claims* the stream (lock file) and starts a
    background thread that assembles the chunks, verifies them against the
    totals the browser sent, and runs the ffmpeg mux, then returns
    immediately. The heavy work never runs inside the request.
    """
    session_key = request.POST.get("session_key")
    session, error = get_session_or_error({"session_key": session_key})
    if error:
        return error
    request._study_session = session

    stream_source = request.POST.get("stream_source")
    if stream_source not in dict(RecordingChunk.STREAM_CHOICES):
        return JsonResponse({"error": "stream_source must be 'webcam' or 'screen'"}, status=400)

    # Already finalized (e.g. a retried final chunk whose earlier response was
    # lost): just acknowledge so the client stops retrying.
    already_done = Recording.objects.filter(
        session=session, stream_source=stream_source
    ).exclude(finalized_at=None).exists()
    if already_done:
        return JsonResponse({"ok": True, "already_finalized": True})

    # Finalization already running for this stream (a retried final chunk):
    # every chunk is already on disk, so just acknowledge and touch nothing.
    session_partial_dir = os.path.join(_session_dir(session), "_partial")
    if os.path.exists(os.path.join(session_partial_dir, f"{stream_source}.lock")):
        return JsonResponse({"ok": True, "processing": True})

    try:
        chunk_index = int(request.POST.get("chunk_index", "0"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "chunk_index must be an integer"}, status=400)
    if chunk_index < 0:
        return JsonResponse({"error": "chunk_index must be >= 0"}, status=400)
    is_final = request.POST.get("is_final") == "true"

    raw_file = request.FILES.get("video_raw")
    if raw_file is None and not is_final:
        return JsonResponse({"error": "video_raw file is required"}, status=400)

    # Truncated-upload check: the browser tells us how many bytes it sent.
    declared_raw = request.POST.get("raw_bytes")
    if declared_raw not in (None, ""):
        try:
            declared_raw_int = int(declared_raw)
        except (TypeError, ValueError):
            return JsonResponse({"error": "raw_bytes must be an integer"}, status=400)
        received_raw = raw_file.size if raw_file is not None else 0
        if received_raw != declared_raw_int:
            return JsonResponse(
                {"error": "size mismatch", "declared": declared_raw_int, "received": received_raw},
                status=409,
            )

    try:
        sidecar = json.loads(request.POST.get("sidecar_json", "{}"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid sidecar_json"}, status=400)

    session_dir = _session_dir(session)
    partial_dir = os.path.join(session_dir, "_partial")
    chunk_dir = os.path.join(partial_dir, stream_source)
    os.makedirs(chunk_dir, exist_ok=True)

    # The final chunk is often 0 bytes (just the encoder tail + is_final flag).
    # The raw file is written first and the sidecar second, both atomically,
    # so a chunk is only acknowledged once both are safely on disk.
    raw_pieces = raw_file.chunks() if raw_file is not None else []
    _write_atomic(os.path.join(chunk_dir, f"{chunk_index:06d}.h264"), raw_pieces, "wb")
    entry = dict(sidecar)
    entry["chunk_index"] = chunk_index
    entry["is_final"] = is_final
    entry.setdefault("frames", [])
    entry.setdefault("frame_count", len(entry["frames"]))
    _write_atomic(
        os.path.join(chunk_dir, f"{chunk_index:06d}.json"),
        [json.dumps(entry)], "w",
    )

    if not is_final:
        return JsonResponse({"ok": True, "chunk_index": chunk_index})

    # Final chunk: claim the stream so a retried final can't start a second
    # mux, then hand the heavy work to a background thread and return now.
    lock_path = os.path.join(partial_dir, f"{stream_source}.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return JsonResponse({"ok": True, "chunk_index": chunk_index, "processing": True})

    try:
        # Non-daemon: on a graceful Gunicorn restart the interpreter waits for
        # a running mux instead of killing it mid-write.
        threading.Thread(
            target=_finalize_stream,
            args=(session.pk, stream_source, lock_path),
            daemon=False,
        ).start()
    except Exception:
        os.remove(lock_path)
        logger.exception("Could not start finalize thread for %s/%s", session.session_key, stream_source)
        return JsonResponse({"error": "could not start video processing"}, status=500)

    return JsonResponse({"ok": True, "chunk_index": chunk_index, "processing": True})


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
        epoch_ms=resolved_epoch_ms(payload),
        stream_source=payload.get("stream_source"),
        meta=payload.get("meta") or {},
    )
    return JsonResponse({"ok": True})