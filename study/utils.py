import os
import subprocess
import logging
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _remux_webm(raw_concat_path: str, output_path: str) -> None:
    """Remux a concatenated WebM through ffmpeg to fix Duration/Cues metadata
    (Chrome's MediaRecorder doesn't know total duration up front, so raw
    concatenation leaves Segment > Info > Duration unset and no seek table)."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", raw_concat_path,
            "-c", "copy",
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero",
            output_path,
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )


def finalize_recording(session, stream_source: str):
    """
    Concatenate all uploaded RecordingChunk files for this session+stream,
    in sequence order, into a single Recording, remuxed through ffmpeg to
    fix Duration/Cues. Idempotent — if this stream is already finalized,
    it's a no-op, so it's safe to call both from upload_chunk (on is_last)
    and again from finalize_all_recordings (on End Study) without double-work.
    """
    from .models import Recording, RecordingChunk

    recording, _ = Recording.objects.get_or_create(
        session=session, stream_source=stream_source
    )
    if recording.finalized_at:
        logger.info(
            "finalize_recording: %s/%s already finalized, skipping",
            session.session_key, stream_source,
        )
        return recording

    chunks = list(
        RecordingChunk.objects.filter(session=session, stream_source=stream_source)
        .order_by("sequence")
    )
    if not chunks:
        logger.warning(
            "finalize_recording: no chunks found for %s/%s",
            session.session_key, stream_source,
        )
        return recording

    chunk_paths = [c.file.path for c in chunks]

    output_dir = os.path.join(
        settings.MEDIA_ROOT, "recordings",
        session.participant.participant_code, session.session_key,
    )
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{stream_source}.webm")
    raw_concat_path = output_path + ".raw.webm"

    with open(raw_concat_path, "wb") as outfile:
        for chunk_path in chunk_paths:
            with open(chunk_path, "rb") as infile:
                outfile.write(infile.read())

    try:
        _remux_webm(raw_concat_path, output_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.error(
            "ffmpeg remux failed for %s/%s: %s — falling back to raw concat",
            session.session_key, stream_source, e,
        )
        os.replace(raw_concat_path, output_path)
    else:
        os.remove(raw_concat_path)

    recording.file.name = os.path.relpath(output_path, settings.MEDIA_ROOT)
    recording.chunk_count = len(chunks)
    recording.finalized_at = timezone.now()
    recording.save(update_fields=["file", "chunk_count", "finalized_at"])

    return recording


def finalize_all_recordings(session):
    """
    Called from finish_session (End Study) to guarantee both webcam and
    screen recordings are finalized even if a stream's final chunk
    (is_last=True) never arrived — participant closed the tab, network
    dropped the last upload, or End Study was clicked mid-upload.
    """
    from .models import RecordingChunk

    stream_sources = (
        RecordingChunk.objects.filter(session=session)
        .values_list("stream_source", flat=True)
        .distinct()
    )
    return {s: finalize_recording(session, s) for s in stream_sources}