"""
Per-question timeline export: ONE row per participant per question -- and,
for MATRIX questions, ONE ROW PER SUB-QUESTION (matrix row, `item_id`) --
with every moment that matters for a cognitive-load analysis on the same
row, all as integer epoch milliseconds (browser Date.now()), plus the
derived durations.

Matrix rows (item_id filled):
    presented_ms          the page holding this row appeared
    first_interaction_ms  first click on this row;  last_change_ms / answered_ms
                          = its last click (its final answer)
    next_clicked_ms       the question's final Next click (logged, but it is
                          NOT this row's answer time)
    since_prev_click_ms   first click - previous click on the page (or page
                          shown): the "time spent on this row" measure
    response_ms           answered_ms - presented_ms
    reading_ms            page shown -> first click on this row
    answering_ms          blank (not meaningful per row)
    attention columns     measured over that same "time on this row" window
    PaaS columns / effort_rating / question_ms repeat the QUESTION-level value
    on every sub-row (the PaaS scale is asked once per matrix question) --
    don't sum or average them across sub-rows as if independent.
    question_order numbers questions; all sub-rows of a matrix share it.

Usage (from the project root, same place you run manage.py):
    python manage.py export_question_timeline
    python manage.py export_question_timeline --outdir websitelogs
    python manage.py export_question_timeline --participant 7
    python manage.py export_question_timeline --participant 7 --participant 12
    python manage.py export_question_timeline --combined all_timelines.csv

Output:
    websitelogs/<participant_code>-timeline.csv   (one file per participant)
    <--combined file>                             (optional, every row in one CSV)

Sources:
    QuestionResponse -> presented_ms, next_clicked_ms, effort_rating, answer
    ActivityEvent    -> everything in between (screen_shown, click,
                        question_answered, text_input, rating_selected,
                        tab_hidden/visible, window_blur/focus,
                        fullscreen_exited/entered, back_attempt, clipboard)

Definitions (all times epoch ms):
    presented_ms          question screen appeared (renderQuestion)
    first_interaction_ms  first click / answer change / text burst on the
                          question (text fields: the end of the first typing
                          burst if no click happened -- an upper bound)
    last_change_ms        last answer change before Next
    next_clicked_ms       the click on the question screen's Next button
                          (= QuestionResponse.answered_epoch_ms)
    answered_ms           when the (sub-)question was answered: = next_clicked_ms
                          for ordinary questions, the row's last click for matrix rows
    question_presented_ms question screen first appeared (= presented_ms except
                          on later pages of a paginated matrix)
    paas_shown_ms         PaaS rating screen appeared
    paas_first_click_ms / paas_last_click_ms   first / last PaaS value clicked
    paas_submitted_ms     click on the PaaS screen's Next button (falls back
                          to the server receipt time, see paas_submitted_source)

Derived durations (ms):
    reading_ms                  presented -> first_interaction
    answering_ms                first_interaction -> next_clicked
    question_ms                 question_presented -> next_clicked (question level)
    response_ms                 presented -> answered (= question_ms for ordinary
                                questions; per-row for matrix rows)
    post_answer_hesitation_ms   last_change -> next_clicked
    paas_first_click_latency_ms paas_shown -> paas_first_click
    paas_ms                     paas_shown -> paas_submitted

Attention/environment during the question and PaaS windows:
    tab_hidden_ms_*, blur_ms_*, fullscreen_out_ms_question,
    fullscreen_exits_question, n_clicks, n_back_attempts, n_clipboard

Quality columns:
    next_click_verified  True when a click on the question's Next button was
                         logged within 50 ms of answered_epoch_ms -- i.e. the
                         row was recorded under the "answered = Next click"
                         definition. False = older session (answered = last
                         answer change) or a missing click event; treat those
                         rows separately when comparing response times.
    flag                 ';'-joined: no_response (question shown, never
                         submitted), no_paas_screen_event, negative_duration,
                         no_presented_time, matrix_not_expanded (matrix answer
                         with no per-row click events -- older app.js -- kept
                         as one row), plus the matrix row flags no_click /
                         no_page_event

Notes / limits:
    * Sessions recorded before the extra event types existed have no
      fullscreen/tab/click events; those columns come out 0/blank for them.
    * Fullscreen transitions logged before fullscreen_entered/exited were
      added to EVENT_TYPES were stored as "other" and can't be recovered.
"""

import csv
import datetime as dt
import os
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from study.models import ActivityEvent, Participant, QuestionResponse, StudySession

try:  # normal case: both commands live in the same management/commands package
    from .export_matrix_responses import build_matrix_rows
except ImportError:  # run as a plain module (tests)
    from export_matrix_responses import build_matrix_rows

QUESTION_NEXT_ID = "btn-next"          # question screen's Next button (app.js)
RATING_NEXT_ID = "btn-rating-next"     # PaaS screen's Next button (app.js)
NEXT_CLICK_TOLERANCE_MS = 50

CSV_COLUMNS = [
    "participant_code", "session_key", "question_order",
    "module_id", "section_id", "question_id", "item_id", "page_index",
    "answer_value", "effort_rating",
    "question_presented_ms", "presented_ms", "first_interaction_ms",
    "last_change_ms", "answered_ms", "next_clicked_ms",
    "paas_shown_ms", "paas_first_click_ms", "paas_last_click_ms",
    "paas_submitted_ms", "paas_submitted_source",
    "reading_ms", "answering_ms", "question_ms", "response_ms",
    "since_prev_click_ms", "post_answer_hesitation_ms",
    "paas_first_click_latency_ms", "paas_ms",
    "n_answer_events", "n_text_bursts", "n_paas_changes",
    "n_clicks", "n_back_attempts", "n_clipboard",
    "tab_hidden_ms_question", "tab_hidden_ms_paas",
    "blur_ms_question", "blur_ms_paas",
    "fullscreen_exits_question", "fullscreen_out_ms_question",
    "next_click_verified", "flag",
]


def _detail(event):
    d = getattr(event, "detail", None) or getattr(event, "meta", None) or {}
    return d if isinstance(d, dict) else {}


def _iso_to_ms(value):
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(round(d.timestamp() * 1000))


def _dt_to_ms(value):
    return int(round(value.timestamp() * 1000)) if value else None


def _diff(a, b):
    return a - b if a is not None and b is not None else ""


def _intervals(events, start_type, end_type):
    """[(start_ms, end_ms)] for paired start/end events. An interval still
    open at the end of the session is closed at the last event's time."""
    out, open_ts = [], None
    for e in events:
        if e.event_type == start_type and open_ts is None:
            open_ts = e.epoch_ms
        elif e.event_type == end_type and open_ts is not None:
            out.append((open_ts, e.epoch_ms))
            open_ts = None
    if open_ts is not None and events:
        out.append((open_ts, events[-1].epoch_ms))
    return out


def _overlap(intervals, lo, hi):
    if lo is None or hi is None or hi < lo:
        return ""
    return sum(max(0, min(e, hi) - max(s, lo)) for s, e in intervals)


def _count(ts_list, lo, hi):
    if lo is None or hi is None:
        return ""
    return sum(1 for t in ts_list if lo <= t <= hi)


def build_session_rows(participant_code, session_key, events, responses):
    """events: ActivityEvent-like objects; responses: {question_id: QuestionResponse}.
    Pure function (no DB access) so it can be tested on its own."""
    ev = sorted((e for e in events if e.epoch_ms is not None), key=lambda e: e.epoch_ms)
    last_ts = ev[-1].epoch_ms if ev else None

    hidden = _intervals(ev, "tab_hidden", "tab_visible")
    blurred = _intervals(ev, "window_blur", "window_focus")
    fs_out = _intervals(ev, "fullscreen_exited", "fullscreen_entered")

    shown_q, shown_r = {}, {}
    answered, texts, rating_sel = defaultdict(list), defaultdict(list), defaultdict(list)
    clicks, backs, clips, fs_exits = [], [], [], []
    for e in ev:
        d, t, et = _detail(e), e.epoch_ms, e.event_type
        qid = d.get("questionId")
        if et == "screen_shown":
            if e.screen_name == "question" and qid:
                shown_q.setdefault(qid, t)
            elif e.screen_name == "rating" and qid:
                shown_r.setdefault(qid, t)
        elif et == "question_answered" and qid:
            answered[qid].append(t)
        elif et == "text_input" and qid:
            texts[qid].append(d.get("lastKeystrokeMs") or _iso_to_ms(d.get("lastKeystrokeAt")) or t)
        elif et == "rating_selected" and qid:
            rating_sel[qid].append(t)
        elif et == "click":
            clicks.append((t, d.get("id", "")))
        elif et == "back_attempt":
            backs.append(t)
        elif et == "clipboard":
            clips.append(t)
        elif et == "fullscreen_exited":
            fs_exits.append(t)

    q_starts = sorted(shown_q.values())
    rows = []

    # Matrix questions are exported ONE ROW PER SUB-QUESTION (matrix row),
    # timed by that row's own clicks; see export_matrix_responses.py.
    matrix_rows, _n_missing = build_matrix_rows(participant_code, session_key, events, responses)
    matrix_by_q = defaultdict(list)
    for mr in matrix_rows:
        matrix_by_q[mr["question_id"]].append(mr)

    for qid in set(responses) | set(shown_q) | set(matrix_by_q):
        r = responses.get(qid)
        flags = []

        presented = r.presented_epoch_ms if r and r.presented_epoch_ms is not None else shown_q.get(qid)
        next_clicked = r.answered_epoch_ms if r else None
        paas_shown = shown_r.get(qid)
        if r is None:
            flags.append("no_response")
        if presented is None:
            flags.append("no_presented_time")
        if r is not None and paas_shown is None:
            flags.append("no_paas_screen_event")

        # End of the question window: Next click, else PaaS screen, else the
        # next question's appearance, else the end of the session's events.
        q_end = next_clicked if next_clicked is not None else paas_shown
        if q_end is None and presented is not None:
            later = [s_ for s_ in q_starts if s_ > presented]
            q_end = later[0] if later else last_ts

        # PaaS screen (question-level; repeated on every sub-row of a matrix).
        paas_first = paas_last = None
        sel = [t for t in rating_sel[qid] if paas_shown is None or t >= paas_shown]
        if sel:
            paas_first, paas_last = min(sel), max(sel)

        paas_sub, paas_src = None, ""
        lo = paas_shown if paas_shown is not None else next_clicked
        if lo is not None:
            cand = [t for t, cid in clicks if cid == RATING_NEXT_ID and t >= lo]
            if cand:
                paas_sub, paas_src = min(cand), "click_event"
        if paas_sub is None and r is not None and getattr(r, "server_received_at", None):
            paas_sub, paas_src = _dt_to_ms(r.server_received_at), "server_received_at"

        verified = ""
        if next_clicked is not None:
            verified = any(
                cid == QUESTION_NEXT_ID
                and next_clicked - NEXT_CLICK_TOLERANCE_MS <= t <= next_clicked + NEXT_CLICK_TOLERANCE_MS
                for t, cid in clicks
            )

        question_ms = _diff(next_clicked, presented)
        if question_ms != "" and question_ms < 0:
            flags.append("negative_duration")

        common = {
            "participant_code": participant_code,
            "session_key": session_key,
            "question_id": qid,
            "effort_rating": r.effort_rating if r and r.effort_rating is not None else "",
            "question_presented_ms": presented if presented is not None else "",
            "next_clicked_ms": next_clicked if next_clicked is not None else "",
            "paas_shown_ms": paas_shown if paas_shown is not None else "",
            "paas_first_click_ms": paas_first if paas_first is not None else "",
            "paas_last_click_ms": paas_last if paas_last is not None else "",
            "paas_submitted_ms": paas_sub if paas_sub is not None else "",
            "paas_submitted_source": paas_src,
            "question_ms": question_ms,
            "paas_first_click_latency_ms": _diff(paas_first, paas_shown),
            "paas_ms": _diff(paas_sub, paas_shown),
            "n_paas_changes": len(sel),
            "tab_hidden_ms_paas": _overlap(hidden, paas_shown, paas_sub),
            "blur_ms_paas": _overlap(blurred, paas_shown, paas_sub),
            "next_click_verified": verified,
        }

        if qid in matrix_by_q:
            # ---- one row per matrix sub-question -------------------------
            for mr in matrix_by_q[qid]:
                page_shown = mr["page_shown_ms"] if mr["page_shown_ms"] != "" else presented
                first = mr["first_click_ms"] if mr["first_click_ms"] != "" else None
                last = mr["last_click_ms"] if mr["last_click_ms"] != "" else None
                since_prev = mr["since_prev_click_ms"]
                # Time "spent on this row" = previous click (or page shown) -> its first click.
                anchor = first - since_prev if (first is not None and since_prev != "") else page_shown
                row_flags = list(flags) + [f for f in mr["flag"].split(";") if f]
                rows.append(dict(common, **{
                    "module_id": mr["module_id"], "section_id": mr["section_id"],
                    "item_id": mr["item_id"], "page_index": mr["page_index"],
                    "answer_value": mr["final_value"],
                    "presented_ms": page_shown if page_shown is not None else "",
                    "first_interaction_ms": first if first is not None else "",
                    "last_change_ms": last if last is not None else "",
                    "answered_ms": last if last is not None else "",
                    "reading_ms": _diff(first, page_shown),
                    "answering_ms": "",   # n/a for a matrix row (see docstring)
                    "response_ms": _diff(last, page_shown),
                    "since_prev_click_ms": since_prev,
                    "post_answer_hesitation_ms": _diff(next_clicked, last),
                    "n_answer_events": mr["n_changes"], "n_text_bursts": "",
                    "n_clicks": mr["n_changes"],
                    "n_back_attempts": _count(backs, anchor, first),
                    "n_clipboard": _count(clips, anchor, first),
                    "tab_hidden_ms_question": _overlap(hidden, anchor, first),
                    "blur_ms_question": _overlap(blurred, anchor, first),
                    "fullscreen_exits_question": _count(fs_exits, anchor, first),
                    "fullscreen_out_ms_question": _overlap(fs_out, anchor, first),
                    "flag": ";".join(row_flags),
                }))
            continue

        # ---- regular (non-matrix) question: one row ------------------------
        if r is not None and str(r.answer_value).lstrip().startswith("{"):
            # A matrix answer with no per-row click events (session recorded
            # before app.js sent itemId): can't be split, so it stays one row.
            flags.append("matrix_not_expanded")
        first_int = last_change = None
        n_answer = n_text = n_clicks = ""
        if presented is not None and q_end is not None:
            in_win = lambda t: presented <= t <= q_end
            q_clicks = [t for t, cid in clicks if in_win(t) and cid != QUESTION_NEXT_ID]
            a_ts = [t for t in answered[qid] if in_win(t)]
            x_ts = [t for t in texts[qid] if in_win(t)]
            cands = q_clicks + a_ts + x_ts
            first_int = min(cands) if cands else None
            changes = a_ts + x_ts
            last_change = max(changes) if changes else None
            n_answer, n_text, n_clicks = len(a_ts), len(x_ts), len(q_clicks)

        rows.append(dict(common, **{
            "module_id": r.module_id if r else "",
            "section_id": r.section_id if r else "",
            "item_id": "", "page_index": "",
            "answer_value": r.answer_value if r else "",
            "presented_ms": presented if presented is not None else "",
            "first_interaction_ms": first_int if first_int is not None else "",
            "last_change_ms": last_change if last_change is not None else "",
            "answered_ms": next_clicked if next_clicked is not None else "",
            "reading_ms": _diff(first_int, presented),
            "answering_ms": _diff(next_clicked, first_int),
            "response_ms": question_ms,
            "since_prev_click_ms": "",
            "post_answer_hesitation_ms": _diff(next_clicked, last_change),
            "n_answer_events": n_answer, "n_text_bursts": n_text, "n_clicks": n_clicks,
            "n_back_attempts": _count(backs, presented, q_end),
            "n_clipboard": _count(clips, presented, q_end),
            "tab_hidden_ms_question": _overlap(hidden, presented, q_end),
            "blur_ms_question": _overlap(blurred, presented, q_end),
            "fullscreen_exits_question": _count(fs_exits, presented, q_end),
            "fullscreen_out_ms_question": _overlap(fs_out, presented, q_end),
            "flag": ";".join(flags),
        }))

    def sort_key(x):
        qp = x["question_presented_ms"] if x["question_presented_ms"] != "" else float("inf")
        pg = x["page_index"] if x["page_index"] != "" else 0
        fc = x["first_interaction_ms"] if x["first_interaction_ms"] != "" else float("inf")
        return (qp, x["question_id"], pg, fc)

    rows.sort(key=sort_key)
    # question_order numbers QUESTIONS (all sub-rows of a matrix share one number).
    order, n = {}, 0
    for row in rows:
        if row["question_id"] not in order:
            n += 1
            order[row["question_id"]] = n
        row["question_order"] = order[row["question_id"]]
    return rows


class Command(BaseCommand):
    help = (
        "Export one row per participant per question with presented / "
        "first-interaction / Next-click / PaaS timestamps (epoch ms), derived "
        "durations, and attention metrics into websitelogs/<id>-timeline.csv"
    )

    def add_arguments(self, parser):
        parser.add_argument("--outdir", default="websitelogs",
                            help="Output folder (created if missing). Default: websitelogs")
        parser.add_argument("--participant", action="append", default=None,
                            help="Limit to this participant_code. Repeatable.")
        parser.add_argument("--combined", default=None,
                            help="Also write every row into this single CSV.")

    def handle(self, *args, **options):
        outdir = options["outdir"]
        wanted = options["participant"]
        os.makedirs(outdir, exist_ok=True)

        participants = Participant.objects.all().order_by("participant_code")
        if wanted:
            participants = participants.filter(participant_code__in=wanted)
            missing = set(wanted) - set(participants.values_list("participant_code", flat=True))
            if missing:
                raise CommandError(f"Unknown participant_code(s): {', '.join(sorted(missing))}")

        all_rows, total_files = [], 0
        for participant in participants:
            rows = []
            for session in StudySession.objects.filter(participant=participant).order_by("started_at"):
                events = ActivityEvent.objects.filter(
                    Q(session=session) | Q(session_key=str(session.session_key))
                ).order_by("epoch_ms")
                responses = {r.question_id: r for r in QuestionResponse.objects.filter(session=session)}
                rows.extend(build_session_rows(
                    participant.participant_code, str(session.session_key), list(events), responses
                ))

            if not rows and not wanted:
                continue

            filepath = os.path.join(outdir, f"{participant.participant_code}-timeline.csv")
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)

            all_rows.extend(rows)
            total_files += 1
            self.stdout.write(self.style.SUCCESS(f"  {filepath}  ({len(rows)} rows)"))

        if options["combined"]:
            with open(options["combined"], "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(all_rows)
            self.stdout.write(self.style.SUCCESS(f"  {options['combined']}  ({len(all_rows)} rows, combined)"))

        self.stdout.write(self.style.SUCCESS(
            f"Done: {total_files} file(s), {len(all_rows)} row(s) written to '{outdir}/'"
        ))