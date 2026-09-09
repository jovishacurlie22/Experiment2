#!/usr/bin/env python3
"""
reorder_cognitive_load.py

Takes a *_question_score.xlsx workbook (the output of question_score.py)
and reorders it for survey delivery:

    - Modules are sorted ascending by the SUM of Cognitive Load Score
      across all their questions.
    - Sections within each module are sorted ascending by the SUM of
      Cognitive Load Score across their questions.
    - Questions within each section are sorted ascending by the MOTHER
      question's Cognitive Load Score. Each mother question and all of its
      follow-up questions move as one block; the block is positioned by the
      mother's score, and follow-ups always trail their mother.

Each question's own Cognitive Load Score is exactly what question_score.py
computed -- this script only reorders rows, it never recalculates or
averages scores across a group. Grouping affects ordering only.

Skip-group detection uses the Question Role assigned by question_score.py.
Module-level inclusion rows are explicitly classified as MOTHER questions,
while true question-level display-logic rows are FOLLOW-UP questions.
Consecutive follow-ups stay attached to the current mother, so a follow-up
can never be sorted ahead of its mother.

Usage:
    python reorder_cognitive_load.py
        Auto-discovers every *_question_score.xlsx under outputs/ (skipping
        already-reordered files) and writes a matching *_reordered.xlsx
        alongside each one.

    python reorder_cognitive_load.py --input X.xlsx --output Y.xlsx
        Reorders a single workbook.
"""

import argparse
import sys
import re
from pathlib import Path

import pandas as pd
import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.styles import Alignment, PatternFill

# Reuse styling/helpers from question_score.py so both workbooks use the
# same mother/follow-up classification and colours.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from question_score import (
    HEADER_FONT, HEADER_FILL, BODY_FONT, WRAP_ALIGN,
    MOTHER_FILL, FOLLOWUP_FILL, PHD_ADVISING_FILL, sanitize_sheet_name,
)

# Final workbook colours
INDEPENDENT_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")


# ---------------------------------------------------------------------------
# Skip-group detection
# ---------------------------------------------------------------------------

MODULE_LEVEL_SKIP_HINTS = [
    "module not selected", "module selected", "included if", "not included if",
]


def is_module_level_skip(skip_text):
    """
    True for skip/display text that's a MODULE-level inclusion rule (e.g.
    "Included if 'Financial Stress' module not selected") rather than a
    rule tied to the answer of the immediately preceding question. These
    aren't positionally anchored to an adjacent row, so they're excluded
    from adjacency-based grouping and reordered independently.
    """
    lower = skip_text.lower()
    return any(hint in lower for hint in MODULE_LEVEL_SKIP_HINTS)


def _clean_text(val):
    """
    Safely coerce a cell value to a stripped string. Rows loaded through
    pandas represent an empty Excel cell as float NaN, not None or "" --
    and NaN is truthy in Python, so a naive `str(val or "")` turns a blank
    cell into the literal 3-character string "nan" instead of "". That
    single bug was enough to make an unrelated question look like it had
    real Skip/Display Logic text and get swept into the wrong skip group.
    """
    if val is None:
        return ""
    if isinstance(val, float) and val != val:  # NaN != NaN is the classic isnan check
        return ""
    return str(val).strip()


def _split_parent_ids(value):
    """Parse the Parent Question # column written by question_score.py."""
    text = _clean_text(value)
    if not text:
        return []
    return [p.strip() for p in re.split(r"[;,]", text) if p.strip()]


def build_dependency_groups(mod_df):
    """Build dependency-connected blocks using Parent Question #.

    This is intentionally module-wide, not section-local. A follow-up may
    legitimately live in a different section from its mother (e.g. a
    satisfaction item depending on the counseling/therapy question). If we
    grouped only within a section, the reorder step could place the
    follow-up before its mother.

    Every connected component is kept intact. Rows inside a component retain
    their original survey order, while independent components can be sorted
    by their anchor/mother score.
    """
    rows = mod_df.to_dict("records")
    if not rows:
        return []

    # Original row position is the stable tie-breaker and the fallback
    # ordering for questions whose dependency cannot be resolved.
    qnum_to_pos = {
        _clean_text(row.get("Question #")): i for i, row in enumerate(rows)
    }

    adjacency = {i: set() for i in range(len(rows))}

    if "Parent Question #" in mod_df.columns:
        for child_idx, row in enumerate(rows):
            for parent_qnum in _split_parent_ids(row.get("Parent Question #")):
                parent_idx = qnum_to_pos.get(parent_qnum)
                if parent_idx is None or parent_idx == child_idx:
                    continue
                adjacency[child_idx].add(parent_idx)
                adjacency[parent_idx].add(child_idx)

    # Connected components.
    components = []
    seen = set()
    for start_idx in range(len(rows)):
        if start_idx in seen:
            continue
        stack = [start_idx]
        component = []
        seen.add(start_idx)
        while stack:
            idx = stack.pop()
            component.append(idx)
            for nxt in adjacency[idx]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        component.sort()
        components.append(component)

    groups = []
    for component in components:
        component_rows = [dict(rows[i]) for i in component]

        # Prefer the earliest Mother Question in the component. If the
        # component contains multiple roots (e.g. a child shown when either
        # vegan OR vegetarian is selected), use the earliest root. Otherwise
        # fall back to the first row.
        mother_positions = [
            i for i, r in zip(component, component_rows)
            if _clean_text(r.get("Question Role")) == "Mother Question"
        ]
        anchor_idx = min(mother_positions) if mother_positions else component[0]
        anchor_row = rows[anchor_idx]

        groups.append({
            "rows": component_rows,
            "anchor_score": pd.to_numeric(
                anchor_row.get("Cognitive Load Score"), errors="coerce"
            ),
            "original_pos": component[0],
            "sections": [r.get("Section") for r in component_rows],
        })

    for g in groups:
        if pd.isna(g["anchor_score"]):
            g["anchor_score"] = float("inf")

    return groups


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------

def reorder_module_df(mod_df, direction="ascending"):
    """
    Reorder one module while guaranteeing that every follow-up remains after
    its dependency chain.

    Dependency blocks are built BEFORE section sorting. This is important
    because a real follow-up can occur in a different section from its
    mother. The old implementation grouped only within each section, which
    could separate a follow-up from its mother after section sorting.

    Modules are still sorted by total cognitive-load sum by the caller, using
    the same `direction`. Within a module, dependency blocks are ordered by
    the mother's/anchor's score (ascending or descending per `direction`).
    Rows inside each dependency block always retain original order --
    direction only changes which block comes first, never the mother/
    follow-up sequence within a block.
    """
    groups = build_dependency_groups(mod_df)
    sign = 1 if direction == "ascending" else -1

    groups.sort(
        key=lambda g: (
            sign * float(g["anchor_score"]),
            g["original_pos"],
        )
    )

    all_rows = []
    group_id = 0
    for g in groups:
        has_dependency = len(g["rows"]) > 1
        if has_dependency:
            group_id += 1
        for r in g["rows"]:
            r = dict(r)
            r["Skip Group ID"] = group_id if has_dependency else None
            all_rows.append(r)

    out_df = pd.DataFrame(all_rows)
    out_df.insert(0, "Delivery Order", range(1, len(out_df) + 1))
    out_df = out_df.rename(columns={"Question #": "Source Question #"})

    module_sum = float(
        pd.to_numeric(mod_df["Cognitive Load Score"], errors="coerce")
        .fillna(0.0)
        .sum()
    )

    # Section summary remains available for the Overview sheet, but it is
    # based on the original section totals. Dependency ordering takes
    # precedence where a block crosses section boundaries.
    section_order = list(dict.fromkeys(mod_df["Section"].tolist()))
    section_summary = []
    for sec_idx, section in enumerate(section_order):
        sec_df = mod_df[mod_df["Section"] == section]
        sec_sum = float(
            pd.to_numeric(sec_df["Cognitive Load Score"], errors="coerce")
            .fillna(0.0)
            .sum()
        )
        section_summary.append((sec_sum, section))
    section_summary.sort(key=lambda x: (x[0], section_order.index(x[1])))

    return out_df, module_sum, section_summary


REORDERED_OUT_COLS = [
    "Delivery Order", "Source Question #", "Section", "Skip Group ID",
    "Question", "Question Role", "Parent Question #", "Response Categories", "Response Type",
    "Flesch Reading Ease", "FRE Band",
    "Blooms Score", "Response Option Complexity", "Response Option Complexity Score",
    "Time-span and Sensitivity", "QAS problems",
    "Cognitive Load Score", "Cognitive Load Band",
    "Notes", "Skip/Display Logic",
]

WIDE_COLS = {
    "Question": 55, "Response Categories": 40, "QAS problems": 22,
    "Notes": 40, "Skip/Display Logic": 40,
}


def style_reordered_sheet(ws, cols):
    for col_idx, col_name in enumerate(cols, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = WIDE_COLS.get(col_name, 16)

    role_col_idx = cols.index("Question Role") + 1 if "Question Role" in cols else None
    group_col_idx = cols.index("Skip Group ID") + 1 if "Skip Group ID" in cols else None

    for row_idx in range(2, ws.max_row + 1):
        role = str(ws.cell(row=row_idx, column=role_col_idx).value or "") if role_col_idx else ""
        for col_idx in range(1, len(cols) + 1):
            c = ws.cell(row=row_idx, column=col_idx)
            c.font = BODY_FONT
            c.alignment = WRAP_ALIGN
            if role == "Independent Question":
                c.fill = INDEPENDENT_FILL
            elif role == "Mother Question":
                c.fill = MOTHER_FILL
            elif role == "Follow-up Question":
                c.fill = FOLLOWUP_FILL
            elif role == "PhD Advising Question":
                c.fill = PHD_ADVISING_FILL

        if group_col_idx and ws.cell(row=row_idx, column=group_col_idx).value is not None:
            pass


def write_overview_sheet(writer, module_order):
    """module_order: list of (module_sum, module_name, section_summary) in final order."""
    rows = [("Delivery order of Modules and Sections (ascending Cognitive Load sum)", "", "")]
    rows.append(("", "", ""))
    rows.append(("Module", "Module Cognitive Load (sum)", ""))
    for m_sum, m_name, sections in module_order:
        rows.append((m_name, round(m_sum, 2), ""))
        for s_sum, s_name in sections:
            rows.append((f"  \u21b3 {s_name}", "", round(s_sum, 2)))
    df = pd.DataFrame(rows, columns=["Module / Section", "Module CL Sum", "Section CL Sum"])
    df.to_excel(writer, sheet_name="Overview", index=False)
    ws = writer.sheets["Overview"]
    for col_idx, width in enumerate((45, 20, 20), start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL


def write_readme_sheet(writer, total_questions, total_groups):
    notes = [
        ("Cognitive-Load Reordering -- Read Me", ""),
        ("", ""),
        ("Ordering rules", ""),
        ("Modules", "Sorted ascending by the SUM of Cognitive Load Score across all their questions."),
        ("Sections", "Sorted ascending by the SUM of Cognitive Load Score across their questions "
                      "(within their module)."),
        ("Questions", "Independent questions and mother questions are ordered by Cognitive Load Score within their section. "
                       "A mother and all of its follow-ups move as one block, positioned by the mother score."),
        ("Skip-logic groups", "Each mother question and its follow-ups move as one block; the block is positioned by the mother question's Cognitive Load Score. "
                               "in the group. A question's own Cognitive Load Score is never averaged "
                               "or altered by grouping -- grouping affects ORDER only."),
        ("", ""),
        ("Grouping detection", ""),
        ("How a group is formed",
         "The Parent Question # column from question_score.py defines explicit dependencies. A dependency block is built module-wide, so a follow-up can remain attached even when its mother and child are in different sections. The Question Role column remains authoritative for role labels."),
        ("Module-level inclusions",
         "Module-level inclusion rows are treated as mother questions, not follow-ups, and are evaluated against the selected HMS modules before scoring. "
         "'Financial Stress' module not selected\") is NOT treated as tying a row to its "
         "immediately preceding row -- those rows are reordered independently by their own score, "
         "since the rule isn't about a specific adjacent question."),
        ("Grouping caveats",
         "Dependency blocks are based on the explicit Parent Question # relationships produced by question_score.py. Rows whose source notes are missing or truncated may require a confirmed manual override in that script. Spot-check Parent Question # and Skip Group ID against the source survey before finalizing delivery order."),
        ("", ""),
        ("Columns added in this workbook", ""),
        ("Delivery Order", "Final sequential position within the module (1..N), reflecting the "
                            "module/section/question sort above."),
        ("Source Question #", "The question's original number from the scored workbook, kept for "
                               "traceability back to the source survey."),
        ("Skip Group ID", "Shared integer for every row in the same skip-logic group (blank for "
                           "standalone questions). Rows sharing an ID must stay adjacent and in this "
                           "relative order in the delivered survey."),
        ("", ""),
        ("Totals", ""),
        ("Total questions reordered", str(total_questions)),
        ("Total skip-logic groups (2+ rows)", str(total_groups)),
    ]
    df = pd.DataFrame(notes, columns=["Item", "Detail"])
    df.to_excel(writer, sheet_name="Read Me", index=False)
    ws = writer.sheets["Read Me"]
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 100
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def reorder_workbook(input_path, output_path, direction="ascending"):
    assert direction in ("ascending", "descending"), direction
    sign = 1 if direction == "ascending" else -1

    input_path = Path(input_path)
    src_wb = openpyxl.load_workbook(input_path, data_only=True)
    module_sheets = [sn for sn in src_wb.sheetnames if sn not in ("Read Me", "Overview")]

    module_results = []  # (module_sum, module_name, out_df, section_summary)
    total_groups = 0
    total_questions = 0

    for sheet_name in module_sheets:
        ws = src_wb[sheet_name]
        headers = [c.value for c in next(ws.iter_rows(max_row=1))]
        rows = [dict(zip(headers, r)) for r in ws.iter_rows(min_row=2, values_only=True)]
        if not rows:
            continue
        mod_df = pd.DataFrame(rows)
        mod_df["Cognitive Load Score"] = pd.to_numeric(mod_df["Cognitive Load Score"], errors="coerce").fillna(0.0)

        # Module name for display: use the Module column if present (it's
        # dropped from per-sheet output by question_score.py), else the
        # sheet name.
        module_name = mod_df["Module"].iloc[0] if "Module" in mod_df.columns else sheet_name

        out_df, module_sum, section_summary = reorder_module_df(mod_df, direction=direction)
        total_groups += out_df["Skip Group ID"].dropna().nunique()
        total_questions += len(out_df)
        module_results.append((module_sum, module_name, sheet_name, out_df, section_summary))

    module_results.sort(key=lambda t: sign * t[0])  # stable

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        used_names = set()
        overview_module_order = []

        for module_sum, module_name, orig_sheet_name, out_df, section_summary in module_results:
            sheet_name = sanitize_sheet_name(orig_sheet_name, used_names)
            out_df.to_excel(writer, sheet_name=sheet_name, index=False, columns=REORDERED_OUT_COLS)
            ws = writer.book[sheet_name]
            ws.freeze_panes = "A2"
            style_reordered_sheet(ws, REORDERED_OUT_COLS)
            overview_module_order.append((module_sum, module_name, section_summary))

        write_overview_sheet(writer, overview_module_order)
        write_readme_sheet(writer, total_questions, total_groups)

        # Move Overview + Read Me to the front for discoverability.
        book = writer.book
        front = ["Read Me", "Overview"]
        rest = [sn for sn in book.sheetnames if sn not in front]
        book._sheets = [book[sn] for sn in front + rest]

    print(f"{input_path.name} -> {output_path.name}")
    print(f"  Modules: {len(module_results)}  Questions: {total_questions}  Skip groups: {total_groups}")
    print(f"  Module order ({direction} Cognitive Load sum):")
    for module_sum, module_name, _sheet, _df, _sec in module_results:
        print(f"    {module_name:45s} sum={module_sum:.2f}")


def parse_args():
    project_root = Path(__file__).resolve().parent.parent
    default_outputs = project_root / "outputs"

    parser = argparse.ArgumentParser(
        description="Reorder a *_question_score.xlsx workbook by cognitive load "
        "(ascending or descending), keeping skip-logic groups together."
    )
    parser.add_argument("--input", default=None, help="Single scored workbook to reorder.")
    parser.add_argument("--output", default=None, help="Output path (single-file mode).")
    parser.add_argument(
        "--direction", choices=["ascending", "descending"], default="ascending",
        help="Sort modules/dependency-blocks by ascending (default) or descending "
             "cognitive load. Use descending to build the counterbalance group.",
    )
    args = parser.parse_args()
    args.default_outputs = default_outputs
    return args


def main():
    args = parse_args()

    if args.input:
        output = args.output or (Path(args.input).parent / f"{Path(args.input).stem}_reordered.xlsx")
        reorder_workbook(args.input, output, direction=args.direction)
        return

    candidates = sorted(args.default_outputs.glob("*_question_score.xlsx")) + \
                 sorted(args.default_outputs.glob("*question_score.xlsx"))
    candidates = sorted(set(candidates))
    candidates = [c for c in candidates if "_reordered" not in c.stem]

    if not candidates:
        raise FileNotFoundError(
            f"No *_question_score.xlsx files found under {args.default_outputs}. "
            "Pass --input explicitly to reorder a single file."
        )

    for input_path in candidates:
        suffix = "_reordered" if args.direction == "ascending" else "_reordered_desc"
        output_path = input_path.parent / f"{input_path.stem}{suffix}.xlsx"
        reorder_workbook(input_path, output_path, direction=args.direction)


if __name__ == "__main__":
    main()