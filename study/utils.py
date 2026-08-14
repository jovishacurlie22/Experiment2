import os
import subprocess
import logging

logger = logging.getLogger(__name__)


def finalize_recording(chunk_paths: list[str], output_path: str) -> str:
    """
    Concatenate sequential .webm chunks into a single file, then remux
    through ffmpeg to fix Duration/Cues metadata (Chrome's MediaRecorder
    doesn't know total duration up front, so raw concatenation leaves
    Segment > Info > Duration unset/Infinity and no seek table).
    """
    raw_concat_path = output_path + ".raw.webm"

    # Step 1: sequential byte concatenation (unchanged from before)
    with open(raw_concat_path, "wb") as outfile:
        for chunk_path in chunk_paths:
            with open(chunk_path, "rb") as infile:
                outfile.write(infile.read())

    # Step 2: remux through ffmpeg to recalculate Duration/Cues from
    # actual cluster timestamps
    try:
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
    except subprocess.CalledProcessError as e:
        logger.error(
            "ffmpeg remux failed for %s: %s",
            raw_concat_path,
            e.stderr.decode(errors="replace"),
        )
        # Fall back to the raw concat so we don't lose the recording
        # entirely, even though duration/seek will be broken.
        os.replace(raw_concat_path, output_path)
        return output_path
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg remux timed out for %s", raw_concat_path)
        os.replace(raw_concat_path, output_path)
        return output_path

    # Step 3: clean up the intermediate raw concat file
    os.remove(raw_concat_path)

    return output_path