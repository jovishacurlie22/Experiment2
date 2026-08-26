# study/recording_ingest.py
import json
import tempfile
from pathlib import Path

import ffmpeg
from django.conf import settings

from .models import Recording, StudySession


def mux_and_lock_cfr(raw_path: Path, avg_fps: float, target_fps: int, out_path: Path):
    """
    Mux a raw AVC Annex-B elementary stream into mp4 and force a constant
    frame rate. avg_fps (from the client sidecar) is used only as ffmpeg's
    *input* timing hint, since a raw Annex-B stream carries no PTS of its
    own — the CFR lock on output is what actually guarantees frame N ==
    N / target_fps.
    """
    (
        ffmpeg
        .input(str(raw_path), r=avg_fps, f='h264')
        .output(
            str(out_path),
            vf=f'fps=fps={target_fps}:round=near',
            vsync='cfr',
            vcodec='libx264',
            pix_fmt='yuv420p',
        )
        .overwrite_output()
        .run(quiet=True)
    )