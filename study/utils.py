"""Helpers for assembling the final per-stream recording out of the
fragmented-MP4 chunks uploaded during a session.

Each chunk produced by capture_session.js is a fragmented MP4 segment
(the first chunk carries the ftyp+moov init segment, later chunks carry
moof+mdat movie fragments). Fragmented MP4 segments are designed to be
playable when simply concatenated byte-for-byte in order, so no
transcoding/ffmpeg dependency is required here — this just streams the
chunk files together in `sequence` order into one file.
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

    filename = f"{stream_source}.mp4"
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
