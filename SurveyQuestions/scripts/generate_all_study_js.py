"""
Generate study_schema.js + study_config.js (STUDY_CONFIG_ASC / STUDY_CONFIG_DESC)
for Demographics + HMS + MECAMH, compatible with the engine as patched per
PATCHES.md (nested sectionOrder/questionOrder ordering; in/notIn/includes/
excludes/includesAny/excludesAny/any condition vocabulary; array showIf = OR).

Demographics: no cognitive-load scoring exists for it and none is wanted --
it's always first, with one fixed section/question order used in both ASC
and DESC (it does not flip between directions).

HMS: content + ASC ordering from ../outputs/hms_question_score_reordered.xlsx
(reorder_cognitive_load.py output, ascending direction). DESC is derived
in-code by reversing the ASC order (see desc_module_order etc. below) --
no separate outputs_asc.xlsx/outputs_desc.xlsx files are read. Ordering is
expressed as sectionOrder (sections ranked by their own best/anchor score) +
questionOrder (questions within a section ranked by score, mother-then-
followups kept adjacent) -- the nested mechanism the installed engine
actually reads, not the flat moduleQuestionOrder override from my earlier
(never-installed) draft.

MECAMH: content + ASC ordering from MECAMH_question_score_reordered.xlsx.
No DESC file was provided, so DESC is derived the same way generate_config.py
derives it for every other module: module order reversed, and each module's
own section order reversed (question order within a section unchanged --
there's only ever one row per section here anyway).
"""
import json
import re

import openpyxl


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(text).strip().lower())
    return re.sub(r"-+", "-", text).strip("-")


def clean(v):
    return "" if v is None else str(v).strip()


def js_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def normalise(text):
    text = str(text).replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


# ---------------------------------------------------------------------------
# Response-category parsing -- two formats seen across the three sources:
#   "1=Label 2=Label ..."        (HMS/MECAMH-style, code=label)
#   "Label | Label | Label"      (pipe-separated, no codes -- Demographics/MECAMH)
# ---------------------------------------------------------------------------

def parse_response_categories(text):
    text = clean(text)
    if not text:
        return []
    if "|" in text and "=" not in text.split("|")[0]:
        parts = [p.strip() for p in text.split("|")]
        options = []
        for i, label in enumerate(parts, start=1):
            if not label:
                continue
            is_other = bool(re.search(r"\(.*specify.*\)", label, re.IGNORECASE))
            options.append({"code": str(i), "label": label, "exclusive": False, "otherFreeText": is_other})
        return options

    parts = re.split(r"(?=\b\d+\s*=)", text)
    options = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d+)\s*=\s*(.*)", part, re.DOTALL)
        if not m:
            continue
        code, label = m.group(1), m.group(2).strip()
        is_mutex = "[mutually exclusive]" in label.lower()
        label_clean = re.sub(r"\[mutually exclusive\]", "", label, flags=re.IGNORECASE).strip()
        is_other = bool(re.search(r"\bother\b.*\(.*specify.*\)", label_clean, re.IGNORECASE))
        options.append({"code": code, "label": label_clean, "exclusive": is_mutex, "otherFreeText": is_other})
    return options


def find_option_code(option_text, options):
    option_text = option_text.strip()
    code_match = re.match(r"\s*(\d+)\s*=", option_text)
    if code_match:
        code = code_match.group(1)
        for opt in options:
            if opt["code"] == code:
                return code
    norm_target = normalise(option_text)
    if len(norm_target) >= 3:
        for opt in options:
            norm_label = normalise(opt["label"])
            if norm_target == norm_label or norm_target in norm_label or norm_label in norm_target:
                return opt["code"]
    return None


def split_matrix_stem_and_items(question_text):
    """Split a matrix question's raw bundled text into (stem, items) in a
    single pass so the two can never duplicate each other."""
    text = question_text.strip()

    # Pattern 1: explicit numbered list, one item per line (MECAMH-style).
    numbered_lines = re.findall(r"^\s*(\d+)\.\s+(.+?)\s*$", text, re.MULTILINE)
    if len(numbered_lines) >= 2:
        first_num_pos = re.search(r"^\s*1\.\s+", text, re.MULTILINE)
        stem = text[:first_num_pos.start()].strip() if first_num_pos else ""
        return stem, [item for _num, item in numbered_lines]

    # Pattern 2: ellipsis-separated bundle -- first part is the intro.
    ellipsis_parts = re.split(r"[\u2026]+|\.\.\.+", text)
    ellipsis_parts = [p.strip(" .") for p in ellipsis_parts if p.strip(" .")]
    if len(ellipsis_parts) >= 3:
        return ellipsis_parts[0], ellipsis_parts[1:]

    # Pattern 3: intro clause ending in "?" or ":" followed by bundled
    # sentences (e.g. "...following: How often...? How often...?").
    # NOTE: "?" added to the lookbehind class below -- this is what was
    # missing, causing the Loneliness question to fail this split entirely.
    stem_match = re.match(r"^(.*?[?:])\s+(.*)$", text, re.DOTALL)
    if stem_match:
        remainder = stem_match.group(2)
        parts = re.split(r"(?<=[a-z\)\.,\?])\s+(?=[A-Z])", remainder)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) >= 2:
            return stem_match.group(1), parts

    # Pattern 4: no "?"/":" divider at all (e.g. "Below are 8 statements...
    # Using the 1-7 scale... I lead a purposeful life. My social...").
    # Intro text may itself span more than one sentence, so use a declared
    # count ("8 statements") when present to know how many trailing
    # sentences are real rows vs. lead-in instructions.
    parts = re.split(r"(?<=[a-z\)\.,])\s+(?=[A-Z])", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        count_match = re.search(r"\b(\d+)\s+(?:statements|items|questions)\b", text, re.IGNORECASE)
        if count_match:
            n = int(count_match.group(1))
            if 0 < n < len(parts):
                return " ".join(parts[:-n]), parts[-n:]
        return parts[0], parts[1:]

    # Truly nothing to split on -- don't fabricate a duplicate; leave the
    # stem empty and flag for manual review instead.
    return "", [text]

CLAUSE_RE = re.compile(
    r'["\u201c]([^"\u201c\u201d]+)["\u201d]\s+is\s+(not\s+)?selected\s+for\s+'
    r'["\u201c]([^"\u201c\u201d]+)["\u201d]',
    re.IGNORECASE,
)

# Excel workbook sheet-tab names are hard-capped at 31 characters, and both
# hms_survey.xlsx and mecamhsurvey.xlsx have module sheets that got silently
# truncated at save time -- the missing text is gone from the tab itself, so
# it has to be restored here rather than recovered from any file. Keyed by
# the exact (truncated) sheet name as it appears in the workbook today.
# TODO: confirm "Mental Health Service Utilizati" -> exact intended full
# title with the source questionnaire (guessing "Mental Health Service
# Utilization" below -- please correct if the real title differs/continues).
MODULE_TITLE_OVERRIDES = {
    "Mental Health Service Utilizati": "Mental Health Service Utilization",
    "Coping Responses and Climate Ch": "Coping Responses and Climate Change",
}


def guess_type(question_text, options, is_matrix):
    if is_matrix:
        return "matrix"
    if not options:
        return "text"
    if len(options) == 2:
        labels = {normalise(o["label"]) for o in options}
        if labels & {"yes", "no"}:
            return "binary"
    if len(options) > 2 and "select all that apply" in question_text.lower():
        return "multi"
    return "nominal"


def load_sheet_rows(path, sheet_name):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name]
    headers = [c.value for c in next(ws.iter_rows(max_row=1))]
    return headers, [dict(zip(headers, r)) for r in ws.iter_rows(min_row=2, values_only=True)]


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------

def build_demographics():
    """No scoring exists (or is wanted) for this sheet -- fixed source
    order, one section per row (as given), always module id 'demographics'."""
    wb = openpyxl.load_workbook("../csvs/Demographics.xlsx", data_only=True)
    ws = wb["Demographics"]
    headers = [c.value for c in next(ws.iter_rows(max_row=1))]
    rows = [dict(zip(headers, r)) for r in ws.iter_rows(min_row=2, values_only=True)]

    module_id = "demographics"
    sections = []
    section_order = []
    review_notes = []

    for i, row in enumerate(rows, start=1):
        # Row 5 has "Department" in the Module column instead of "Demographics"
        # (source typo) -- every row on this sheet belongs to Demographics
        # regardless of that column's value.
        sec_name = clean(row.get("Section")) or "General"
        qid = f"{module_id}-q{i}"
        question_text = clean(row.get("Question"))
        options = parse_response_categories(row.get("Response Categories"))
        qtype = guess_type(question_text, options, False)
        if not options and qtype == "text" and re.search(r"\bold\b|\bage\b", question_text, re.IGNORECASE):
            qtype = "numeric"

        question = {"id": qid, "type": qtype, "stem": question_text, "options": options}
        sections.append((sec_name, question))
        if sec_name not in section_order:
            section_order.append(sec_name)

    sections_by_name = {}
    for sec_name, q in sections:
        sections_by_name.setdefault(sec_name, []).append(q)

    module = {
        "id": module_id,
        "title": "Demographics",
        "sections": [
            {"id": f"{module_id}-{slugify(name)}", "title": name, "questions": sections_by_name[name]}
            for name in section_order
        ],
    }
    # Fixed order for both directions; only section order flips (handled by caller).
    return module, section_order, review_notes


def build_hms_content():
    wb = openpyxl.load_workbook("../outputs/hms_question_score_reordered.xlsx", data_only=True)
    module_sheets = [sn for sn in wb.sheetnames if sn not in ("Read Me", "Overview")]

    modules = []
    review_notes = []

    for sheet_name in module_sheets:
        headers, rows = load_sheet_rows("../outputs/hms_question_score_reordered.xlsx", sheet_name)
        if not rows:
            continue
        module_id = f"hms-{slugify(sheet_name)}"

        qnum_to_id = {}
        qnum_to_row = {}
        for row in rows:
            qnum = clean(row.get("Source Question #"))
            if qnum:
                qid = f"{module_id}-q{qnum}"
                qnum_to_id[qnum] = qid
                qnum_to_row[qnum] = row

        section_order = []
        sections_by_name = {}
        for row in rows:
            sec_name = clean(row.get("Section")) or "General"
            if sec_name not in sections_by_name:
                sections_by_name[sec_name] = []
                section_order.append(sec_name)

        # Track each question's own type (needed to pick in/notIn vs
        # includes/excludes when a showIf clause references it as a parent).
        qid_to_type = {}

        for row in rows:
            qnum = clean(row.get("Source Question #"))
            qid = qnum_to_id.get(qnum)
            if qid is None:
                continue
            sec_name = clean(row.get("Section")) or "General"
            question_text = clean(row.get("Question"))
            options = parse_response_categories(row.get("Response Categories"))
            is_matrix = "matrix" in clean(row.get("Response Type")).lower()
            qtype = guess_type(question_text, options, is_matrix)
            qid_to_type[qid] = qtype

            question = {
                "id": qid, "type": qtype, "stem": question_text, "options": options,
                "_qnum": qnum, "_role": clean(row.get("Question Role")),
                "_score": row.get("Cognitive Load Score") or 0,
                "_parent_field": clean(row.get("Parent Question #")),
                "_raw_skip": clean(row.get("Skip/Display Logic")),
                "_parent_ids": [qnum_to_id[p.strip()] for p in clean(row.get("Parent Question #")).split(";")
                                if p.strip() and p.strip() in qnum_to_id],
            }
            if is_matrix:
                stem, items = split_matrix_stem_and_items(question_text)
                question["stem"] = stem
                question["matrixItems"] = [{"id": f"{qid}-i{i}", "label": item} for i, item in enumerate(items)]
                review_notes.append((qid, "matrix_split", f"{len(items)} items parsed" + ("" if stem else " — EMPTY STEM, needs manual review")))

            sections_by_name[sec_name].append(question)

        # showIf resolution, now operator-aware based on the PARENT's type.
        for sec_name, questions in sections_by_name.items():
            for q in questions:
                if q["_role"] != "Follow-up Question" or not q["_parent_field"]:
                    continue
                clauses = CLAUSE_RE.findall(q["_raw_skip"])
                show_if_clauses = []
                for parent_num in [p.strip() for p in q["_parent_field"].split(";") if p.strip()]:
                    parent_id = qnum_to_id.get(parent_num)
                    if parent_id is None:
                        review_notes.append((q["id"], "unresolved_parent", f"parent Q#{parent_num} not in this module"))
                        continue
                    parent_row = qnum_to_row[parent_num]
                    parent_options = parse_response_categories(parent_row.get("Response Categories"))
                    parent_text = clean(parent_row.get("Question"))
                    parent_type = qid_to_type.get(parent_id, "nominal")

                    matched = []
                    for option_text, negation, parent_phrase in clauses:
                        norm_phrase = normalise(parent_phrase)
                        norm_parent = normalise(parent_text)
                        if len(norm_phrase) >= 15 and (norm_phrase in norm_parent or norm_parent in norm_phrase):
                            matched.append((option_text, bool(negation)))

                    if not matched:
                        review_notes.append((q["id"], "needs_review", f"no clause matched parent Q#{parent_num}"))
                        show_if_clauses.append({"questionId": parent_id, "any": True})
                        continue

                    negations = {n for _o, n in matched}
                    if len(negations) != 1:
                        review_notes.append((q["id"], "needs_review", f"mixed selected/not-selected vs Q#{parent_num}"))
                        show_if_clauses.append({"questionId": parent_id, "any": True})
                        continue

                    codes = []
                    unresolved = False
                    for option_text, _neg in matched:
                        code = find_option_code(option_text, parent_options)
                        if code is None:
                            unresolved = True
                        else:
                            codes.append(code)
                    if unresolved or not codes:
                        review_notes.append((q["id"], "needs_review", f"couldn't resolve option code(s) vs Q#{parent_num}"))
                        show_if_clauses.append({"questionId": parent_id, "any": True})
                        continue

                    is_multi_parent = parent_type == "multi"
                    negated = negations == {True}
                    if is_multi_parent:
                        key = "excludesAny" if negated else "includesAny"
                    else:
                        key = "notIn" if negated else "in"
                    show_if_clauses.append({"questionId": parent_id, key: codes})

                q["showIf"] = show_if_clauses if len(show_if_clauses) > 1 else (show_if_clauses[0] if show_if_clauses else None)
                if q["showIf"]:
                    root_num = q["_parent_field"].split(";")[0].strip()
                    seen = set()
                    while True:
                        root_row = qnum_to_row.get(root_num)
                        if root_row is None:
                            break
                        rp = clean(root_row.get("Parent Question #")).split(";")[0].strip()
                        if not rp or rp in seen:
                            break
                        seen.add(rp)
                        root_num = rp
                    q["group"] = qnum_to_id.get(root_num, qid)

        modules.append({
            "id": module_id,
            "title": MODULE_TITLE_OVERRIDES.get(sheet_name, sheet_name),
            "sections": [
                {"id": f"{module_id}-{slugify(name)}", "title": name, "questions": sections_by_name[name]}
                for name in section_order if sections_by_name[name]
            ],
        })

    return modules, review_notes


def build_mecamh_content():
    path = "../outputs/MECAMH_question_score_reordered.xlsx"
    wb = openpyxl.load_workbook(path, data_only=True)
    module_sheets = [sn for sn in wb.sheetnames if sn not in ("Read Me", "Overview")]

    modules = []
    review_notes = []

    for sheet_name in module_sheets:
        headers, rows = load_sheet_rows(path, sheet_name)
        if not rows:
            continue
        module_id = f"mecamh-{slugify(sheet_name)}"

        section_order = []
        sections_by_name = {}
        for row in rows:
            sec_name = clean(row.get("Section")) or "General"
            if sec_name not in sections_by_name:
                sections_by_name[sec_name] = []
                section_order.append(sec_name)

        for row in rows:
            qnum = clean(row.get("Source Question #"))
            qid = f"{module_id}-q{qnum}"
            sec_name = clean(row.get("Section")) or "General"
            question_text = clean(row.get("Question"))
            options = parse_response_categories(row.get("Response Categories"))
            is_matrix = "matrix" in clean(row.get("Response Type")).lower()
            qtype = guess_type(question_text, options, is_matrix)

            question = {
                "id": qid, "type": qtype, "stem": question_text, "options": options,
                "_qnum": qnum, "_score": row.get("Cognitive Load Score") or 0,
            }
            if is_matrix:
                stem, items = split_matrix_stem_and_items(question_text)
                question["stem"] = stem
                question["matrixItems"] = [{"id": f"{qid}-i{i}", "label": item} for i, item in enumerate(items)]
                review_notes.append((qid, "matrix_split", f"{len(items)} items parsed" + ("" if stem else " — EMPTY STEM, needs manual review")))

            sections_by_name[sec_name].append(question)

        modules.append({
            "id": module_id,
            "title": MODULE_TITLE_OVERRIDES.get(sheet_name, sheet_name),
            "sections": [
                {"id": f"{module_id}-{slugify(name)}", "title": name, "questions": sections_by_name[name]}
                for name in section_order if sections_by_name[name]
            ],
            "_ascending_delivery_order": [int(r.get("Delivery Order")) for r in rows],
        })

    return modules, review_notes


# ---------------------------------------------------------------------------
# Ordering: nested sectionOrder[moduleId] / questionOrder[sectionId], matching
# the ALREADY-INSTALLED engine (see PATCHES.md) -- not a flat override.
# ---------------------------------------------------------------------------

def order_module_by_direction(module, direction):
    """direction: 'ascending' or 'descending'. Sections ranked by their own
    anchor score; questions within a section grouped (mother+followups kept
    adjacent) and groups ranked by anchor score. Works for both HMS (has
    real Question Role/parent chains) and MECAMH (no follow-ups -- every
    question is its own one-member group) since both carry a numeric
    `_score` per question; `_role` defaults to Independent when absent."""
    sign = 1 if direction == "ascending" else -1

    section_scores = []
    for sec in module["sections"]:
        qs = sec["questions"]
        anchors = [q for q in qs if q.get("_role", "Independent Question") != "Follow-up Question"]
        best = min((q["_score"] for q in anchors), default=min((q["_score"] for q in qs), default=0))
        section_scores.append((sign * best, sec))
    section_scores.sort(key=lambda t: t[0])
    section_order = [sec["id"] for _score, sec in section_scores]

    question_order = {}
    for _score, sec in section_scores:
        qs = sec["questions"]
        id_to_q = {q["id"]: q for q in qs}

        # Union-find over question ids in this section, so a follow-up with
        # multiple parents (e.g. two sibling checkboxes that both trigger the
        # same next question) keeps ALL of them adjacent to it -- not just
        # whichever parent happened to be picked for the cosmetic "group" badge.
        parent_uf = {q["id"]: q["id"] for q in qs}

        def find(x):
            while parent_uf[x] != x:
                parent_uf[x] = parent_uf[parent_uf[x]]
                x = parent_uf[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent_uf[ra] = rb

        for q in qs:
            for pid in q.get("_parent_ids", []) or []:
                if pid in id_to_q:
                    union(q["id"], pid)

        groups = {}
        order = []
        for q in qs:
            key = find(q["id"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(q)
        for key in groups:
            groups[key].sort(key=lambda q: 0 if q.get("_role", "Independent Question") != "Follow-up Question" else 1)
        scored = []
        for key in order:
            members = groups[key]
            anchor_score = next(
                (q["_score"] for q in members if q.get("_role", "Independent Question") != "Follow-up Question"),
                members[0]["_score"],
            )
            scored.append((sign * anchor_score, key))
        scored.sort(key=lambda t: t[0])
        flat_ids = []
        for _s, key in scored:
            flat_ids.extend(q["id"] for q in groups[key])
        question_order[sec["id"]] = flat_ids

    return section_order, question_order


def module_score(module):
    # Ranked by SUM of question scores, matching reorder_cognitive_load.py's
    # own "Module order (ascending Cognitive Load sum)" convention -- HMS and
    # MECAMH modules are merged and sorted together on this single scale, so
    # neither source is ever grouped separately from the other.
    all_scores = [q["_score"] for sec in module["sections"] for q in sec["questions"]]
    return sum(all_scores)


# ---------------------------------------------------------------------------
# JS emission
# ---------------------------------------------------------------------------

def emit_option(opt):
    parts = [f'{{ value: {js_string(opt["code"])}, label: {js_string(opt["label"])}']
    if opt["exclusive"]:
        parts.append(", exclusive: true")
    if opt["otherFreeText"]:
        parts.append(", otherFreeText: true")
    parts.append(" }")
    return "".join(parts)


def emit_show_if(cond):
    def one(c):
        if c.get("any"):
            return f'{{ questionId: {js_string(c["questionId"])}, any: true }}'
        for key in ("in", "notIn", "includes", "excludes", "includesAny", "excludesAny"):
            if key in c:
                val = c[key]
                if isinstance(val, list):
                    inner = ", ".join(js_string(x) for x in val)
                    return f'{{ questionId: {js_string(c["questionId"])}, {key}: [{inner}] }}'
                return f'{{ questionId: {js_string(c["questionId"])}, {key}: {js_string(val)} }}'
        return f'{{ questionId: {js_string(c["questionId"])}, any: true }}'
    if isinstance(cond, list):
        return "[" + ", ".join(one(c) for c in cond) + "]"
    return one(cond)


def emit_question(q, review_by_qid, indent="        "):
    lines = [f'{indent}{{']
    lines.append(f'{indent}  id: {js_string(q["id"])},')
    lines.append(f'{indent}  type: {js_string(q["type"])},')
    lines.append(f'{indent}  stem: {js_string(q["stem"])},')

    for kind, detail in review_by_qid.get(q["id"], []):
        lines.append(f'{indent}  // TODO-VERIFY ({kind}): {detail}')

    if q.get("matrixItems"):
        lines.append(f'{indent}  items: [')
        for item in q["matrixItems"]:
            lines.append(f'{indent}    {{ id: {js_string(item["id"])}, label: {js_string(item["label"])} }},')
        lines.append(f'{indent}  ],')

    if q["options"]:
        lines.append(f'{indent}  options: [')
        for opt in q["options"]:
            lines.append(f'{indent}    {emit_option(opt)},')
        lines.append(f'{indent}  ],')
    else:
        lines.append(f'{indent}  options: [],')

    if q.get("showIf"):
        lines.append(f'{indent}  showIf: {emit_show_if(q["showIf"])},')
    if q.get("group"):
        lines.append(f'{indent}  group: {js_string(q["group"])},')

    lines.append(f'{indent}}},')
    return "\n".join(lines)


def emit_schema_js(all_modules, review_notes):
    review_by_qid = {}
    for qid, kind, detail in review_notes:
        if kind == "matrix_split":
            continue
        review_by_qid.setdefault(qid, []).append((kind, detail))

    todo_count = sum(1 for n in review_notes if n[1] != "matrix_split")

    out = []
    out.append("/* AUTO-GENERATED by generate_all_study_js.py from Demographics.xlsx,")
    out.append("   the HMS scoring pipeline output, and MECAMH_question_score_reordered.xlsx.")
    out.append("   Do not hand-edit content here except to resolve a `// TODO-VERIFY` comment --")
    out.append("   regenerate from the workbooks instead for any other change.")
    out.append(f"   {todo_count} TODO-VERIFY comment(s) below need a human check before this")
    out.append("   schema is used with real participants. Ordering lives in study_config.js,")
    out.append("   not here. */")
    out.append("")
    out.append("window.STUDY_MODULES = [")
    for mod in all_modules:
        out.append("  {")
        out.append(f'    id: {js_string(mod["id"])},')
        out.append('    kind: "standard",')
        out.append(f'    title: {js_string(mod["title"])},')
        out.append("    sections: [")
        for sec in mod["sections"]:
            out.append("      {")
            out.append(f'        id: {js_string(sec["id"])},')
            out.append(f'        title: {js_string(sec["title"])},')
            out.append("        questions: [")
            for q in sec["questions"]:
                out.append(emit_question(q, review_by_qid))
            out.append("        ]")
            out.append("      },")
        out.append("    ]")
        out.append("  },")
    out.append("];")
    return "\n".join(out)


def emit_config_js(global_name, other_label, module_order, section_order_by_module, question_order_by_section):
    out = []
    out.append(f"/* AUTO-GENERATED by generate_all_study_js.py -- {other_label} cognitive-load ordering.")
    out.append("   Regenerate from the workbooks rather than hand-editing.")
    out.append(f"   Loaded as window.{global_name}; app.js picks between this and its sibling")
    out.append("   based on odd/even participant id (see the app.js patch in PATCHES.md). */")
    out.append("")
    out.append(f"window.{global_name} = {{")
    out.append("  activeModuleIds: [")
    for mid in module_order:
        out.append(f"    {js_string(mid)},")
    out.append("  ],")
    out.append("")
    out.append("  sectionOrder: {")
    for mid in module_order:
        secs = section_order_by_module.get(mid, [])
        if len(secs) <= 1:
            continue
        out.append(f"    {js_string(mid)}: [")
        for sid in secs:
            out.append(f"      {js_string(sid)},")
        out.append("    ],")
    out.append("  },")
    out.append("")
    out.append("  questionOrder: {")
    for sid, qids in question_order_by_section.items():
        if len(qids) <= 1:
            continue
        out.append(f"    {js_string(sid)}: [")
        for qid in qids:
            out.append(f"      {js_string(qid)},")
        out.append("    ],")
    out.append("  },")
    out.append("};")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    demo_module, demo_section_order, demo_review = build_demographics()
    hms_modules, hms_review = build_hms_content()
    mecamh_modules, mecamh_review = build_mecamh_content()

    all_modules = [demo_module] + hms_modules + mecamh_modules
    all_review = demo_review + hms_review + mecamh_review

    # Master ordering of non-Demographics modules by aggregate score.
    scored_modules = sorted(hms_modules + mecamh_modules, key=module_score)
    asc_module_order = ["demographics"] + [m["id"] for m in scored_modules]
    desc_module_order = ["demographics"] + [m["id"] for m in reversed(scored_modules)]

    # Demographics keeps one fixed section/question order in both ASC and
    # DESC -- it isn't cognitive-load-scored, so there's nothing principled
    # to flip; only the scored HMS/MECAMH modules after it differ by
    # direction.
    section_order_asc = {"demographics": demo_section_order}
    section_order_desc = {"demographics": demo_section_order}
    question_order_asc = {}
    question_order_desc = {}

    for mod in scored_modules:
        sec_asc, q_asc = order_module_by_direction(mod, "ascending")
        sec_desc, q_desc = order_module_by_direction(mod, "descending")
        section_order_asc[mod["id"]] = sec_asc
        section_order_desc[mod["id"]] = sec_desc
        question_order_asc.update(q_asc)
        question_order_desc.update(q_desc)

    with open("study_schema.js", "w", encoding="utf-8") as f:
        f.write(emit_schema_js(all_modules, all_review))

    with open("study_config.js", "w", encoding="utf-8") as f:
        asc_js = emit_config_js("STUDY_CONFIG_ASC", "ascending", asc_module_order, section_order_asc, question_order_asc)
        desc_js = emit_config_js("STUDY_CONFIG_DESC", "descending", desc_module_order, section_order_desc, question_order_desc)
        default_js = "// Back-compat default before a participant id is known (e.g. the\n// \"up to N questions\" estimate on the instructions screen).\nwindow.STUDY_CONFIG = window.STUDY_CONFIG_ASC;\n"
        f.write(asc_js + "\n\n" + desc_js + "\n\n" + default_js)

    total_q = sum(len(s["questions"]) for m in all_modules for s in m["sections"])
    print(f"Modules: {len(all_modules)}  Questions: {total_q}")
    kinds = {}
    for _qid, kind, _detail in all_review:
        kinds[kind] = kinds.get(kind, 0) + 1
    for kind, count in kinds.items():
        print(f"  {kind}: {count}")
    print("ASC module order:", asc_module_order)
    print("DESC module order:", desc_module_order)