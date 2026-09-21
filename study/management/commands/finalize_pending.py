"""
Re-run finalization for any recording whose chunks are on disk but that was
never turned into an mp4 (e.g. Gunicorn was restarted mid-mux, or ffmpeg
failed). Safe to run repeatedly; already-finalized streams are skipped.

    python manage.py finalize_pending --dry-run
    python manage.py finalize_pending

Run it while no participant is finishing a session (it clears any stale
<stream>.lock file left behind by an interrupted mux).
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from study.models import Recording, StudySession
from study.views import _chunk_indices, finalize_stream_from_chunks


class Command(BaseCommand):
    help = "Finalize recordings whose chunk files are still in _partial/."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be finalized, do nothing.")

    def handle(self, *args, **options):
        root = os.path.join(settings.MEDIA_ROOT, "recordings")
        if not os.path.isdir(root):
            self.stdout.write("No recordings directory yet.")
            return

        found = 0
        for participant_code in sorted(os.listdir(root)):
            participant_dir = os.path.join(root, participant_code)
            if not os.path.isdir(participant_dir):
                continue
            for session_key in sorted(os.listdir(participant_dir)):
                partial_dir = os.path.join(participant_dir, session_key, "_partial")
                for stream in ("webcam", "screen"):
                    chunk_dir = os.path.join(partial_dir, stream)
                    if not _chunk_indices(chunk_dir):
                        continue
                    label = f"participant {participant_code} / session {session_key} / {stream}"

                    try:
                        session = StudySession.objects.select_related("participant").get(session_key=session_key)
                    except (StudySession.DoesNotExist, ValueError, TypeError):
                        self.stderr.write(f"SKIP {label}: no matching StudySession")
                        continue

                    if Recording.objects.filter(session=session, stream_source=stream).exclude(finalized_at=None).exists():
                        self.stdout.write(f"skip {label}: already finalized (leftover chunk files)")
                        continue

                    found += 1
                    if options["dry_run"]:
                        self.stdout.write(f"would finalize {label}")
                        continue

                    lock_path = os.path.join(partial_dir, f"{stream}.lock")
                    if os.path.exists(lock_path):
                        os.remove(lock_path)
                    try:
                        manifest = finalize_stream_from_chunks(session, stream)
                        self.stdout.write(self.style.SUCCESS(
                            f"finalized {label}: {manifest['received_frames']} frames, complete={manifest['complete']}"
                        ))
                    except Exception as exc:
                        self.stderr.write(self.style.ERROR(f"FAILED {label}: {exc}"))

        if found == 0:
            self.stdout.write("Nothing pending.")