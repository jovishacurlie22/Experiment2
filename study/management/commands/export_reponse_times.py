import csv

from django.core.management.base import BaseCommand

from study.models import QuestionResponse


class Command(BaseCommand):
    help = (
        "Exports each participant's question responses together with their "
        "response times (epoch-ms and seconds), as a single CSV."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", default="responses.csv")
        parser.add_argument("--participant", default=None)
        parser.add_argument("--session-key", default=None)

    def handle(self, *args, **options):
        output_path = options["output"]
        participant_code = options["participant"]
        session_key = options["session_key"]

        qs = QuestionResponse.objects.select_related(
            "session", "session__participant"
        ).order_by(
            "session__participant__participant_code",
            "session__session_key",
            "server_received_at",
        )
        if participant_code:
            qs = qs.filter(session__participant__participant_code=participant_code)
        if session_key:
            qs = qs.filter(session__session_key=session_key)

        if not qs.exists():
            self.stdout.write(self.style.WARNING("No QuestionResponse rows matched the given filters."))
            return

        rows_written = 0
        missing_timestamps = 0
        negative_durations = 0

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "participant_code", "session_key", "module_id", "section_id",
                "question_id", "answer_value", "effort_rating",
                "presented_at", "answered_at",
                "presented_epoch_ms", "answered_epoch_ms",
                "response_time_ms", "response_time_seconds", "flag",
            ])

            for r in qs.iterator():
                flag = ""
                # Prefer epoch_ms (authoritative, integer) when present;
                # fall back to the datetime fields for older rows recorded
                # before presented_epoch_ms/answered_epoch_ms existed.
                if r.presented_epoch_ms is not None and r.answered_epoch_ms is not None:
                    response_time_ms = r.answered_epoch_ms - r.presented_epoch_ms
                    response_time_seconds = response_time_ms / 1000
                elif r.presented_at is not None and r.answered_at is not None:
                    response_time_seconds = (r.answered_at - r.presented_at).total_seconds()
                    response_time_ms = round(response_time_seconds * 1000)
                else:
                    response_time_ms = ""
                    response_time_seconds = ""
                    missing_timestamps += 1
                    flag = "missing_timestamp"

                if response_time_seconds != "" and response_time_seconds < 0:
                    negative_durations += 1
                    flag = "negative_duration"

                writer.writerow([
                    r.session.participant.participant_code,
                    str(r.session.session_key),
                    r.module_id, r.section_id, r.question_id,
                    r.answer_value,
                    r.effort_rating if r.effort_rating is not None else "",
                    r.presented_at.isoformat() if r.presented_at else "",
                    r.answered_at.isoformat() if r.answered_at else "",
                    r.presented_epoch_ms if r.presented_epoch_ms is not None else "",
                    r.answered_epoch_ms if r.answered_epoch_ms is not None else "",
                    response_time_ms, response_time_seconds, flag,
                ])
                rows_written += 1

        self.stdout.write(self.style.SUCCESS(f"Wrote {rows_written} rows to {output_path}."))
        if missing_timestamps:
            self.stdout.write(self.style.WARNING(f"{missing_timestamps} row(s) missing a timestamp."))
        if negative_durations:
            self.stdout.write(self.style.WARNING(f"{negative_durations} row(s) had answered before presented."))