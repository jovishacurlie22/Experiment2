"""
Exports every activity for each participant into a single, merged,
chronologically-sorted CSV: screens shown, question answer revisions,
final question submissions (with effort rating + presented/answered
times), consent, recording start/stop, session start/end, fullscreen
changes, server hits -- everything logged for that participant.

Usage (from the project root, same place you run manage.py):
    python manage.py export_activity_logs
    python manage.py export_activity_logs --outdir websitelogs
    python manage.py export_activity_logs --participant 7
    python manage.py export_activity_logs --participant 7 --participant 12

Output:
    websitelogs/<participant_code>-logs.csv   (one file per participant)

Row sources, merged and sorted by unix_ms:
    1. Every ActivityEvent row (session_started, consent_given,
       screen_shown, question_answered [revision], fullscreen_entered/
       exited, recording_start/stop, webcam_recording_stopped,
       screen_recording_stopped, session_ended, server_hit, other...)
    2. Two synthetic rows per QuestionResponse:
         - "question_presented"      @ presented_epoch_ms
         - "question_answered_final" @ answered_epoch_ms
                                        (carries answer_value + effort_rating)

Columns:
    unix_ms, unix_us, resolution_ms, unix_s, datetime_utc, source,
    event_type, session_key, screen_name, module_id, section_id,
    question_id, answer_value, effort_rating, stream_source,
    request_path, client_timestamp, server_timestamp, detail

Timestamp honesty (see project notes on ms/us sync):
    Every row in THIS file is timestamped via the browser's Date.now(),
    which is a real, absolute unix timestamp -- but its actual clock
    granularity is ~1ms, not better. unix_us here is just unix_ms * 1000
    (a unit conversion, not added precision), and resolution_ms=1 says
    so explicitly. When you later merge this with the webcam/screen/gaze
    manifest (whose rows come from anchored high-resolution counters),
    that file's rows should carry a resolution_ms well under 1 -- don't
    treat the two as equally precise just because both end up as
    "unix_us" columns.
"""

import csv
import datetime as dt
import json
import os

from django.core.management.base import BaseCommand, CommandError

from study.models import ActivityEvent, Participant, QuestionResponse

# Every timestamp in this export originates from the browser's Date.now(),
# a real absolute unix clock but with ~1ms granularity. Recorded explicitly
# per-row so downstream merges never mistake unit conversion for precision.
CLIENT_CLOCK_RESOLUTION_MS = 1

CSV_COLUMNS = [
    "unix_ms",
    "unix_us",
    "resolution_ms",
    "unix_s",
    "datetime_utc",
    "source",
    "event_type",
    "session_key",
    "screen_name",
    "module_id",
    "section_id",
    "question_id",
    "answer_value",
    "effort_rating",
    "stream_source",
    "request_path",
    "client_timestamp",
    "server_timestamp",
    "detail",
]


def _timestamps(epoch_ms):
    """Given an integer unix_ms, return (unix_ms, unix_us, unix_s, iso_utc).
    unix_us is a lossless *unit* conversion (ms * 1000) -- it does not
    imply the underlying clock measured anything finer than a millisecond."""
    if epoch_ms is None:
        return "", "", "", ""
    unix_us = epoch_ms * 1000
    unix_s = epoch_ms / 1000
    datetime_utc = dt.datetime.fromtimestamp(unix_s, tz=dt.timezone.utc).isoformat()
    return epoch_ms, unix_us, unix_s, datetime_utc


def _row(**kwargs):
    """Build a full-width row dict, defaulting any omitted column to ''."""
    row = {col: "" for col in CSV_COLUMNS}
    row.update(kwargs)
    return row


def rows_for_activity_event(event):
    unix_ms, unix_us, unix_s, datetime_utc = _timestamps(event.epoch_ms)
    session_key = event.session_key or (
        str(event.session.session_key) if event.session_id else ""
    )
    detail = event.detail or event.meta or {}
    return [_row(
        unix_ms=unix_ms,
        unix_us=unix_us,
        resolution_ms=CLIENT_CLOCK_RESOLUTION_MS if unix_ms != "" else "",
        unix_s=unix_s,
        datetime_utc=datetime_utc,
        source="activity_event",
        event_type=event.event_type,
        session_key=session_key,
        screen_name=event.screen_name,
        module_id=(detail or {}).get("moduleId", ""),
        section_id=(detail or {}).get("sectionId", ""),
        question_id=(detail or {}).get("questionId", ""),
        answer_value=json.dumps((detail or {}).get("value"))
            if isinstance(detail, dict) and "value" in detail else "",
        stream_source=event.stream_source or "",
        request_path=event.request_path,
        client_timestamp=event.client_timestamp.isoformat() if event.client_timestamp else "",
        server_timestamp=event.server_timestamp.isoformat() if event.server_timestamp else "",
        detail=json.dumps(detail, ensure_ascii=False) if detail else "",
    )]


def rows_for_question_response(response):
    session_key = str(response.session.session_key)
    out = []

    if response.presented_epoch_ms is not None:
        unix_ms, unix_us, unix_s, datetime_utc = _timestamps(response.presented_epoch_ms)
        out.append(_row(
            unix_ms=unix_ms,
            unix_us=unix_us,
            resolution_ms=CLIENT_CLOCK_RESOLUTION_MS,
            unix_s=unix_s,
            datetime_utc=datetime_utc,
            source="question_response",
            event_type="question_presented",
            session_key=session_key,
            screen_name="question",
            module_id=response.module_id,
            section_id=response.section_id,
            question_id=response.question_id,
            client_timestamp=response.presented_at.isoformat() if response.presented_at else "",
        ))

    if response.answered_epoch_ms is not None:
        unix_ms, unix_us, unix_s, datetime_utc = _timestamps(response.answered_epoch_ms)
        out.append(_row(
            unix_ms=unix_ms,
            unix_us=unix_us,
            resolution_ms=CLIENT_CLOCK_RESOLUTION_MS,
            unix_s=unix_s,
            datetime_utc=datetime_utc,
            source="question_response",
            event_type="question_answered_final",
            session_key=session_key,
            screen_name="question",
            module_id=response.module_id,
            section_id=response.section_id,
            question_id=response.question_id,
            answer_value=response.answer_value,
            effort_rating=response.effort_rating
                if response.effort_rating is not None else "",
            client_timestamp=response.answered_at.isoformat() if response.answered_at else "",
            server_timestamp=response.server_received_at.isoformat()
                if response.server_received_at else "",
        ))

    return out


class Command(BaseCommand):
    help = (
        "Export a merged, chronological activity log per participant "
        "(ActivityEvent + QuestionResponse) into websitelogs/<id>-logs.csv"
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
                 "Default: every participant with at least one row.",
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
            events = ActivityEvent.objects.filter(participant=participant)
            responses = QuestionResponse.objects.filter(
                session__participant=participant
            ).select_related("session")

            rows = []
            for event in events:
                rows.extend(rows_for_activity_event(event))
            for response in responses:
                rows.extend(rows_for_question_response(response))

            if not rows and not wanted_codes:
                continue

            rows.sort(key=lambda r: r["unix_ms"] if isinstance(r["unix_ms"], int) else float("inf"))

            filename = f"{participant.participant_code}-logs.csv"
            filepath = os.path.join(outdir, filename)

            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)

            total_files += 1
            total_rows += len(rows)
            self.stdout.write(self.style.SUCCESS(f"  {filepath}  ({len(rows)} rows)"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {total_files} file(s), {total_rows} row(s) written to '{outdir}/'"
            )
        )