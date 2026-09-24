"""
Exports each participant's question answers and PaaS mental-effort
ratings -- no timestamps at all, just what was answered and how
effortful it felt.

Usage (from the project root, same place you run manage.py):
    python manage.py export_paas_ratings
    python manage.py export_paas_ratings --outdir websitelogs
    python manage.py export_paas_ratings --participant 7
    python manage.py export_paas_ratings --participant 7 --participant 12

Output:
    websitelogs/<participant_code>-paas.csv   (one file per participant)

One row per QuestionResponse -- the single source of both the final
answer_value and the effort_rating (PaaS mental-effort scale) for each
question. ActivityEvent isn't touched here: question_answered revisions,
screen_shown, consent, and recording events carry no PaaS rating, so
they add nothing this export needs (see export_activity_logs.py for the
full activity timeline, timestamps included).

Rows are still ordered by session start then insertion order, purely so
each file reads in the order questions were actually answered -- no time
value itself appears in the output.

Columns:
    participant_code, session_key, module_id, section_id, question_id,
    answer_value, effort_rating
"""

import csv
import os

from django.core.management.base import BaseCommand, CommandError

from study.models import Participant, QuestionResponse

CSV_COLUMNS = [
    "participant_code",
    "session_key",
    "module_id",
    "section_id",
    "question_id",
    "answer_value",
    "effort_rating",
]


class Command(BaseCommand):
    help = (
        "Export each participant's question answers + PaaS effort ratings "
        "(no timestamps) into websitelogs/<id>-paas.csv"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--outdir",
            default="websitelogs",
            help="Output folder (created if missing). Default: websitelogs",
        )
        parser.add_argument(
            "--participant",
            action="append",
            default=None,
            help="Limit export to this participant_code. Repeatable. "
                 "Default: every participant with at least one response.",
        )

    def handle(self, *args, **options):
        outdir = options["outdir"]
        wanted_codes = options["participant"]

        os.makedirs(outdir, exist_ok=True)

        participants = Participant.objects.all().order_by("participant_code")
        if wanted_codes:
            participants = participants.filter(participant_code__in=wanted_codes)
            found_codes = set(participants.values_list("participant_code", flat=True))
            missing = set(wanted_codes) - found_codes
            if missing:
                raise CommandError(f"Unknown participant_code(s): {', '.join(sorted(missing))}")

        total_files = 0
        total_rows = 0

        for participant in participants:
            responses = (
                QuestionResponse.objects
                .filter(session__participant=participant)
                .select_related("session")
                .order_by("session__started_at", "id")
            )
            row_count = responses.count()

            if row_count == 0 and not wanted_codes:
                continue

            filename = f"{participant.participant_code}-paas.csv"
            filepath = os.path.join(outdir, filename)

            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for r in responses:
                    writer.writerow({
                        "participant_code": participant.participant_code,
                        "session_key": str(r.session.session_key),
                        "module_id": r.module_id,
                        "section_id": r.section_id,
                        "question_id": r.question_id,
                        "answer_value": r.answer_value,
                        "effort_rating": r.effort_rating if r.effort_rating is not None else "",
                    })

            total_files += 1
            total_rows += row_count
            self.stdout.write(self.style.SUCCESS(f"  {filepath}  ({row_count} rows)"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {total_files} file(s), {total_rows} row(s) written to '{outdir}/'"
            )
        )