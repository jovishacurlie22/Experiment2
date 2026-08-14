# study/management/commands/export_activity_csv.py
import csv
from django.core.management.base import BaseCommand
from study.models import ActivityEvent, Participant

class Command(BaseCommand):
    help = "Export per-participant activity log as CSV with video-relative offsets"

    def add_arguments(self, parser):
        parser.add_argument("participant_id", type=int)
        parser.add_argument("--out", default=None)

    def handle(self, *args, **opts):
        participant = Participant.objects.get(id=opts["participant_id"])
        events = list(
            ActivityEvent.objects.filter(participant=participant).order_by("epoch_ms")
        )
        if not events:
            self.stdout.write("No events found.")
            return

        # recording_start epoch per stream, so offsets are per-stream accurate
        rec_starts = {
            e.stream_source: e.epoch_ms
            for e in events
            if e.event_type == "recording_start" and e.stream_source
        }

        out_path = opts["out"] or f"activity_participant_{participant.id}.csv"
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "session_key", "event_type", "stream_source", "epoch_ms", "iso_time",
                "offset_webcam_ms", "offset_webcam_hhmmss",
                "offset_screen_ms", "offset_screen_hhmmss",
                "meta",
            ])
            for e in events:
                row = [e.session_key, e.event_type, e.stream_source or "", e.epoch_ms,
                       self._iso(e.epoch_ms)]
                for stream in ("webcam", "screen"):
                    start = rec_starts.get(stream)
                    if start is not None and e.epoch_ms >= start:
                        offset_ms = e.epoch_ms - start
                        row += [offset_ms, self._hhmmss(offset_ms)]
                    else:
                        row += ["", ""]
                row.append(e.meta)
                writer.writerow(row)

        self.stdout.write(self.style.SUCCESS(f"Wrote {out_path}"))

    @staticmethod
    def _iso(epoch_ms):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()

    @staticmethod
    def _hhmmss(offset_ms):
        s = offset_ms // 1000
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"