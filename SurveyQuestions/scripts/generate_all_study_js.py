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
from difflib import SequenceMatcher

import openpyxl

from pathlib import Path

# Script lives in SurveyQuestions/scripts/; static JS lives at
# Experiment2/study/static/study/js/ -- write straight there so there's
# no manual copy step left to forget.
STATIC_JS_DIR = Path(__file__).resolve().parent.parent.parent / "study" / "static" / "study" / "js"

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


def phrase_matches_parent(phrase, parent_text):
    """Validates a clause's quoted parent-phrase against the ACTUAL text of
    the parent question already identified via Parent Question #. Strict
    substring containment is tried first (cheap, zero false-positive risk);
    when that fails, falls back to the same conservative fuzzy-match
    tolerance question_score.py's own _find_parent_question_indices()
    already uses for its broader search (SequenceMatcher ratio >= 0.70 and
    word-overlap >= 0.55) -- ordinary paraphrase drift between how a Notes
    cell describes a question and the question's own wording (typos,
    singular/plural, an added qualifier word, a reworded clause at the end)
    shouldn't sink an otherwise-correct, already-narrowed clause match.
    Since this only validates a SPECIFIC candidate already resolved by
    Parent Question #, not a search across the whole module, the risk of a
    false positive here is much lower than in that broader search."""
    norm_phrase = normalise(phrase)
    norm_parent = normalise(parent_text)
    if len(norm_phrase) >= 15 and (norm_phrase in norm_parent or norm_parent in norm_phrase):
        return True
    if not norm_phrase or not norm_parent:
        return False
    ratio = SequenceMatcher(None, norm_phrase, norm_parent).ratio()
    phrase_tokens = set(norm_phrase.split())
    overlap = len(phrase_tokens & set(norm_parent.split())) / max(1, len(phrase_tokens))
    return ratio >= 0.70 and overlap >= 0.55


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
        # Exact match first, across ALL options, before falling back to
        # substring containment. A single containment pass in source order
        # picks whichever option happens to come first in the list -- e.g.
        # option_text "Agree" would match "Strongly agree" (contains
        # "agree") before ever reaching the actual "Agree" option, silently
        # resolving to the wrong code. Two passes fixes that without
        # weakening the fuzzy fallback for genuinely partial phrasing.
        for opt in options:
            if norm_target == normalise(opt["label"]):
                return opt["code"]
        for opt in options:
            norm_label = normalise(opt["label"])
            if norm_target in norm_label or norm_label in norm_target:
                return opt["code"]
        # Fuzzy fallback: pick the BEST-scoring option (argmax across the
        # whole list, not first-match) so paraphrase drift in how the Notes
        # column restates an option's wording doesn't sink an otherwise-
        # clear match. Same tolerance phrase_matches_parent() applies to
        # parent-question text, applied here to option labels. Argmax
        # instead of first-match-past-threshold avoids the same
        # order-dependence bug the exact-match pass above was fixed for.
        best_code, best_ratio = None, 0.0
        target_tokens = set(norm_target.split())
        for opt in options:
            norm_label = normalise(opt["label"])
            ratio = SequenceMatcher(None, norm_target, norm_label).ratio()
            overlap = len(target_tokens & set(norm_label.split())) / max(1, len(target_tokens))
            if ratio >= 0.6 and overlap >= 0.5 and ratio > best_ratio:
                best_code, best_ratio = opt["code"], ratio
        if best_code is not None:
            return best_code
    return None


BOILERPLATE_PARENTHETICAL_RE = re.compile(r"^\([^)]*\)$")


def _strip_boilerplate_lead(stem, parts):
    """A leading part that's nothing but a parenthetical instruction --
    "(Select all that apply)", "(Select all that apply.)" -- isn't a real
    matrix row; it's an instruction that belongs on the stem. Splitting it
    out as its own item duplicates it into the row list. Loop (not just
    once) in case more than one such parenthetical stacks up."""
    while parts and BOILERPLATE_PARENTHETICAL_RE.match(parts[0]):
        stem = (stem + " " + parts.pop(0)).strip()
    return stem, parts


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
    # NOTE: "," was removed from the lookbehind class -- a comma inside a
    # single row's own sentence (e.g. "In school, I am always seeking...")
    # was being misread as a row boundary, snapping off "In school," as a
    # bogus one-word row and corrupting the next row's text. Every other
    # split in this workbook still separates cleanly on sentence-ending
    # punctuation + capital letter without comma's help.
    stem_match = re.match(r"^(.*?[?:])\s+(.*)$", text, re.DOTALL)
    if stem_match:
        remainder = stem_match.group(2)
        # "]" added to the lookbehind class -- a row that ends in its own
        # bracketed annotation (e.g. "Location [Do not display for digital
        # resources]") was being fused onto the next row's text, since a
        # closing bracket wasn't a recognized row-ending character.
        parts = re.split(r"(?<=[a-z\)\.\?\]])\s+(?=[A-Z])", remainder)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) >= 2:
            stem, parts = _strip_boilerplate_lead(stem_match.group(1), parts)
            if len(parts) >= 2:
                return stem, parts

    # Pattern 4: no "?"/":" divider at all (e.g. "Below are 8 statements...
    # Using the 1-7 scale... I lead a purposeful life. My social...").
    # Intro text may itself span more than one sentence, so use a declared
    # count ("8 statements") when present to know how many trailing
    # sentences are real rows vs. lead-in instructions.
    parts = re.split(r"(?<=[a-z\)\.\]])\s+(?=[A-Z])", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        count_match = re.search(r"\b(\d+)\s+(?:statements|items|questions)\b", text, re.IGNORECASE)
        if count_match:
            n = int(count_match.group(1))
            if 0 < n < len(parts):
                stem, items = " ".join(parts[:-n]), parts[-n:]
                return _strip_boilerplate_lead(stem, items)
        stem, items = parts[0], parts[1:]
        return _strip_boilerplate_lead(stem, items)

    # Truly nothing to split on -- don't fabricate a duplicate; leave the
    # stem empty and flag for manual review instead.
    return "", [text]

# Parses "Display if X is selected for Y" style clauses out of the Notes
# column. Two properties of the source text this has to tolerate, found by
# scanning every "selected for" occurrence across hms_survey.xlsx (11 of 59
# such notes were failing to match at all under the original stricter
# regex):
#
#   1. Smart-quote/spacing corruption around the parent phrase's opening
#      quote -- several notes read literally `for”Are you aware...` with no
#      space and a closing-style curly quote used where an opening one
#      belongs (e.g. every Suicide Contagion follow-up). The original regex
#      required `\s+` plus a straight-or-open curly quote there, so these
#      clauses matched zero times.
#   2. Multiple option phrases OR'd/AND'd together before "is selected for"
#      (e.g. `"Yes" OR "Unsure" is selected for ...`, `"Somewhat Agree,"
#      "Agree" or "Strongly Agree" is selected for ...`). The original
#      regex could only ever capture the single quoted phrase immediately
#      before "is selected for", silently dropping every earlier option.
#
# A clause with zero matches (or an incomplete option list) doesn't fail
# loudly -- build_hms_content()'s caller falls back to `{"any": true}`,
# which shows the follow-up as soon as the parent has ANY answer at all,
# regardless of which option was actually picked. That fallback is exactly
# what was surfacing as "I said No but still get the follow-up".
QUOTE = r'["\u201c\u201d]'
CLAUSE_RE = re.compile(
    r'(?P<options>(?:' + QUOTE + r'[^"\u201c\u201d]+' + QUOTE + r'\s*(?:,\s*)?(?:(?:or|and)\s+)?)+)'
    r'(?:is|are|was|were)\s+(?P<neg>not\s+)?selected\s+for\s*'
    r'(?:' + QUOTE + r')(?P<parent>[^"\u201c\u201d]+)(?:' + QUOTE + r')?',
    re.IGNORECASE,
)
# A second, rarer word order: "Display if selected X, Y, or Z for W" -- the
# verb comes BEFORE the option list instead of after it. Same option-list
# and parent-phrase grammar, just reordered, so reuses the same building
# blocks rather than being a one-off special case.
CLAUSE_RE_VERB_FIRST = re.compile(
    r'(?P<neg>not\s+)?selected\s+(?P<options>(?:' + QUOTE + r'[^"\u201c\u201d]+' + QUOTE + r'\s*(?:,\s*)?(?:(?:or|and)\s+)?)+)'
    r'for\s*(?:' + QUOTE + r')(?P<parent>[^"\u201c\u201d]+)(?:' + QUOTE + r')?',
    re.IGNORECASE,
)
OPTION_RE = re.compile(r'["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]')


def extract_clauses(note):
    """Returns a flat list of (option_text, negation_bool, parent_phrase)
    tuples -- same shape the caller already expects from a plain
    CLAUSE_RE.findall(), except a single "X OR Y is selected for Z" clause
    now yields one tuple per option instead of only the last one."""
    out = []
    for rex in (CLAUSE_RE, CLAUSE_RE_VERB_FIRST):
        for m in rex.finditer(note):
            options = OPTION_RE.findall(m.group("options"))
            neg = bool(m.group("neg"))
            parent = m.group("parent").strip()
            for opt in options:
                out.append((opt, neg, parent))
    return out


# A second clause grammar entirely: a direct value check against the
# PARENT's own answer ("'X' is not 0=0.", "'X' is not 'No, Never.'"),
# rather than "which option was selected for X". Two forms depending on
# whether the compared value is written as a response CODE or as a quoted
# LABEL -- kept as separate patterns since a shared one would have to guess
# which side of "is (not)" is the code and which is the label.
VALUE_CLAUSE_CODE_RE = re.compile(
    QUOTE + r'([^"\u201c\u201d]+)' + QUOTE + r'\s+is\s+(not\s+)?(\d+)\s*=',
    re.IGNORECASE,
)
VALUE_CLAUSE_LABEL_RE = re.compile(
    QUOTE + r'([^"\u201c\u201d]+)' + QUOTE + r'\s+is\s+(not\s+)?' + QUOTE + r'([^"\u201c\u201d]+)' + QUOTE,
    re.IGNORECASE,
)


def extract_value_clauses(note):
    """Returns a list of (parent_phrase, negation_bool, value, kind) tuples,
    kind being "code" (value is already a response code, e.g. "0") or
    "label" (value is a response label needing find_option_code() same as
    an ordinary clause option)."""
    out = []
    for m in VALUE_CLAUSE_CODE_RE.finditer(note):
        out.append((m.group(1).strip(), bool(m.group(2)), m.group(3), "code"))
    for m in VALUE_CLAUSE_LABEL_RE.finditer(note):
        out.append((m.group(1).strip(), bool(m.group(2)), m.group(3).strip(), "label"))
    return out


# A third grammar: no option or value at all -- "Display if previous
# question is displayed" or `Display only if "X" is displayed.` This
# references another question's own VISIBILITY, not its answer, so it's
# resolved differently downstream (inheriting that question's showIf
# wholesale) rather than turned into an equals/in/includes-style condition.
DISPLAYED_RE = re.compile(QUOTE + r'([^"\u201c\u201d]+)' + QUOTE + r'\s+is\s+displayed', re.IGNORECASE)
PREVIOUS_QUESTION_DISPLAYED_RE = re.compile(r'previous\s+question\s+is\s+displayed', re.IGNORECASE)

# A fourth grammar, specific to matrix parents: "respondent selects
# anything other than 'X' for any statement in 'PARENT'". This is a
# per-ROW condition on a matrix question (any one of its items differs
# from X), not a whole-question condition -- corresponds to
# matrixAnyNotEquals in study_engine.js's evalCondition, which reads the
# matrix answer object per-row rather than as a single flat value.
MATRIX_ANY_CLAUSE_RE = re.compile(
    r'anything\s+other\s+than\s+' + QUOTE + r'([^"\u201c\u201d]+)' + QUOTE +
    r'\s+for\s+any\s+statement\s+in\s+' + QUOTE + r'([^"\u201c\u201d]+)' + QUOTE,
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Pipe-in matrix rows -- CCMH source Notes describe some matrix questions'
# ROWS as "whatever the participant selected in an earlier question"
# (survey-tool "carry forward choices"), rather than fixed text. Two forms
# seen in this workbook:
#   "...the statements are the selected options for 'PARENT'..."      (q26)
#   "...N statements for each option selected for 'PARENT'..."        (q28)
# The first means "one row per selected option of PARENT". The second means
# "the N statements bundled in this row's own Question text, repeated once
# per selected option of PARENT" -- a template, not a literal row list.
# Neither is wired up via the structured Parent Question # column (that's
# reserved for showIf), so PARENT is resolved the same fuzzy way a showIf
# clause's quoted phrase is: phrase_matches_parent() against every other
# question's own text in the module.
PIPE_IN_SIMPLE_RE = re.compile(
    r"statements\s+are\s+the\s+selected\s+options\s+for\s*" + QUOTE + r"([^\"\u201c\u201d]+)" + QUOTE,
    re.IGNORECASE,
)
PIPE_IN_TEMPLATE_RE = re.compile(
    r"statements?\s+for\s+each\s+option\s+selected\s+for\s*" + QUOTE + r"([^\"\u201c\u201d]+)" + QUOTE,
    re.IGNORECASE,
)
# An inline "[pipe in ...]" bracket embedded mid-sentence in the Question
# column itself (as opposed to a Notes-column instruction) -- e.g. q28's
# "...at [pipe in selected options from: 'PARENT']? Convenient hours...".
# Left in place, this corrupts split_matrix_stem_and_items() (the bracket's
# own trailing text gets sliced off as a bogus extra row). Replaced with a
# generic phrase instead of deleted outright so the stem this produces still
# reads as a complete sentence once dynamic rows are substituted in at
# render time (see pipeInItems in study_engine.js/app.js).
PIPE_IN_BRACKET_RE = re.compile(r"\[\s*pipe\s+in[^\]]*\]", re.IGNORECASE)


def strip_pipe_in_bracket(text, replacement="each place you selected"):
    return PIPE_IN_BRACKET_RE.sub(replacement, text)

# ---------------------------------------------------------------------------
# Row-level "hide for digital resources" annotations. A few source rows
# carry a bracketed instruction inside their own text -- e.g.
# "Location [Do not display for digital resources]" (the per-place
# satisfaction aspects). That bracket is an INSTRUCTION to the survey
# builder, not participant-facing text, so it is stripped from the label and
# turned into a machine-readable flag (hideForDigital: true) on the item.
#
# Whether a given place is "digital" is NOT a property of the place itself
# (YourDost, a community provider etc. can each be in-person or remote) --
# it's the participant's own answer to the earlier "how were your sessions
# conducted" matrix (In-person only / Remote-telehealth only / Both). A
# place counts as digital when that answer is the remote-only one; "Both"
# still involves a physical location, so Location stays for it. The
# question is found by DIGITAL_MODE_QUESTION_PHRASE and the remote-only
# codes are read off its options, then emitted on the pipe-in as
# hideForDigitalWhen: { matrixQuestionId, in: [...] } for app.js to check
# per place at render time.
# ---------------------------------------------------------------------------
DIGITAL_HIDE_RE = re.compile(r"\s*\[\s*do\s+not\s+display\s+for\s+digital[^\]]*\]", re.IGNORECASE)
DIGITAL_MODE_QUESTION_PHRASE = "how were your counseling or therapy sessions conducted"


def remote_only_codes(options):
    """Codes of options that mean remote/digital ONLY -- mentions remote,
    telehealth or digital, but not in-person / both."""
    codes = []
    for opt in options:
        label = opt["label"]
        if re.search(r"remote|telehealth|digital|online", label, re.IGNORECASE) and not re.search(r"in[- ]person|both", label, re.IGNORECASE):
            codes.append(opt["code"])
    return codes


def make_items(qid, items, tag):
    """Build [{id, label, hideForDigital?}] from raw split row texts,
    stripping any "[Do not display for digital resources]" annotation from
    the participant-facing label. Ids stay index-based on the ORIGINAL row
    position, so hiding a row never renumbers its neighbours."""
    out = []
    for i, raw in enumerate(items):
        hide = bool(DIGITAL_HIDE_RE.search(raw))
        item = {"id": f"{qid}-{tag}{i}", "label": DIGITAL_HIDE_RE.sub("", raw).strip()}
        if hide:
            item["hideForDigital"] = True
        out.append(item)
    return out



# A THIRD pipe-in shape: a filtered pipe-in, embedded as a bracket in the
# QUESTION text itself rather than the Notes column -- "[pipe in the
# selected options A/B/... from the question: PARENT]" (q29's stem). Unlike
# the two Notes-driven forms above, this doesn't name its OWN source
# question -- PARENT here is itself a pipe-in matrix (e.g. q26, whose rows
# are already piped in from q25), and "the selected options A/B/..." are
# option LABELS of PARENT that filter which of PARENT's (piped-in) rows
# qualify. So the actual row source for a filtered pipe-in is PARENT's own
# pipeInFrom, filtered to rows where PARENT's per-row answer matches one of
# A/B/...'s resolved option codes.
PIPE_IN_FILTERED_BRACKET_RE = re.compile(
    r"\[\s*pipe\s+in\s+(?:the\s+)?selected\s+options\s+(?P<filterlabels>.+?)\s+from\s+the\s+question:\s*"
    r"(?:" + QUOTE + r")?(?P<parent>[^\[\]\"\u201c\u201d]+?)(?:" + QUOTE + r")?\s*\]",
    re.IGNORECASE,
)


def find_qnum_by_phrase(phrase, qnum_to_row):
    """Same fuzzy resolution as find_qid_by_phrase, but returns the source
    Question # instead of the id -- needed here to look the parent's own
    row (and options) back up afterward."""
    best_qnum, best_ratio = None, 0.0
    norm_phrase = normalise(phrase)
    for qnum, row in qnum_to_row.items():
        qtext = clean(row.get("Question"))
        if phrase_matches_parent(phrase, qtext):
            ratio = SequenceMatcher(None, norm_phrase, normalise(qtext)).ratio()
            if ratio > best_ratio:
                best_qnum, best_ratio = qnum, ratio
    return best_qnum


def find_qid_by_phrase(phrase, qnum_to_row, qnum_to_id):
    """Resolves a quoted question-phrase to a question id by fuzzy-matching
    against every question's own text in this module (same tolerance as
    phrase_matches_parent), for references -- like the pipe-in Notes above
    -- that name a question by text rather than via Parent Question #."""
    return qnum_to_id.get(find_qnum_by_phrase(phrase, qnum_to_row))


# Excel workbook sheet-tab names are hard-capped at 31 characters, and both
# hms_survey.xlsx and mecamhsurvey.xlsx have module sheets that got silently
# truncated at save time -- the missing text is gone from the tab itself, so
# it has to be restored here rather than recovered from any file. Keyed by
# the exact (truncated) sheet name as it appears in the workbook today.
# Confirmed full titles (per Jovisha):
#   "Mental Health Service Utilizati"  -> "Mental Health Service Utilization"
#   "Academic Persistence, Retention"  -> "Academic Persistence, Retention and Competition"
#   "Coping Responses and Climate Ch"  -> "Coping Responses and Climate Change"
MODULE_TITLE_OVERRIDES = {
    "Mental Health Service Utilizati": "Mental Health Service Utilization",
    "Academic Persistence, Retention": "Academic Persistence, Retention and Competition",
    "Coping Responses and Climate Ch": "Coping Responses and Climate Change",
}


def full_module_name(sheet_name):
    """Resolve a possibly-truncated Excel sheet-tab name to its real title.
    Used for BOTH the module id and the display title -- previously only
    the title went through MODULE_TITLE_OVERRIDES while the id was slugified
    straight from the truncated sheet_name, so a module could display the
    correct full name while its id (and everything keyed off it --
    activeModuleIds, sectionOrder, questionOrder in study_config.js) stayed
    truncated/mismatched with the title."""
    return MODULE_TITLE_OVERRIDES.get(sheet_name, sheet_name)



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
    # Return section IDS (matching module["sections"][i]["id"]), not raw
    # section names -- study_config.js's sectionOrder must key off the same
    # ids study_schema.js uses. order_module_by_direction() (which HMS/
    # MECAMH go through) always derives its section_order from sec["id"],
    # never from raw names -- Demographics skips that function entirely
    # (it's unscored/fixed-order) so it has to build the id list itself
    # here instead, or main() ends up writing raw names like "Age" into
    # sectionOrder.demographics instead of "demographics-age".
    section_ids = [f"{module_id}-{slugify(name)}" for name in section_order]
    return module, section_ids, review_notes


def build_hms_content():
    wb = openpyxl.load_workbook("../outputs/hms_question_score_reordered.xlsx", data_only=True)
    module_sheets = [sn for sn in wb.sheetnames if sn not in ("Read Me", "Overview")]

    modules = []
    review_notes = []

    for sheet_name in module_sheets:
        headers, rows = load_sheet_rows("../outputs/hms_question_score_reordered.xlsx", sheet_name)
        if not rows:
            continue
        module_id = f"hms-{slugify(full_module_name(sheet_name))}"

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
        # Running id -> question-dict map, built incrementally as rows are
        # processed (NOT the same as id_to_qobj below, which needs every
        # row done first). A filtered pipe-in (see PIPE_IN_FILTERED_BRACKET_RE)
        # names an earlier matrix question as its parent and needs that
        # parent's OWN pipeInFrom, so it has to look the parent up while
        # still mid-loop -- this only works because the referenced parent
        # is always the row immediately above it in the source sheet.
        built_questions_by_id = {}

        for row in rows:
            qnum = clean(row.get("Source Question #"))
            qid = qnum_to_id.get(qnum)
            if qid is None:
                continue
            sec_name = clean(row.get("Section")) or "General"
            question_text = clean(row.get("Question"))
            options = parse_response_categories(row.get("Response Categories"))
            is_matrix = "matrix" in clean(row.get("Response Type")).lower()
            filtered_bracket = PIPE_IN_FILTERED_BRACKET_RE.search(question_text)
            # A filtered pipe-in question (q29-style) reads as rows-of-
            # providers even when the Response Type column never classified
            # it as Matrix -- the bracket in its own text is what actually
            # decides this, not that column.
            qtype = "matrix" if filtered_bracket else guess_type(question_text, options, is_matrix)
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

            if filtered_bracket:
                filter_qnum = find_qnum_by_phrase(filtered_bracket.group("parent"), qnum_to_row)
                filter_qid = qnum_to_id.get(filter_qnum)
                filter_row = qnum_to_row.get(filter_qnum)
                filter_parent_options = parse_response_categories(filter_row.get("Response Categories")) if filter_row else []
                filter_labels = [p.strip() for p in filtered_bracket.group("filterlabels").split("/") if p.strip()]
                filter_codes = [c for c in (find_option_code(lbl, filter_parent_options) for lbl in filter_labels) if c is not None]
                # The filtered question's actual row SOURCE isn't the named
                # parent itself -- it's whatever THAT parent's own rows were
                # piped in from (q26's rows are places piped in from q25;
                # q29 wants those same places, just narrowed to the ones
                # q26 marked matching filter_codes).
                upstream_source = built_questions_by_id.get(filter_qid, {}).get("pipeInFrom") if filter_qid else None
                question["stem"] = PIPE_IN_FILTERED_BRACKET_RE.sub("", question_text).strip()
                if filter_qid and upstream_source and len(filter_codes) == len(filter_labels):
                    question["pipeInFrom"] = upstream_source
                    question["pipeInFilter"] = {"matrixQuestionId": filter_qid, "in": filter_codes}
                else:
                    review_notes.append((qid, "needs_review", "filtered pipe-in source/options not fully resolved -- left as an unfiltered question, needs manual review"))
            elif is_matrix:
                notes_text = clean(row.get("Notes"))
                simple_pipe = PIPE_IN_SIMPLE_RE.search(notes_text)
                template_pipe = PIPE_IN_TEMPLATE_RE.search(notes_text)
                cleaned_text = strip_pipe_in_bracket(question_text)

                if simple_pipe:
                    pipe_qid = find_qid_by_phrase(simple_pipe.group(1), qnum_to_row, qnum_to_id)
                    question["stem"] = cleaned_text
                    if pipe_qid:
                        question["pipeInFrom"] = pipe_qid
                    else:
                        review_notes.append((qid, "needs_review", "pipe-in source question not resolved from Notes -- fell back to static matrix items"))
                        stem, items = split_matrix_stem_and_items(cleaned_text)
                        question["stem"] = stem
                        question["matrixItems"] = make_items(qid, items, "i")
                elif template_pipe:
                    stem, items = split_matrix_stem_and_items(cleaned_text)
                    question["stem"] = stem
                    pipe_qid = find_qid_by_phrase(template_pipe.group(1), qnum_to_row, qnum_to_id)
                    if pipe_qid:
                        question["pipeInFrom"] = pipe_qid
                        question["pipeInTemplate"] = make_items(qid, items, "t")
                        if any(t.get("hideForDigital") for t in question["pipeInTemplate"]):
                            mode_qnum = find_qnum_by_phrase(DIGITAL_MODE_QUESTION_PHRASE, qnum_to_row)
                            mode_qid = qnum_to_id.get(mode_qnum)
                            mode_row = qnum_to_row.get(mode_qnum)
                            mode_codes = remote_only_codes(parse_response_categories(mode_row.get("Response Categories"))) if mode_row else []
                            if mode_qid and mode_codes:
                                question["hideForDigitalWhen"] = {"matrixQuestionId": mode_qid, "in": mode_codes}
                                review_notes.append((qid, "digital_rows", f"row(s) flagged hideForDigital are hidden for places answered remote-only (code(s) {', '.join(mode_codes)}) in {mode_qid}"))
                            else:
                                review_notes.append((qid, "needs_review", "has hideForDigital row(s) but the 'how sessions were conducted' question / its remote-only option was not resolved -- rows will show for every place"))
                        # Same split, but with the bracket replaced by a
                        # substitution placeholder instead of generic text --
                        # this is what app.js actually displays per-screen
                        # (one screen per selected place), substituting each
                        # place's own label in for "{option}". `stem` above
                        # (the generic version) is kept only as a fallback for
                        # a schema that predates this field.
                        stem_template, _ = split_matrix_stem_and_items(strip_pipe_in_bracket(question_text, "{option}"))
                        question["pipeInStemTemplate"] = stem_template
                        review_notes.append((qid, "matrix_split", f"{len(items)} template item(s) parsed, piped from {pipe_qid}" + ("" if stem else " — EMPTY STEM, needs manual review")))
                    else:
                        review_notes.append((qid, "needs_review", "pipe-in source question not resolved from Notes -- fell back to static matrix items"))
                        question["matrixItems"] = make_items(qid, items, "i")
                else:
                    stem, items = split_matrix_stem_and_items(cleaned_text)
                    question["stem"] = stem
                    question["matrixItems"] = make_items(qid, items, "i")
                    review_notes.append((qid, "matrix_split", f"{len(items)} items parsed" + ("" if stem else " — EMPTY STEM, needs manual review")))

            built_questions_by_id[qid] = question
            sections_by_name[sec_name].append(question)

        # showIf resolution, now operator-aware based on the PARENT's type.
        # id_to_qobj is built ONCE, up front -- it holds the same object
        # references that get mutated (q["showIf"] set) as this loop runs,
        # so a later question inheriting an earlier one's showIf (see the
        # "is displayed" handling below) always sees that earlier
        # question's up-to-date, already-resolved condition, not a stale
        # snapshot -- as long as the parent is processed earlier in
        # document order, which every case seen in this workbook is.
        id_to_qobj = {qq["id"]: qq for qs in sections_by_name.values() for qq in qs}
        for sec_name, questions in sections_by_name.items():
            for q in questions:
                if q["_role"] != "Follow-up Question" or not q["_parent_field"]:
                    continue
                clauses = extract_clauses(q["_raw_skip"])
                value_clauses = extract_value_clauses(q["_raw_skip"])
                matrix_any_clauses = [
                    (m.group(1).strip(), m.group(2).strip())
                    for m in MATRIX_ANY_CLAUSE_RE.finditer(q["_raw_skip"])
                ]
                displayed_match = DISPLAYED_RE.search(q["_raw_skip"])
                previous_displayed = bool(PREVIOUS_QUESTION_DISPLAYED_RE.search(q["_raw_skip"]))
                parent_nums = [p.strip() for p in q["_parent_field"].split(";") if p.strip()]
                show_if_clauses = []
                for parent_num in parent_nums:
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
                        if phrase_matches_parent(parent_phrase, parent_text):
                            matched.append((option_text, bool(negation)))

                    if not matched:
                        # "Previous question is displayed" doesn't name a
                        # phrase to fuzzy-match at all -- it only makes
                        # sense when there's exactly one parent, and that
                        # parent IS "the previous question" by construction
                        # (Parent Question # was resolved to it), so apply
                        # it directly rather than searching for a quote.
                        if previous_displayed and len(parent_nums) == 1:
                            parent_q = id_to_qobj.get(parent_id)
                            if parent_q is not None:
                                inherited = parent_q.get("showIf")
                                if isinstance(inherited, list):
                                    show_if_clauses.extend(inherited)
                                elif inherited:
                                    show_if_clauses.append(inherited)
                                continue  # resolved either way -- inherited condition(s), or parent (and so this) is unconditional
                        # Named "'X' is displayed" -- confirm the named
                        # phrase is actually THIS parent before inheriting
                        # its condition (a note can name a different
                        # question here than the one this parent_num
                        # points to, when there are multiple parents).
                        if displayed_match and phrase_matches_parent(displayed_match.group(1), parent_text):
                            parent_q = id_to_qobj.get(parent_id)
                            if parent_q is not None:
                                inherited = parent_q.get("showIf")
                                if isinstance(inherited, list):
                                    show_if_clauses.extend(inherited)
                                elif inherited:
                                    show_if_clauses.append(inherited)
                                continue
                        # Direct value comparison against the parent's own
                        # answer ("'X' is not 0=0.", "'X' is not 'Label.'")
                        # rather than an option-selection clause.
                        value_matched = [
                            (neg, val, kind) for phrase, neg, val, kind in value_clauses
                            if phrase_matches_parent(phrase, parent_text)
                        ]
                        if value_matched:
                            neg, val, kind = value_matched[0]
                            code = val if kind == "code" else find_option_code(val, parent_options)
                            if code is not None:
                                show_if_clauses.append({"questionId": parent_id, "notEquals" if neg else "equals": code})
                                continue
                        # "Anything other than X for any statement in
                        # PARENT" -- a per-row matrix condition, only
                        # meaningful when the parent actually IS a matrix.
                        if parent_type == "matrix":
                            matrix_any_matched = [
                                option_text for option_text, phrase in matrix_any_clauses
                                if phrase_matches_parent(phrase, parent_text)
                            ]
                            if matrix_any_matched:
                                code = find_option_code(matrix_any_matched[0], parent_options)
                                if code is not None:
                                    show_if_clauses.append({"questionId": parent_id, "matrixAnyNotEquals": code})
                                    continue
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
                    if parent_type == "matrix":
                        # Parent is a matrix (e.g. q26): "X is selected for
                        # PARENT" means "some row of PARENT's answer is X",
                        # not "PARENT's own answer is X" -- PARENT's answer
                        # is a per-row object, not a scalar, so plain in/
                        # notIn (which compare against a scalar) can never
                        # match. matrixAnyIn/matrixAnyNotIn read it per-row
                        # the same way matrixAnyNotEquals already does for
                        # its own grammar above.
                        key = "matrixAnyNotIn" if negated else "matrixAnyIn"
                    elif is_multi_parent:
                        key = "excludesAny" if negated else "includesAny"
                    else:
                        key = "notIn" if negated else "in"
                    show_if_clauses.append({"questionId": parent_id, key: codes})

                # Dedupe: "is displayed" inheritance and a value/option
                # clause can independently arrive at the identical
                # condition for different declared parents (Q25 does this
                # for exactly this reason -- inheriting Q24's condition and
                # separately deriving the same condition against Q23,
                # Q24's own parent). Harmless if left in (shouldShow ORs
                # the list), but redundant.
                deduped = []
                seen_clauses = set()
                for c in show_if_clauses:
                    key = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in c.items()))
                    if key not in seen_clauses:
                        seen_clauses.add(key)
                        deduped.append(c)
                show_if_clauses = deduped

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

        # Implicit ordering dependencies: a question that isn't a declared
        # Follow-up (no skip-logic condition on it -- it always displays)
        # can still be a *content* follow-up of an earlier matrix question
        # in the same section, when its own matrix rows are literally a
        # subset of that earlier question's rows -- e.g. a "the 2 problems
        # below" recall question whose two rows restate two of the main
        # matrix's nine. The source Notes column has no way to express this
        # (it isn't a display condition -- the recall question always shows),
        # so cognitive-load reordering has no signal that it must trail the
        # question it's quoting: in "descending" direction it was sorting
        # this question by its own (higher) score, ahead of the very
        # question its own wording depends on having just been shown.
        # Detected generically off matrixItems already parsed above, not
        # hardcoded to any specific question pair -- see order_module_by_
        # direction() for how this gets used to pin delivery order.
        for sec_name, questions in sections_by_name.items():
            for i, q in enumerate(questions):
                if q["_role"] == "Follow-up Question" or q.get("_parent_ids") or "matrixItems" not in q:
                    continue
                q_items = {normalise(it["label"]) for it in q["matrixItems"]}
                if not q_items:
                    continue
                for earlier in questions[:i]:
                    if "matrixItems" not in earlier:
                        continue
                    earlier_items = {normalise(it["label"]) for it in earlier["matrixItems"]}
                    if q_items < earlier_items:
                        q["_implicit_after"] = earlier["id"]
                        review_notes.append((q["id"], "implicit_order",
                            f"rows are a subset of {earlier['id']}'s -- pinned to follow it in delivery order"))
                        break

        modules.append({
            "id": module_id,
            "title": full_module_name(sheet_name),
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
        module_id = f"mecamh-{slugify(full_module_name(sheet_name))}"

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
                question["matrixItems"] = make_items(qid, items, "i")
                review_notes.append((qid, "matrix_split", f"{len(items)} items parsed" + ("" if stem else " — EMPTY STEM, needs manual review")))

            sections_by_name[sec_name].append(question)

        modules.append({
            "id": module_id,
            "title": full_module_name(sheet_name),
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

    # Per-section anchor score, same as before.
    section_score = {}
    for sec in module["sections"]:
        qs = sec["questions"]
        anchors = [q for q in qs if q.get("_role", "Independent Question") != "Follow-up Question"]
        best = min((q["_score"] for q in anchors), default=min((q["_score"] for q in qs), default=0))
        section_score[sec["id"]] = sign * best

    # Cross-section dependency edges: if question in dependent's section
    # cites a parent id living in a DIFFERENT section, the whole dependent
    # section has to be delivered after that parent section -- otherwise
    # the dependent question's showIf evaluates against an unanswered
    # parent (permanently hidden, since the cursor never revisits earlier
    # positions) the moment the two sections land on opposite sides of the
    # pure-score sort below. The within-section union-find further down
    # already keeps same-section mother/follow-up pairs adjacent; this is
    # the section-level analogue for cross-section pairs (e.g. Utilization
    # Q40's showIf cites Q7/Q12, which live in different sections than Q40
    # itself; Q27-Q30's showIf cites Q22/Q23/Q26, in a different section
    # again). A pure per-section score sort has no way to know about these
    # -- it can and did put the dependent section first purely because its
    # own anchor score happened to sort that way under one direction.
    id_to_section = {q["id"]: sec["id"] for sec in module["sections"] for q in sec["questions"]}
    deps = {sec["id"]: set() for sec in module["sections"]}
    for sec in module["sections"]:
        for q in sec["questions"]:
            parent_ids = list(q.get("_parent_ids", []) or [])
            implicit_pid = q.get("_implicit_after")
            if implicit_pid:
                parent_ids.append(implicit_pid)
            for pid in parent_ids:
                parent_sec = id_to_section.get(pid)
                if parent_sec and parent_sec != sec["id"]:
                    deps[sec["id"]].add(parent_sec)

    # Stable topological sort: repeatedly pick the best-scoring section
    # among those whose dependency sections are already placed. Degrades
    # to the original pure score sort whenever there are no cross-section
    # dependencies (deps all empty), so this is a strict generalization,
    # not a behavior change for modules that don't need it.
    remaining = {sec["id"] for sec in module["sections"]}
    placed = []
    placed_set = set()
    while remaining:
        ready = [sid for sid in remaining if deps[sid] <= placed_set]
        if not ready:
            # A dependency cycle shouldn't happen in practice (it would mean
            # two sections each need a question from the other answered
            # first) -- fall back to plain score order for whatever's left
            # rather than looping forever.
            ready = list(remaining)
        ready.sort(key=lambda sid: section_score[sid])
        chosen = ready[0]
        placed.append(chosen)
        placed_set.add(chosen)
        remaining.discard(chosen)
    id_to_sec_obj = {sec["id"]: sec for sec in module["sections"]}
    section_order = placed

    question_order = {}
    for sec_id in section_order:
        sec = id_to_sec_obj[sec_id]
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
        # Implicit-order links (see build_hms_content) move as part of the
        # same block as their declared-parent siblings, but must still land
        # AFTER any real skip-logic follow-ups within that block -- tier()
        # below handles that; this union just keeps them together.
        for q in qs:
            implicit_pid = q.get("_implicit_after")
            if implicit_pid and implicit_pid in id_to_q:
                union(q["id"], implicit_pid)

        # Three tiers, not two: the true anchor (0), its declared skip-logic
        # follow-ups (1), then anything only implicitly pinned after it (2).
        # Collapsing tiers 0 and 2 together (as a plain has-a-parent? check
        # would) let an implicit-after question tie-break ahead of a real
        # follow-up under a stable sort, since it enters the group before
        # the follow-up is even discovered -- that's exactly how the PHQ-2
        # recall question was landing between the PHQ-9 matrix and its own
        # difficulty follow-up instead of after both.
        def tier(q):
            if q.get("_role", "Independent Question") == "Follow-up Question":
                return 1
            if q.get("_implicit_after"):
                return 2
            return 0

        groups = {}
        order = []
        for q in qs:
            key = find(q["id"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(q)
        for key in groups:
            groups[key].sort(key=tier)
        scored = []
        for key in order:
            members = groups[key]
            anchor_score = next(
                (q["_score"] for q in members if tier(q) == 0),
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
        # "equals"/"notEquals" (direct value-comparison clauses) and
        # "matrixAnyNotEquals" (per-row matrix clauses) carry a single
        # scalar code value, not a list -- these were being silently
        # dropped by the loop below, which only ever checked the six
        # list/array-oriented condition keys, and fell through to the
        # generic "any: true" fallback for anything else. That fallback
        # is meant for genuinely UNRESOLVED clauses (see the
        # needs_review/unresolved_parent review-note paths in
        # build_hms_content) -- it was never meant to also catch these
        # two condition types, which resolve to a specific value just
        # fine and shouldn't degrade to "show unconditionally."
        for key in ("equals", "notEquals", "matrixAnyNotEquals"):
            if key in c:
                return f'{{ questionId: {js_string(c["questionId"])}, {key}: {js_string(c[key])} }}'
        for key in ("in", "notIn", "includes", "excludes", "includesAny", "excludesAny", "matrixAnyIn", "matrixAnyNotIn"):
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


def emit_item(item):
    extra = ", hideForDigital: true" if item.get("hideForDigital") else ""
    return f'{{ id: {js_string(item["id"])}, label: {js_string(item["label"])}{extra} }}'


def emit_question(q, review_by_qid, indent="        "):
    lines = [f'{indent}{{']
    lines.append(f'{indent}  id: {js_string(q["id"])},')
    lines.append(f'{indent}  type: {js_string(q["type"])},')
    lines.append(f'{indent}  stem: {js_string(q["stem"])},')

    for kind, detail in review_by_qid.get(q["id"], []):
        lines.append(f'{indent}  // TODO-VERIFY ({kind}): {detail}')

    if q.get("pipeInFrom"):
        # Rows are resolved at render time (app.js:resolvePipeInItems) from
        # whatever the participant selected for pipeInFrom, not fixed here.
        if q.get("pipeInTemplate"):
            lines.append(f'{indent}  pipeInItems: {{')
            lines.append(f'{indent}    fromQuestionId: {js_string(q["pipeInFrom"])},')
            if q.get("pipeInStemTemplate"):
                lines.append(f'{indent}    stemTemplate: {js_string(q["pipeInStemTemplate"])},')
            lines.append(f'{indent}    template: [')
            for item in q["pipeInTemplate"]:
                lines.append(f'{indent}      {emit_item(item)},')
            lines.append(f'{indent}    ],')
            if q.get("hideForDigitalWhen"):
                hd = q["hideForDigitalWhen"]
                hd_codes = ", ".join(js_string(c) for c in hd["in"])
                lines.append(f'{indent}    hideForDigitalWhen: {{ matrixQuestionId: {js_string(hd["matrixQuestionId"])}, in: [{hd_codes}] }},')
            lines.append(f'{indent}  }},')
        elif q.get("pipeInFilter"):
            filt = q["pipeInFilter"]
            codes = ", ".join(js_string(c) for c in filt["in"])
            lines.append(f'{indent}  pipeInItems: {{')
            lines.append(f'{indent}    fromQuestionId: {js_string(q["pipeInFrom"])},')
            lines.append(f'{indent}    filter: {{ matrixQuestionId: {js_string(filt["matrixQuestionId"])}, in: [{codes}] }},')
            lines.append(f'{indent}  }},')
        else:
            lines.append(f'{indent}  pipeInItems: {{ fromQuestionId: {js_string(q["pipeInFrom"])} }},')
    elif q.get("matrixItems"):
        lines.append(f'{indent}  items: [')
        for item in q["matrixItems"]:
            lines.append(f'{indent}    {emit_item(item)},')
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

    with open(STATIC_JS_DIR /"study_schema.js", "w", encoding="utf-8") as f:
        f.write(emit_schema_js(all_modules, all_review))

    with open(STATIC_JS_DIR /"study_config.js", "w", encoding="utf-8") as f:
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