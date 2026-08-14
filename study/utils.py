"""Helpers for assembling the final per-stream recording out of the WebM
chunks uploaded during a session.

Each chunk produced by capture_session.js is a MediaRecorder Blob for a
CHUNK_TIMESLICE_MS window of that stream. Simple concatenation of
MediaRecorder's sequential Blobs in order reproduces a valid, playable WebM
file (this is the standard "streaming MediaRecorder to disk" technique), so
no transcoding/ffmpeg dependency is required here — this just streams the
chunk files together in `sequence` order into one file.

Frame-level timestamps for downstream analysis are extracted from the
finalized .webm afterward via ffprobe (pts_time per frame) rather than
tracked during upload — see frame_align.py from Experiment 1 for the same
technique.
"""

from django.core.files.base import ContentFile
from django.utils import timezone

from .models import Recording, RecordingChunk


def finalize_recording(session, stream_source):
    """Concatenate all uploaded chunks for (session, stream_source) into a
    single Recording.file, in ascending sequence order. Safe to call more
    than once (e.g. if the "is_last" chunk race-condition means it's
    called from both the last chunk upload and session finish) — it just
    re-does the concatenation from whatever chunks exist so far.

    Returns the Recording instance, or None if no chunks exist yet.
    """
    chunks = list(
        RecordingChunk.objects.filter(session=session, stream_source=stream_source).order_by(
            "sequence"
        )
    )
    if not chunks:
        return None

    recording, _ = Recording.objects.get_or_create(session=session, stream_source=stream_source)
    if recording.started_at is None:
        recording.started_at = chunks[0].uploaded_at

    buf = bytearray()
    for chunk in chunks:
        chunk.file.open("rb")
        try:
            buf.extend(chunk.file.read())
        finally:
            chunk.file.close()

    filename = f"{stream_source}.webm"
    recording.file.save(filename, ContentFile(bytes(buf)), save=False)
    recording.chunk_count = len(chunks)
    recording.finalized_at = timezone.now()
    recording.save()
    return recording


def finalize_all_recordings(session):
    """Finalize both streams for a session (called when the session ends)."""
    results = {}
    for stream_source, _ in RecordingChunk.STREAM_CHOICES:
        results[stream_source] = finalize_recording(session, stream_source)
    return results
