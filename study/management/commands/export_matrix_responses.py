"""
Per-ROW export for matrix questions: ONE row per participant per matrix row,
timed by when that row's option was actually clicked. (A matrix question's
QuestionResponse holds the whole grid as one answer, so its single
presented -> Next-click span says little about individual rows.)

Usage (from the project root, same place you run manage.py):
    python manage.py export_matrix_responses
    python manage.py export_matrix_responses --outdir websitelogs
    python manage.py export_matrix_responses --participant 7
    python manage.py export_matrix_responses --combined all_matrix_rows.csv

Output:
    websitelogs/<participant_code>-matrix.csv   (one file per participant)
    <--combined file>                           (optional, every row in one CSV)

Sources (ActivityEvent only, plus QuestionResponse for question-level times):
    matrix_page_shown   a matrix page appeared (page index + the row ids on it)
    question_answered   a row was clicked; detail.itemId / itemValue name the row
    matrix_page_next    Next clicked on a non-final page

All times are integer epoch ms (browser Date.now()).

Columns:
    participant_code, session_key, module_id, section_id, question_id, item_id
    page_index, total_pages   which page of the question the row was on
    click_order_in_page       1 = first row clicked on that page
    question_presented_ms     question screen first appeared
    page_shown_ms             this row's page appeared (all its rows appear together)
    first_click_ms            first time this row was clicked
    last_click_ms             last time (= its final answer)
    page_next_clicked_ms      Next clicked on this row's page (final page: the
                              question's Next click)
    question_next_clicked_ms  the question's final Next click
    rt_first_click_ms         first_click - page_shown
    since_prev_click_ms       first_click - the previous click of ANY row on this
                              page (or page_shown for the first click) -- the
                              usual "time spent on this row" measure
    rt_last_click_ms          last_click - page_shown
    n_changes                 how many times the row was clicked (1 = never changed)
    first_value, final_value  option code first / finally chosen
    flag                      no_click (row shown, never answered), no_page_event
                              (page-shown event missing), missing_item_id_events

Notes:
    * Rows are only produced from sessions whose clicks carry itemId (i.e. the
      current app.js). If matrix clicks without itemId are found, the run warns
      -- the deployed JS is stale.
    * Clicking the already-selected option fires no event, so n_changes counts
      real changes only.
"""

import csv
import os
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from study.models import ActivityEvent, Participant, QuestionResponse, StudySession

CSV_COLUMNS = [
    "participant_code", "session_key", "module_id", "section_id", "question_id",
    "item_id", "page_index", "total_pages", "click_order_in_page",
    "question_presented_ms", "page_shown_ms", "first_click_ms", "last_click_ms",
    "page_next_clicked_ms", "question_next_clicked_ms",
    "rt_first_click_ms", "since_prev_click_ms", "rt_last_click_ms",
    "n_changes", "first_value", "final_value", "flag",
]


def _detail(event):
    d = getattr(event, "detail", None) or getattr(event, "meta", None) or {}
    return d if isinstance(d, dict) else {}


def _diff(a, b):
    return a - b if a is not None and b is not None else ""


def build_matrix_rows(participant_code, session_key, events, responses):
    """Pure function (no DB access). Returns (rows, n_events_without_item_id)."""
    ev = sorted((e for e in events if e.epoch_ms is not None), key=lambda e: e.epoch_ms)

    pages = defaultdict(list)    # qid -> [{ts, page, total, rows}]
    page_next = defaultdict(dict)  # qid -> {from_page: ts}
    clicks = defaultdict(list)   # qid -> [{ts, item, value, page, module, section}]
    shown_q = {}
    missing_item_id = 0

    for e in ev:
        d, t, et = _detail(e), e.epoch_ms, e.event_type
        qid = d.get("questionId")
        if not qid:
            continue
        if et == "screen_shown" and e.screen_name == "question":
            shown_q.setdefault(qid, t)
        elif et == "matrix_page_shown":
            pages[qid].append({"ts": t, "page": d.get("page", 0),
                               "total": d.get("totalPages", 1),
                               "rows": list(d.get("rowIds") or [])})
        elif et == "matrix_page_next":
            page_next[qid].setdefault(d.get("fromPage"), t)
        elif et == "question_answered" and isinstance(d.get("value"), dict):
            if d.get("itemId"):
                clicks[qid].append({"ts": t, "item": d["itemId"], "value": d.get("itemValue"),
                                    "page": d.get("page", 0),
                                    "module": d.get("moduleId", ""), "section": d.get("sectionId", "")})
            else:
                missing_item_id += 1

    rows = []
    for qid in set(pages) | set(clicks):
        r = responses.get(qid)
        presented = (r.presented_epoch_ms if r and r.presented_epoch_ms is not None else shown_q.get(qid))
        q_next = r.answered_epoch_ms if r else None
        module = r.module_id if r else ""
        section = r.section_id if r else ""
        q_pages = sorted(pages[qid], key=lambda p: p["ts"])
        q_clicks = sorted(clicks[qid], key=lambda c: c["ts"])
        if q_clicks and not module:
            module, section = q_clicks[0]["module"], q_clicks[0]["section"]

        # Window of each page instance: from its shown time to the next page's.
        def page_for(click_ts, item):
            best = None
            for p in q_pages:
                if p["ts"] <= click_ts and item in p["rows"]:
                    best = p
            return best

        # prev-click bookkeeping per page instance
        prev_ts = {}   # id(page) -> last click ts so far
        per_item = {}  # (item) -> aggregate
        for c in q_clicks:
            p = page_for(c["ts"], c["item"])
            base = p["ts"] if p else None
            key = id(p) if p else None
            since_prev = _diff(c["ts"], prev_ts.get(key, base))
            prev_ts[key] = c["ts"]
            agg = per_item.get(c["item"])
            if agg is None:
                per_item[c["item"]] = {
                    "page": p, "first": c["ts"], "last": c["ts"], "n": 1,
                    "first_value": c["value"], "final_value": c["value"],
                    "since_prev": since_prev,
                }
            else:
                agg["last"], agg["n"], agg["final_value"] = c["ts"], agg["n"] + 1, c["value"]

        # click order within each page instance
        order = {}
        by_page = defaultdict(list)
        for item, agg in per_item.items():
            by_page[id(agg["page"]) if agg["page"] else None].append((agg["first"], item))
        for lst in by_page.values():
            for i, (_, item) in enumerate(sorted(lst), start=1):
                order[item] = i

        def emit(item, agg, page):
            flags = []
            if page is None:
                flags.append("no_page_event")
            page_shown = page["ts"] if page else None
            page_idx = page["page"] if page else (agg["page"]["page"] if agg and agg["page"] else "")
            total = page["total"] if page else ""
            if page is not None:
                pn = page_next[qid].get(page["page"])
                is_last = total in ("", None) or page["page"] >= (total - 1)
                page_next_ms = q_next if is_last else pn
            else:
                page_next_ms = None
            if agg is None:
                flags.append("no_click")
            first = agg["first"] if agg else None
            last = agg["last"] if agg else None
            rows.append({
                "participant_code": participant_code, "session_key": session_key,
                "module_id": module, "section_id": section, "question_id": qid,
                "item_id": item, "page_index": page_idx, "total_pages": total,
                "click_order_in_page": order.get(item, ""),
                "question_presented_ms": presented if presented is not None else "",
                "page_shown_ms": page_shown if page_shown is not None else "",
                "first_click_ms": first if first is not None else "",
                "last_click_ms": last if last is not None else "",
                "page_next_clicked_ms": page_next_ms if page_next_ms is not None else "",
                "question_next_clicked_ms": q_next if q_next is not None else "",
                "rt_first_click_ms": _diff(first, page_shown),
                "since_prev_click_ms": agg["since_prev"] if agg else "",
                "rt_last_click_ms": _diff(last, page_shown),
                "n_changes": agg["n"] if agg else 0,
                "first_value": agg["first_value"] if agg else "",
                "final_value": agg["final_value"] if agg else "",
                "flag": ";".join(flags),
            })

        seen = set()
        for p in q_pages:                       # every row shown on any page
            for item in p["rows"]:
                if item in seen:
                    continue
                agg = per_item.get(item)
                if agg is not None and agg["page"] is not p:
                    continue                    # belongs to a different page instance
                seen.add(item)
                emit(item, agg, p)
        for item, agg in per_item.items():      # clicked rows with no page event
            if item not in seen:
                emit(item, agg, agg["page"])

    def sort_key(x):
        return (x["question_presented_ms"] if x["question_presented_ms"] != "" else float("inf"),
                x["question_id"],
                x["page_index"] if x["page_index"] != "" else 0,
                x["first_click_ms"] if x["first_click_ms"] != "" else float("inf"))
    rows.sort(key=sort_key)
    return rows, missing_item_id


class Command(BaseCommand):
    help = ("Export one row per participant per matrix row, timed by the click "
            "on that row's option (epoch ms), into websitelogs/<id>-matrix.csv")

    def add_arguments(self, parser):
        parser.add_argument("--outdir", default="websitelogs",
                            help="Output folder (created if missing). Default: websitelogs")
        parser.add_argument("--participant", action="append", default=None,
                            help="Limit to this participant_code. Repeatable.")
        parser.add_argument("--combined", default=None,
                            help="Also write every row into this single CSV.")

    def handle(self, *args, **options):
        outdir, wanted = options["outdir"], options["participant"]
        os.makedirs(outdir, exist_ok=True)

        participants = Participant.objects.all().order_by("participant_code")
        if wanted:
            participants = participants.filter(participant_code__in=wanted)
            missing = set(wanted) - set(participants.values_list("participant_code", flat=True))
            if missing:
                raise CommandError(f"Unknown participant_code(s): {', '.join(sorted(missing))}")

        all_rows, total_files, stale = [], 0, 0
        for participant in participants:
            rows = []
            for session in StudySession.objects.filter(participant=participant).order_by("started_at"):
                events = ActivityEvent.objects.filter(
                    Q(session=session) | Q(session_key=str(session.session_key))
                ).order_by("epoch_ms")
                responses = {r.question_id: r for r in QuestionResponse.objects.filter(session=session)}
                s_rows, n_missing = build_matrix_rows(
                    participant.participant_code, str(session.session_key), list(events), responses)
                rows.extend(s_rows)
                if n_missing:
                    stale += n_missing
                    self.stdout.write(self.style.WARNING(
                        f"  {participant.participant_code}: {n_missing} matrix click event(s) without "
                        f"itemId (older app.js) -- those rows can't be exported."))

            if not rows and not wanted:
                continue

            filepath = os.path.join(outdir, f"{participant.participant_code}-matrix.csv")
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
            f"Done: {total_files} file(s), {len(all_rows)} row(s) written to '{outdir}/'"))