#!/usr/bin/env python3
"""
Frame-accurate mux patch for Experiment2.
 
Lives in timesynch/ (a sibling of study/, gaze_study/, etc. at the repo
root). Run it from inside timesynch/ on the VM:
 
    cd ~/Experiment2/timesynch
    python3 apply_frame_mux_patch.py
 
Before running, make sure `av` is installed:
 
    pip install av
    python -c "import av; print(av.__version__)"
 
This script patches three files using exact-match old_str/new_str replacement
(same pattern used throughout this session), printing SUCCESS/ERROR for each
step and aborting on the first failure so partial patches don't get applied.
 
PTS decision: uses sidecar frame['timestamp_us'] (WebCodecs frame.timestamp,
device-native capture time) rather than capture_perf_ms (performance.now()),
since the latter includes JS-loop/queueing jitter that frame-accurate sync
is meant to eliminate.
 
After this script succeeds, you still need to:
    python manage.py makemigrations study -n add_recording_sidecar_file
    python manage.py migrate study
    python manage.py collectstatic --noinput   # no JS changed here, but harmless
    git add -A && git commit -m "Add frame-accurate mux via PyAV + persist sidecar"
    git push
"""
import sys
from pathlib import Path
 
REPO_ROOT = Path(__file__).resolve().parent.parent
 
 
def patch_file(path: Path, old: str, new: str, label: str):
    if not path.exists():
        print(f"ERROR: {label}: file not found: {path}")
        sys.exit(1)
    content = path.read_text()
    count = content.count(old)
    if count == 0:
        print(f"ERROR: {label}: old_str not found in {path}")
        print("---- old_str was ----")
        print(old)
        sys.exit(1)
    if count > 1:
        print(f"ERROR: {label}: old_str is not unique in {path} (found {count} times)")
        sys.exit(1)
    content = content.replace(old, new)
    path.write_text(content)
    print(f"SUCCESS: {label}")
 
 
# ---------------------------------------------------------------------------
# PATCH 1: study/recording_ingest.py — add mux_frame_accurate()
# ---------------------------------------------------------------------------
 
ingest_path = REPO_ROOT / "study" / "recording_ingest.py"
 
ingest_old = '''# study/recording_ingest.py
import json
import tempfile
from pathlib import Path
 
import ffmpeg
from django.conf import settings
 
from .models import Recording, StudySession'''
 
ingest_new = '''# study/recording_ingest.py
import json
import tempfile
from fractions import Fraction
from pathlib import Path
 
import av
import ffmpeg
from django.conf import settings
 
from .models import Recording, StudySession'''
 
patch_file(ingest_path, ingest_old, ingest_new, "recording_ingest.py imports")
 
ingest_fn_anchor = '''        .overwrite_output()
        .run(quiet=True)
    )'''
 
ingest_fn_new = '''        .overwrite_output()
        .run(quiet=True)
    )
 
 
def mux_frame_accurate(raw_path: Path, frames: list, out_path: Path):
    """
    Remux a raw AVC Annex-B elementary stream into mp4, stream-copy
    (no re-encode), assigning each packet's PTS from the real per-frame
    capture timestamp recorded in the client sidecar
    (frame['timestamp_us'] -- WebCodecs' `frame.timestamp`, device/track
    -native capture time), instead of ffmpeg's synthetic uniform-spacing
    `-r avg_fps` assumption used by mux_and_lock_cfr.
 
    `frames` is the sidecar's `frames` array as-is: a list of dicts with
    at least `timestamp_us` per entry, in capture order.
 
    Raises ValueError if the raw stream's packet count doesn't match the
    sidecar frame count -- silently misaligning frame N with the wrong
    timestamp is worse than failing loudly here.
    """
    input_container = av.open(str(raw_path), mode="r")
    in_stream = input_container.streams.video[0]
 
    output_container = av.open(str(out_path), mode="w")
    out_stream = output_container.add_stream(template=in_stream)
 
    # Sidecar timestamps are microseconds (frame.timestamp), so a
    # 1/1_000_000 time_base gives an exact, lossless PTS conversion.
    time_base = Fraction(1, 1_000_000)
    out_stream.time_base = time_base
 
    packets = [p for p in input_container.demux(in_stream) if p.size > 0]
 
    if len(packets) != len(frames):
        input_container.close()
        output_container.close()
        raise ValueError(
            f"raw packet count ({len(packets)}) != sidecar frame count "
            f"({len(frames)}) -- refusing to mux, possible frame "
            f"misalignment"
        )
 
    for packet, frame_meta in zip(packets, frames):
        pts = int(frame_meta["timestamp_us"])
        packet.pts = pts
        packet.dts = pts
        packet.time_base = time_base
        packet.stream = out_stream
        output_container.mux(packet)
 
    output_container.close()
    input_container.close()'''
 
patch_file(ingest_path, ingest_fn_anchor, ingest_fn_new, "recording_ingest.py add mux_frame_accurate()")
 
 
# ---------------------------------------------------------------------------
# PATCH 2: study/models.py — add sidecar_file field + upload_to fn on Recording
# ---------------------------------------------------------------------------
 
models_path = REPO_ROOT / "study" / "models.py"
 
models_old = '''    stream_source = models.CharField(max_length=10, choices=RecordingChunk.STREAM_CHOICES)
    file = models.FileField(upload_to=recording_path, blank=True)
    chunk_count = models.PositiveIntegerField(default=0)'''
 
models_new = '''    stream_source = models.CharField(max_length=10, choices=RecordingChunk.STREAM_CHOICES)
    file = models.FileField(upload_to=recording_path, blank=True)
    sidecar_file = models.FileField(upload_to="recording_sidecar_path", blank=True, null=True)
    chunk_count = models.PositiveIntegerField(default=0)'''
 
# NOTE: upload_to must be a real function reference, not a string -- see the
# fixup applied right below. Written as a string first so this patch step
# doesn't fail if recording_sidecar_path isn't defined yet; the next patch
# defines it, and the one after that fixes the reference.
patch_file(models_path, models_old, models_new, "models.py add sidecar_file field (placeholder upload_to)")
 
models_fn_anchor = '''class Recording(models.Model):'''
 
models_fn_new = '''def recording_sidecar_path(instance, filename):
    """upload_to for Recording.sidecar_file -- mirrors recording_path's
    layout convention (check recording_path's actual definition via
    `grep -n "def recording_path" study/models.py` if this diverges)."""
    return (
        f"recordings/{instance.session.participant.participant_code}/"
        f"{instance.session.session_key}/{instance.stream_source}_sidecar.json"
    )
 
 
class Recording(models.Model):'''
 
patch_file(models_path, models_fn_anchor, models_fn_new, "models.py add recording_sidecar_path()")
 
models_fixref_old = '''    sidecar_file = models.FileField(upload_to="recording_sidecar_path", blank=True, null=True)'''
models_fixref_new = '''    sidecar_file = models.FileField(upload_to=recording_sidecar_path, blank=True, null=True)'''
 
patch_file(models_path, models_fixref_old, models_fixref_new, "models.py fix sidecar_file upload_to reference")
 
 
# ---------------------------------------------------------------------------
# PATCH 3: study/views.py — wire mux_frame_accurate() + persist sidecar file
# ---------------------------------------------------------------------------
 
views_path = REPO_ROOT / "study" / "views.py"
 
# NOTE: this assumes the existing import line is exactly
#   from .recording_ingest import mux_and_lock_cfr
# If views.py imports it differently (e.g. combined with other names or a
# different alias), this step will ERROR with old_str-not-found -- grep
# `from .recording_ingest import` first and adjust before rerunning.
views_import_old = '''from .recording_ingest import mux_and_lock_cfr'''
views_import_new = '''from .recording_ingest import mux_and_lock_cfr, mux_frame_accurate'''
 
patch_file(views_path, views_import_old, views_import_new, "views.py import mux_frame_accurate")
 
views_body_old = '''        avg_fps = sidecar.get("avg_fps") or sidecar.get("target_fps") or 30
        target_fps = sidecar.get("target_fps") or 30
 
        try:
            mux_and_lock_cfr(raw_path, avg_fps, target_fps, Path(output_path))
        except Exception as e:
            logger.error(
                "ffmpeg mux/CFR-lock failed for %s/%s: %s",
                session.session_key, stream_source, e,
            )
            return JsonResponse({"error": "video processing failed"}, status=500)'''
 
views_body_new = '''        frames = sidecar.get("frames") or []
 
        try:
            mux_frame_accurate(raw_path, frames, Path(output_path))
        except Exception as e:
            logger.error(
                "frame-accurate mux failed for %s/%s: %s",
                session.session_key, stream_source, e,
            )
            return JsonResponse({"error": "video processing failed"}, status=500)
 
        sidecar_path = os.path.join(output_dir, f"{stream_source}_sidecar.json")
        with open(sidecar_path, "w") as f:
            json.dump(sidecar, f)'''
 
patch_file(views_path, views_body_old, views_body_new, "views.py use mux_frame_accurate + write sidecar")
 
views_save_old = '''        defaults={
            "file": os.path.relpath(output_path, settings.MEDIA_ROOT),
            "chunk_count": sidecar.get("frame_count", 0),
            "finalized_at": timezone.now(),
        },'''
 
views_save_new = '''        defaults={
            "file": os.path.relpath(output_path, settings.MEDIA_ROOT),
            "sidecar_file": os.path.relpath(sidecar_path, settings.MEDIA_ROOT),
            "chunk_count": sidecar.get("frame_count", 0),
            "finalized_at": timezone.now(),
        },'''
 
patch_file(views_path, views_save_old, views_save_new, "views.py save sidecar_file on Recording")
 
print()
print("All patches applied. Next steps:")
print("  python manage.py makemigrations study -n add_recording_sidecar_file")
print("  python manage.py migrate study")
print("  python manage.py check")
print("  git diff   # review before committing")
 