"""
Exports every activity for each participant into a single, merged,
chronologically-sorted CSV: screens shown, individual matrix-row clicks
(with their own timestamps), one fully-populated row per question
(presented + answered + effort rating together), consent, recording
start/stop, session start/end, fullscreen changes, server hits --
everything logged for that participant.

Usage (from the project root, same place you run manage.py):
    python manage.py export_activity_logs
    python manage.py export_activity_logs --outdir websitelogs
    python manage.py export_activity_logs --participant 7
    python manage.py export_activity_logs --participant 7 --participant 12

Output:
    websitelogs/<participant_code>-logs.csv   (one file per participant)

Row sources, merged and sorted by unix_ms (the row's PRIMARY timestamp --
see each source below for which instant that is):
    1. Every ActivityEvent row (session_started, consent_given,
       screen_shown, fullscreen_entered/exited, recording_start/stop,
       webcam_recording_stopped, screen_recording_stopped, session_ended,
       server_hit, other...), EXCEPT "question_answered" on a matrix
       question -- see (1b).
    1b. Matrix row clicks: app.js fires a "question_answered" ActivityEvent
        on every row click, but each event's logged value is the FULL
        cumulative answer object so far (every row answered up to that
        point), not just the row just clicked. Each event here is diffed
        against the previous click for that same question (per session)
        so only the row(s) that actually changed on THIS click are
        emitted -- one CSV row per matrix row, carrying that click's own
        timestamp, event_type "matrix_row_answered", and the item_id
        column identifying which row it was.
    2. ONE row per QuestionResponse -- not two. Its primary timestamp
       (unix_ms / datetime_utc) is the ANSWERED instant (answered_epoch_ms),
       since that's the moment worth sorting a question's row by. The
       earlier PRESENTED instant is kept too, in presented_unix_ms /
       presented_datetime_utc, on that SAME row, so nothing needs a
       separate near-empty placeholder row -- module_id, section_id,
       question_id, answer_value and effort_rating are always populated
       together on this one row.

       answered_epoch_ms itself is exactly what app.js's recordAnswerChange
       set as the answer's own last-changed time -- app.js's renderRating()
       (the PaaS screen) deliberately reuses that same value rather than
       stamping "now" at submit time, specifically so effort-rating dwell
       time is never counted as part of answering the question. If a
       participant's answered_epoch_ms here looks like it landed AFTER
       their PaaS screen, that points to the deployed static JS being
       stale (collectstatic/gunicorn restart pending), not a schema issue
       -- the source of truth for that timestamp is client-side, in
       state.answerLastChangedAt[q.id], set at the moment of the actual
       answer change, never at the rating screen.

Columns:
    unix_ms, unix_us, resolution_ms, unix_s, datetime_utc,
    presented_unix_ms, presented_datetime_utc, source, event_type,
    session_key, screen_name, module_id, section_id, question_id,
    item_id, answer_value, effort_rating, stream_source, request_path,
    client_timestamp, server_timestamp, detail

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
    "presented_unix_ms",
    "presented_datetime_utc",
    "source",
    "event_type",
    "session_key",
    "screen_name",
    "module_id",
    "section_id",
    "question_id",
    "item_id",
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


def rows_for_activity_event(event, matrix_state):
    """matrix_state: mutable {(session_key, question_id): last_answer_dict},
    threaded across ALL events for a participant (in chronological order)
    so a matrix question's row-click diff carries over correctly even
    though this function only sees one event at a time."""
    unix_ms, unix_us, unix_s, datetime_utc = _timestamps(event.epoch_ms)
    session_key = event.session_key or (
        str(event.session.session_key) if event.session_id else ""
    )
    detail = event.detail or event.meta or {}
    question_id = detail.get("questionId", "") if isinstance(detail, dict) else ""
    value = detail.get("value") if isinstance(detail, dict) else None

    base = dict(
        unix_ms=unix_ms,
        unix_us=unix_us,
        resolution_ms=CLIENT_CLOCK_RESOLUTION_MS if unix_ms != "" else "",
        unix_s=unix_s,
        datetime_utc=datetime_utc,
        source="activity_event",
        session_key=session_key,
        screen_name=event.screen_name,
        module_id=detail.get("moduleId", "") if isinstance(detail, dict) else "",
        section_id=detail.get("sectionId", "") if isinstance(detail, dict) else "",
        question_id=question_id,
        stream_source=event.stream_source or "",
        request_path=event.request_path,
        client_timestamp=event.client_timestamp.isoformat() if event.client_timestamp else "",
        server_timestamp=event.server_timestamp.isoformat() if event.server_timestamp else "",
    )

    if event.event_type == "question_answered" and isinstance(value, dict):
        # Matrix row click. `value` is the FULL cumulative answer object at
        # click time, not just the row just clicked -- diff against this
        # question's last-seen state (scoped per session_key, so a re-login
        # never inherits a stale earlier session's rows) so each emitted
        # row is exactly ONE matrix row, stamped with THIS click's own time.
        key = (session_key, question_id)
        previous = matrix_state.get(key, {})
        changed = {k: v for k, v in value.items() if previous.get(k) != v}
        matrix_state[key] = dict(value)
        if not changed:
            return []  # click didn't actually change anything (rare no-op)
        rows = []
        for item_id, item_value in changed.items():
            row = dict(base)
            row["event_type"] = "matrix_row_answered"
            row["item_id"] = item_id
            row["answer_value"] = item_value
            row["detail"] = json.dumps(value, ensure_ascii=False)
            rows.append(_row(**row))
        return rows

    row = dict(base)
    row["event_type"] = event.event_type
    row["answer_value"] = json.dumps(value) if isinstance(detail, dict) and "value" in detail else ""
    row["detail"] = json.dumps(detail, ensure_ascii=False) if detail else ""
    return [_row(**row)]


def rows_for_question_response(response):
    """ONE row per QuestionResponse -- presented_at and answered_at both
    live on this SAME row (presented_unix_ms / presented_datetime_utc
    alongside the row's primary unix_ms / datetime_utc, which is the
    ANSWERED instant), so module_id/section_id/question_id/answer_value/
    effort_rating are always populated together instead of split across
    two half-empty rows."""
    if response.answered_epoch_ms is None and response.presented_epoch_ms is None:
        return []

    unix_ms, unix_us, unix_s, datetime_utc = _timestamps(response.answered_epoch_ms)
    presented_unix_ms, _presented_us, _presented_s, presented_datetime_utc = _timestamps(
        response.presented_epoch_ms
    )

    return [_row(
        unix_ms=unix_ms,
        unix_us=unix_us,
        resolution_ms=CLIENT_CLOCK_RESOLUTION_MS if unix_ms != "" else "",
        unix_s=unix_s,
        datetime_utc=datetime_utc,
        presented_unix_ms=presented_unix_ms,
        presented_datetime_utc=presented_datetime_utc,
        source="question_response",
        event_type="question_answered_final",
        session_key=str(response.session.session_key),
        screen_name="question",
        module_id=response.module_id,
        section_id=response.section_id,
        question_id=response.question_id,
        answer_value=response.answer_value,
        effort_rating=response.effort_rating if response.effort_rating is not None else "",
        client_timestamp=response.answered_at.isoformat() if response.answered_at else "",
        server_timestamp=response.server_received_at.isoformat()
            if response.server_received_at else "",
    )]


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
            events = ActivityEvent.objects.filter(participant=participant).order_by("session_key", "epoch_ms")
            responses = QuestionResponse.objects.filter(
                session__participant=participant
            ).select_related("session")

            rows = []
            matrix_state = {}  # reset per participant; keyed by session_key too
            for event in events:
                rows.extend(rows_for_activity_event(event, matrix_state))
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