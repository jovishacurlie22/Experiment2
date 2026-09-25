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
    1. Every ActivityEvent row, EXCEPT "question_answered" on a matrix
       question -- see (1b). That includes:
         lifecycle    session_started, consent_given, session_ended
         screens      screen_shown (question / rating / moduleIntro / ...)
         answers      question_answered, text_input (debounced, with
                      detail.lastKeystrokeAt), matrix_page_next,
                      rating_selected (PaaS value clicked)
         interaction  click (button/input/select/link, with id/name/value),
                      clipboard (copy/paste/cut), context_menu, back_attempt,
                      end_study_opened / end_study_cancelled /
                      end_study_confirmed
         attention    tab_hidden / tab_visible, window_blur / window_focus,
                      fullscreen_entered / fullscreen_exited, page_hide
         recording    recording_start / recording_stop,
                      webcam_recording_stopped / screen_recording_stopped
         other        server_hit, other
       Sessions recorded before the interaction events were added contain
       only the older types; fullscreen changes from that period were stored
       as "other" and can't be told apart.
    1b. Matrix row clicks: app.js fires a "question_answered" ActivityEvent
        on every row click. Its `value` is the FULL cumulative answer object
        so far, but the event also carries detail.itemId / itemValue naming
        the row just clicked, which is used directly (events without them
        fall back to diffing against the previous click for that question).
        Only the row that actually changed on THIS click is
        emitted -- one CSV row per matrix row, carrying that click's own
        timestamp, event_type "matrix_row_answered", and the item_id
        column identifying which row it was.
    2. ONE row per QuestionResponse -- not two -- except MATRIX questions,
       which get ONE ROW PER SUB-QUESTION (item_id = the matrix row; its
       unix_ms = that row's last click, presented_unix_ms = when its page
       appeared; effort_rating = the question's PaaS rating, repeated on each
       sub-row; detail = first_click_ms / since_prev_click_ms / n_changes /
       question_next_clicked_ms). The per-click matrix_row_answered rows from
       (1b) are kept too: they are the raw click log, one per click including
       changed answers, whereas these are one final row per sub-question.
       For ordinary questions the row's primary timestamp
       (unix_ms) is the ANSWERED instant (answered_epoch_ms),
       since that's the moment worth sorting a question's row by. The
       earlier PRESENTED instant is kept too, in presented_unix_ms /
       on that SAME row, so nothing needs a
       separate near-empty placeholder row -- module_id, section_id,
       question_id, answer_value and effort_rating are always populated
       together on this one row.

       answered_epoch_ms is the moment the participant CLICKED NEXT on the
       question screen: app.js stamps state.answerSubmittedAtMs[q.id] =
       Date.now() in the question's Next handler (the final Next, after the
       last matrix page) and sends it from renderRating() as answeredEpochMs.
       It is not the time of the last answer change, and nothing on the PaaS
       screen affects it, so effort-rating dwell time is never counted as
       answering time. presented_epoch_ms is stamped in renderQuestion() the
       moment the question screen appears. Both are integer epoch ms taken
       straight from the browser's Date.now().

       Sessions recorded BEFORE this change stored the last answer-change
       time as answered_epoch_ms instead; export_question_timeline.py's
       next_click_verified column tells the two kinds of session apart. If a
       NEW session's answered_epoch_ms looks like it landed after its PaaS
       screen, the deployed static JS is stale (collectstatic / gunicorn
       restart pending), not a schema issue.

Columns:
    unix_ms, unix_us, resolution_ms, unix_s,
    presented_unix_ms, source, event_type,
    session_key, screen_name, module_id, section_id, question_id,
    item_id, answer_value, effort_rating, stream_source, request_path,
    server_epoch_ms, detail

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
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from study.models import ActivityEvent, Participant, QuestionResponse

try:  # normal case: both commands live in the same management/commands package
    from .export_matrix_responses import build_matrix_rows
except ImportError:  # run as a plain module (tests)
    from export_matrix_responses import build_matrix_rows

# Every timestamp in this export originates from the browser's Date.now(),
# a real absolute unix clock but with ~1ms granularity. Recorded explicitly
# per-row so downstream merges never mistake unit conversion for precision.
CLIENT_CLOCK_RESOLUTION_MS = 1

CSV_COLUMNS = [
    "unix_ms",
    "unix_us",
    "resolution_ms",
    "unix_s",
    "presented_unix_ms",
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
    "server_epoch_ms",
    "detail",
]


def _timestamps(epoch_ms):
    """Given an integer unix_ms, return (unix_ms, unix_us, unix_s).
    unix_us is a lossless *unit* conversion (ms * 1000) -- it does not
    imply the underlying clock measured anything finer than a millisecond."""
    if epoch_ms is None:
        return "", "", ""
    return epoch_ms, epoch_ms * 1000, epoch_ms / 1000


def _dt_ms(value):
    """Server-side datetime -> integer unix ms ('' if missing)."""
    return int(round(value.timestamp() * 1000)) if value else ""


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
    unix_ms, unix_us, unix_s = _timestamps(event.epoch_ms)
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
        source="activity_event",
        session_key=session_key,
        screen_name=event.screen_name,
        module_id=detail.get("moduleId", "") if isinstance(detail, dict) else "",
        section_id=detail.get("sectionId", "") if isinstance(detail, dict) else "",
        question_id=question_id,
        stream_source=event.stream_source or "",
        request_path=event.request_path,
        server_epoch_ms=_dt_ms(event.server_timestamp),
    )

    if event.event_type == "question_answered" and isinstance(value, dict):
        # Matrix row click. `value` is the FULL cumulative answer object at
        # click time, not just the row just clicked -- diff against this
        # question's last-seen state (scoped per session_key, so a re-login
        # never inherits a stale earlier session's rows) so each emitted
        # row is exactly ONE matrix row, stamped with THIS click's own time.
        key = (session_key, question_id)
        previous = matrix_state.get(key, {})
        matrix_state[key] = dict(value)
        if isinstance(detail, dict) and detail.get("itemId"):
            # New-style event: app.js names the clicked row directly.
            changed = {detail["itemId"]: detail.get("itemValue")}
        else:
            changed = {k: v for k, v in value.items() if previous.get(k) != v}
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


def _is_matrix_answer(answer_value):
    try:
        return isinstance(json.loads(answer_value), dict)
    except (TypeError, ValueError):
        return False


def rows_for_question_response(response, matrix_items=None):
    """ONE row per QuestionResponse -- or, for a MATRIX question, ONE ROW PER
    SUB-QUESTION (matrix row). presented_at and answered_at both live on the
    SAME row (presented_unix_ms alongside the row's primary unix_ms, which is
    the ANSWERED instant), so module_id/section_id/question_id/answer_value/
    effort_rating are always populated together instead of split across
    two half-empty rows.

    Ordinary question: unix_ms = the click on the question screen's Next
    button; presented_unix_ms = the question screen appearing.
    Matrix sub-question: unix_ms = that row's LAST click (its final answer);
    presented_unix_ms = when its page appeared; item_id = the row; answer_value
    = the option finally chosen; effort_rating = the question-level PaaS
    rating (asked once per matrix, repeated on each sub-row); `detail` carries
    the row's first_click_ms, since_prev_click_ms, n_changes and the
    question's Next-click time.

    matrix_items: {item_id: row-dict from build_matrix_rows} for this
    (session, question), or None."""
    if response.answered_epoch_ms is None and response.presented_epoch_ms is None:
        return []

    if matrix_items and _is_matrix_answer(response.answer_value):
        out = []
        for item_id, mr in sorted(
            matrix_items.items(),
            key=lambda kv: (kv[1]["page_index"] if kv[1]["page_index"] != "" else 0,
                            kv[1]["first_click_ms"] if kv[1]["first_click_ms"] != "" else float("inf")),
        ):
            answered = mr["last_click_ms"] if mr["last_click_ms"] != "" else None
            presented = mr["page_shown_ms"] if mr["page_shown_ms"] != "" else response.presented_epoch_ms
            unix_ms, unix_us, unix_s = _timestamps(answered)
            presented_unix_ms, _pu, _ps = _timestamps(presented)
            out.append(_row(
                unix_ms=unix_ms,
                unix_us=unix_us,
                resolution_ms=CLIENT_CLOCK_RESOLUTION_MS if unix_ms != "" else "",
                unix_s=unix_s,
                presented_unix_ms=presented_unix_ms,
                source="question_response",
                event_type="question_answered_final",
                session_key=str(response.session.session_key),
                screen_name="question",
                module_id=response.module_id,
                section_id=response.section_id,
                question_id=response.question_id,
                item_id=item_id,
                answer_value=mr["final_value"],
                effort_rating=response.effort_rating if response.effort_rating is not None else "",
                server_epoch_ms=_dt_ms(response.server_received_at),
                detail=json.dumps({
                    "first_click_ms": mr["first_click_ms"],
                    "since_prev_click_ms": mr["since_prev_click_ms"],
                    "n_changes": mr["n_changes"],
                    "page_index": mr["page_index"],
                    "question_next_clicked_ms": response.answered_epoch_ms,
                    "flag": mr["flag"],
                }),
            ))
        return out

    unix_ms, unix_us, unix_s = _timestamps(response.answered_epoch_ms)
    presented_unix_ms, _presented_us, _presented_s = _timestamps(
        response.presented_epoch_ms
    )

    return [_row(
        unix_ms=unix_ms,
        unix_us=unix_us,
        resolution_ms=CLIENT_CLOCK_RESOLUTION_MS if unix_ms != "" else "",
        unix_s=unix_s,
        presented_unix_ms=presented_unix_ms,
        source="question_response",
        event_type="question_answered_final",
        session_key=str(response.session.session_key),
        screen_name="question",
        module_id=response.module_id,
        section_id=response.section_id,
        question_id=response.question_id,
        answer_value=response.answer_value,
        effort_rating=response.effort_rating if response.effort_rating is not None else "",
        server_epoch_ms=_dt_ms(response.server_received_at),
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
            # participant OR session-linked, so events are never dropped just
            # because their participant column was left empty.
            events = list(
                ActivityEvent.objects
                .filter(Q(participant=participant) | Q(session__participant=participant))
                .select_related("session")
                .order_by("session_key", "epoch_ms")
            )
            responses = list(
                QuestionResponse.objects.filter(session__participant=participant)
                .select_related("session")
            )

            # Per-sub-question data for matrix questions (from click events).
            events_by_session = defaultdict(list)
            for event in events:
                sk = event.session_key or (str(event.session.session_key) if event.session_id else "")
                events_by_session[sk].append(event)
            responses_by_session = defaultdict(dict)
            for response in responses:
                responses_by_session[str(response.session.session_key)][response.question_id] = response
            matrix_items = defaultdict(dict)  # (session_key, question_id) -> {item_id: row}
            for sk, evs in events_by_session.items():
                mrows, _missing = build_matrix_rows(
                    participant.participant_code, sk, evs, responses_by_session.get(sk, {})
                )
                for mr in mrows:
                    matrix_items[(sk, mr["question_id"])][mr["item_id"]] = mr

            rows = []
            matrix_state = {}  # reset per participant; keyed by session_key too
            for event in events:
                rows.extend(rows_for_activity_event(event, matrix_state))
            for response in responses:
                key = (str(response.session.session_key), response.question_id)
                rows.extend(rows_for_question_response(response, matrix_items.get(key)))

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