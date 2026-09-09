"""
Scores every question extracted by extract_questions.py on:
  - Readability (Flesch Reading Ease, Flesch-Kincaid Grade) -- question text
  - Bloom's Revised Taxonomy complexity                      -- question text
  - Response Option Complexity                                -- response options, response-type-aware (UPDATED)
  - Time-span & Sensitivity                                   -- question text
  - QAS (Question Appraisal System) problem flags             -- question text + response options
  - Composite Cognitive Load score (weighted combination of the above) (UPDATED - weights restored)

Output: question_score.xlsx, one sheet per Module, sorted by Section within each,
plus a "Read Me" sheet documenting every scoring assumption below.

CHANGE LOG (most recent first):
  - Response Option Complexity rewritten: now classifies each question's response
    format (Binary / Likert / Numeric / Categorical-nominal / Categorical-ordinal)
    and only runs the Flesch+QAS formula for the ordinal/non-nominal categorical
    case, per updated spec. All other types resolve straight to "Low".
  - classify_qas() now also scans the response-options text (not just the question)
    for the "Response Categories" flag, and additionally flags it when a question
    has more than 7 discrete response options. Previously this flag only fired on
    keyword matches inside the question text, which almost never happened -- so it
    was nearly always silent. See NOTE 3 below.
  - Composite Cognitive Load restored to a WEIGHTED formula (previously equal-weighted
    per-dimension bands): Flesch Reading Ease 0.20, Bloom's 0.05, Response Option
    Complexity 0.10, Time-span & Sensitivity 0.15, QAS problems 0.50.
  - Output format changed from single CSV to multi-sheet XLSX (one sheet per Module).
"""

import re
import argparse
from pathlib import Path
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

INPUT_CSV = "hms_survey.xlsx"
OUTPUT_XLSX = "question_score.xlsx"

# ---------------------------------------------------------------------------
# Readability
# ---------------------------------------------------------------------------

def count_sentences(text):
    return len(re.findall(r'[.!?]', text)) or 1

def count_words(text):
    return len(re.findall(r'\w+', text)) or 1

def count_syllables(word):
    word = word.lower()
    syllables = re.findall(r'[aeiouy]+', word)
    return max(1, len(syllables))

def total_syllables(text):
    words = re.findall(r'\w+', text)
    return sum(count_syllables(w) for w in words) or 1

def flesch_reading_ease(text):
    words = count_words(text)
    sentences = count_sentences(text)
    syllables = total_syllables(text)
    raw = 206.835 - 1.015 * (words / sentences) - 84.6 * (syllables / words)
    # Floor at 0 to match the mentor-reviewed workbook's convention -- the
    # formula can go arbitrarily negative on long, under-punctuated run-on
    # text (e.g. multi-item matrix stems with no periods between items),
    # which isn't a meaningful reading-ease value below 0 anyway.
    return round(max(0.0, raw), 2)


def fre_band(fre):
    """
    FRE 0-40   -> High reading complexity
    FRE 41-70  -> Moderate reading complexity
    FRE 71-100 -> Low reading complexity

    Scores outside 0-100 (FRE can go negative or above 100 on dense/short
    text) are clipped to the nearest end before banding.
    """
    clipped = max(0.0, min(100.0, fre))
    if clipped <= 40:
        return "High"
    elif clipped <= 70:
        return "Moderate"
    else:
        return "Low"


# ---------------------------------------------------------------------------
# Bloom's Revised Taxonomy (keyword heuristic -- unchanged from original script)
# ---------------------------------------------------------------------------

def blooms_complexity(text):
    q = str(text).lower()
    if any(w in q for w in ["what", "when", "where", "who", "how many", "how much", "how old"]):
        return "Low"
    elif any(w in q for w in ["explain", "summarize", "compare", "classify", "interpret", "describe"]):
        return "Medium"
    elif any(w in q for w in ["evaluate", "justify", "design", "formulate", "analyze"]):
        return "High"
    else:
        return "Low"


# ---------------------------------------------------------------------------
# Time-span & sensitivity (unchanged from original script)
# ---------------------------------------------------------------------------

def time_sensitivity_complexity(text):
    q = str(text).lower()
    if any(w in q for w in ["last month", "past month", "past 4 weeks", "last 30 days", "12 months", "past year", "last year"]):
        return "High"
    elif any(w in q for w in ["sexual", "drug", "mental health", "depression", "anxiety", "suicide", "self-harm", "abuse", "assault"]):
        return "High"
    elif any(w in q for w in ["yesterday", "today", "this week", "recently", "past 2 weeks", "last 2 weeks"]):
        return "Medium"
    else:
        return "Low"


# ---------------------------------------------------------------------------
# Response option parsing (unchanged from original script)
# ---------------------------------------------------------------------------

def combined_item_text(question_text, response_text):
    """
    Full reading item = question stem + response options. Used for FRE,
    Blooms, and Time-span/Sensitivity scoring so those metrics reflect what
    a respondent actually has to read and process, not just the question
    stem. Options are folded in as parsed labels ("Not at all. Several
    days...") rather than the raw coded string ("1=Not at all 2=..."), so
    numeric option codes don't distort word/syllable counts. Falls back to
    the raw response text when it can't be parsed into discrete labels
    (e.g. open-text or fill-in items).
    """
    question = str(question_text).strip()
    options = parse_options(response_text)
    if options:
        options_text = ". ".join(o.rstrip(".") for o in options if o.strip()) + "."
    else:
        raw = str(response_text).strip()
        options_text = raw if raw and raw.lower() != "nan" else ""
    return (question + " " + options_text).strip()


def parse_options(response_text):
    """Split a 'RESPONSE CATEGORIES' cell into option labels.

    Supports two formats seen across surveys:
      - HMS-style numbered:  '1=Yes 2=No'
      - Pipe-delimited scale labels: 'Not at all | Somewhat | Extremely'
    """
    text = str(response_text)
    if not text.strip() or text.strip().lower() == "nan":
        return []

    if re.search(r'\d+\s*=', text):
        parts = re.split(r'(?=\d+\s*=)', text)
        labels = []
        for p in parts:
            m = re.match(r'\d+\s*=\s*(.*)', p.strip())
            if m and m.group(1).strip():
                labels.append(m.group(1).strip())
        return labels

    if "|" in text:
        labels = [p.strip() for p in text.split("|") if p.strip()]
        return labels

    return []


# ---------------------------------------------------------------------------
# Matrix / grid question detection
#
# A "matrix" (a.k.a. grid) question presents several statements/items that
# all share one response scale, rendered on the page as a table. Two
# independent signals are used, either of which is sufficient:
#
#   1. Notes/Citation text says so directly. HMS's source PDF marks these
#      items with phrasing like "Matrix table with 8 statements" in the
#      NOTES/CITATION column -- when that text is available (see the
#      optional "Notes" input column), trust it.
#   2. The question text itself is a stem followed by 2+ numbered
#      sub-items (e.g. "1. I feel calm  2. I feel tense  3. ..."), which is
#      how matrix items look once the sub-statements have been folded into
#      the Question cell. This catches surveys (e.g. MECAMH) that don't
#      carry a Notes/Citation column at all.
#
# Per spec: a matrix question's Response Type is reported as
# "Categorical (Grid)" and its Response Option Complexity is fixed at
# "Medium" -- it does NOT go through the normal per-type classification or
# the Flesch+QAS formula.
# ---------------------------------------------------------------------------

NUMBERED_ITEM_RE = re.compile(r'(?:^|[\.\?\!]\s+|\n)\s*(\d{1,2})[\.\)]\s+\S')


def is_matrix_by_notes(notes_text):
    text = str(notes_text).strip().lower()
    if not text or text == "nan":
        return False
    return "matrix" in text


def is_matrix_by_question_text(question_text):
    text = str(question_text)
    if not text.strip():
        return False
    numbers = [int(m.group(1)) for m in NUMBERED_ITEM_RE.finditer(text)]
    if len(numbers) < 2:
        return False
    # Require at least two of the matches to look like a genuine ascending
    # 1, 2, 3... item sequence (rather than incidental numbers like "12-17
    # years" or "1=Yes" response codes leaking into the question text).
    ascending_pairs = sum(1 for a, b in zip(numbers, numbers[1:]) if b == a + 1)
    return ascending_pairs >= 1 and numbers[0] in (1, 2)


def is_matrix_question(question_text, notes_text=""):
    return is_matrix_by_notes(notes_text) or is_matrix_by_question_text(question_text)


# ---------------------------------------------------------------------------
# Skip / display logic preservation
#
# HMS's Notes/Citation column embeds conditional-display rules for
# follow-up questions, e.g. "Display if 'Yes' is selected for ...", or
# "Included if 'Financial Stress' module not selected". These need to
# survive into the scored workbook so they can be wired up as branching
# logic in the web-based survey -- this column carries that text through
# verbatim rather than summarizing or dropping it.
# ---------------------------------------------------------------------------

SKIP_LOGIC_KEYWORDS = [
    "display if", "displayed if", "do not display", "display only",
    "shown if", "show if", "shown only",
    "included if", "include if", "not included if", "module not selected", "module selected",
    "skip if", "skip to", "conditional on",
    "if selected", "is selected for", "based on embedded skip logic",
    "respondent's age is between", "respondent selects", "respondent's",
]


def extract_skip_logic(notes_text):
    text = str(notes_text).strip()
    if not text or text.lower() == "nan":
        return ""
    lower = text.lower()
    if any(kw in lower for kw in SKIP_LOGIC_KEYWORDS):
        return text
    return ""


# ---------------------------------------------------------------------------
# Response TYPE classification (NEW)
#
# NOTE 1: This is a keyword/pattern heuristic, same spirit as the rest of the
# script -- it is not a perfect NLP classifier. Spot-check the "Response Type"
# column, especially anything landing in "Categorical (ordinal/non-nominal)",
# since that's the only bucket that feeds the Flesch+QAS formula below.
# ---------------------------------------------------------------------------

LIKERT_CUES = [
    "strongly agree", "strongly disagree", "agree", "disagree",
    "never", "always", "rarely", "often", "sometimes",
    "extremely", "not at all", "a little", "very much",
    "very dissatisfied", "very satisfied",
]

LIKERT_ANCHOR_PAIRS = [
    ("strongly disagree", "strongly agree"),
    ("never", "always"),
    ("not at all", "extremely"),
    ("very dissatisfied", "very satisfied"),
    ("not at all", "nearly every day"),
    ("not at all", "every day"),
]

BINARY_WORD_SETS = [
    {"yes", "no"}, {"true", "false"}, {"male", "female"}, {"agree", "disagree"},
]

# Response options that are meta/catch-all choices rather than part of the
# substantive scale (e.g. an "attitude" scale with a trailing "Don't know").
# Excluded when checking whether every SUBSTANTIVE option in a response set
# matches the same ordinal keyword group (see is_likert_type's full-group-
# match path below) -- a "Don't know" tacked onto an otherwise clean 5-point
# agreement scale shouldn't prevent it from being recognized as Likert.
CATCH_ALL_LABELS = [
    "don't know", "dont know", "not applicable", "n/a", "not sure",
    "prefer not to answer", "none of these", "not applicable",
]

# Groups of ordinal cue words -- if >=2 options in a response match the SAME
# group, the response set is treated as ordered (categorical/non-nominal)
# rather than nominal. If instead EVERY substantive (non-catch-all) option
# matches the same group AND the option count is 5/6/7/9, it's treated as
# Likert instead (see is_likert_type).
ORDINAL_KEYWORD_GROUPS = [
    {"never", "rarely", "sometimes", "occasionally", "often", "frequently",
     "most of the time", "always",
     "every day", "nearly every day", "several days", "more than half the days"},
    {"strongly disagree", "disagree", "neutral", "somewhat disagree", "somewhat agree",
     "agree", "strongly agree"},
    {"not difficult", "somewhat difficult", "very difficult", "extremely difficult"},
    {"mild", "moderate", "severe"},
    {"never stressful", "rarely stressful", "sometimes stressful", "often stressful",
     "always stressful"},
    {"never true", "sometimes true", "often true"},
    {"1st year", "2nd year", "3rd year", "4th year", "5th year", "6th year", "7th"},
    {"8th grade", "9th grade", "10th grade", "11th grade", "12th grade", "high school",
     "some college", "associate", "bachelor", "graduate", "doctoral"},
    {"very dissatisfied", "dissatisfied", "satisfied", "very satisfied"},
    {"not at all confident", "somewhat confident", "very confident"},
    {"very helpful", "helpful", "somewhat helpful", "not helpful"},
    {"very supportive", "supportive", "not supportive", "very unsupportive"},
    {"not close", "a little close", "somewhat close", "moderately close", "very close"},
    {"very easy", "easy", "somewhat easy", "somewhat difficult", "difficult", "very difficult"},
]


def is_numeric_type(options, raw_text):
    if len(options) == 1:
        lbl = options[0].lower()
        if "_" in lbl or "years old" in lbl or re.search(r'\bage\b', lbl):
            return True
    if re.search(r'_{2,}', str(raw_text)):
        return True
    return is_explicit_numeric_tag(raw_text)


def is_binary_type(options):
    if len(options) != 2:
        return False
    # Any exactly-two-option response is functionally a binary choice
    # regardless of how long the option labels themselves are (e.g. "Prior
    # to starting college" / "After starting college" is still Boolean).
    return True


def is_likert_type(options):
    if len(options) not in (5, 6, 7, 9):
        return False
    first, last = options[0].lower(), options[-1].lower()
    for start_kw, end_kw in LIKERT_ANCHOR_PAIRS:
        if start_kw in first and end_kw in last:
            return True
    # Fallback: nearly every option carries a likert cue word (agreement/frequency/etc.)
    cue_hits = sum(1 for opt in options if any(c in opt.lower() for c in LIKERT_CUES))
    if cue_hits >= len(options) - 1:
        return True
    # Full-group-match fallback: every SUBSTANTIVE option (i.e. excluding
    # catch-alls like "Don't know"/"Not applicable") matches the same
    # ordinal keyword group -- e.g. a 6-point Very easy...Very difficult
    # scale, or a 5-point Not close...Very close scale. A partial match
    # (only some options fit the group) is left as plain Categorical
    # (Ordinal) rather than promoted to Likert -- see is_ordinal_categorical.
    substantive = [
        opt.lower().replace("\u2019", "'") for opt in options
        if not any(ca in opt.lower().replace("\u2019", "'") for ca in CATCH_ALL_LABELS)
    ]
    if len(substantive) >= 3:
        for group in ORDINAL_KEYWORD_GROUPS:
            if all(any(kw in lbl for kw in group) for lbl in substantive):
                return True
    # "Not at all ... [count/frequency buckets] ... Don't know"-style 5-point
    # scale: a classic validated-instrument frequency format ("Not at all",
    # "1-2 times", "3-5 times", "More than 5 times", "Don't know") where the
    # anchor phrasing ("not at all" opening, a catch-all closing) is the
    # actual signal of a Likert-style item -- not the specific wording of
    # the middle buckets, which can be verbal ("Rarely"/"Often") or numeric
    # ("1-2 times"/"3-5 times") interchangeably. Restricted to the classic
    # 5-point width so it doesn't reach for wider raw-count enumerations
    # (e.g. a 7-point "0 times...10 or more times" tally, which is ordinal
    # count data rather than a frequency-of-behavior scale).
    if len(options) == 5:
        first_norm = first.replace("\u2019", "'")
        last_norm = last.replace("\u2019", "'")
        starts_with_not_at_all = first_norm.startswith("not at all")
        ends_with_catchall = any(ca in last_norm for ca in CATCH_ALL_LABELS)
        if starts_with_not_at_all and ends_with_catchall:
            return True
    return False


def is_ordinal_categorical(options):
    labels = [o.lower() for o in options]
    for group in ORDINAL_KEYWORD_GROUPS:
        hits = sum(1 for lbl in labels if any(kw in lbl for kw in group))
        # Larger option sets need a strict MAJORITY of options matching the
        # same group, not just >=2 -- otherwise a handful of incidental
        # overlapping words (e.g. "associate"/"bachelor"/"doctoral" showing
        # up in a *degree-type* choice list) falsely drags an unrelated,
        # genuinely nominal list into "ordinal" just because a large
        # keyword group happens to share vocabulary with a few of its
        # options. For small option sets (<=4) this reduces to the
        # original ">=2" behavior.
        required = max(2, len(labels) // 2 + 1)
        if hits >= required:
            return True
    # Ordered numeric ranges, e.g. "12-17 years", "3 to 5 times", "10 or
    # more times", "Between 1 and 2 months", "2 months or more".
    # "less than"/"more than" require an immediately-following digit so
    # they don't false-positive on unrelated phrases like "without a
    # prescription or more than prescribed" inside a drug-name option.
    range_hits = sum(
        1 for lbl in labels
        if re.search(r'\d+\s*[-\u2013]\s*\d+', lbl)
        or re.search(r'\d+\s+to\s+\d+', lbl)
        or re.search(r'\bbetween\s+\d+\s+and\s+\d+\b', lbl)
        or re.search(r'\d+\s+(?:\w+\s+)?or (?:more|fewer|less)\b', lbl)
        or re.search(r'(?:less than|more than)\s+\d', lbl)
    )
    return range_hits >= 2


def is_explicit_numeric_tag(raw_text):
    """
    Narrow, text-level numeric signals that never parse into 'N=Label' or
    '|'-delimited options, so they must be caught before classify_response_type's
    empty-options bail-out. Deliberately narrow (unlike is_numeric_type's
    blank-underscore check) so it can't misfire ahead of fill-in/matrix
    detection.
    """
    lower = str(raw_text).lower()
    if "[open text]" in lower and "numeric" in lower:
        return True
    if "dropdown" in lower and re.search(r'\d+\s*[-\u2013]\s*\d+|\d+\s+or more\b', lower):
        return True
    return False


def classify_response_type(response_text, question_text="", notes_text=""):
    """Returns (response_type_label, options_list)."""
    text = str(response_text).strip()
    if not text or text.lower() == "nan":
        return "No options / structured", []
    lower = text.lower()

    # Numeric-entry tags/dropdowns are checked before the open-text and
    # empty-options bail-outs below, since they never parse into "N=Label"
    # or "|"-delimited options.
    if is_explicit_numeric_tag(text):
        return "Numeric", []
    if "[open text]" in lower:
        return "Open text", []
    if "fill-in" in lower:
        return "Structured/Matrix", []

    if is_matrix_question(question_text, notes_text) or "matrix" in lower:
        return "Categorical (Grid)", parse_options(text)

    options = parse_options(text)
    if not options:
        return "Unstructured/Other", []

    if is_numeric_type(options, text):
        return "Numeric", options
    if is_binary_type(options):
        return "Binary", options
    if len(options) in (5, 6, 7, 9) and is_likert_type(options):
        return "Likert", options
    if is_ordinal_categorical(options):
        return "Categorical (ordinal/non-nominal)", options
    return "Categorical (nominal)", options


# ---------------------------------------------------------------------------
# Response Option Complexity -- REWRITTEN per updated spec
#
# Rules:
#   1. Binary                              -> Low
#   2. Likert (5/7/9-pt, verified)         -> Low
#   3. Numeric                             -> Low
#   4. Categorical + nominal               -> Low
#   5. Categorical + non-nominal (ordinal) -> 0.5*(normalized Flesch Reading Ease)
#                                              + 0.5*(QAS "Response Categories" flag)
#                                              banded: <0.25 Low, 0.25-0.6 Medium, 0.6-1 High
#
# NOTE 2: Flesch Reading Ease is 0-100, HIGHER = easier to read. For a
# complexity contribution we want the opposite direction, so it's normalized as
# (100 - clip(FRE, 0, 100)) / 100 before combining with the QAS flag. Flag if
# this isn't the convention you intended.
#
# Open text / Structured-Matrix / Unstructured response types aren't covered by
# the 5 rules above (they weren't part of the spec) -- these keep the original
# script's behavior of "High" (free-recall response burden), unchanged.
# ---------------------------------------------------------------------------

def _clip01(x):
    return max(0.0, min(1.0, x))

def _flesch_complexity_norm(fre):
    """0-1, higher = harder to read = more complex."""
    clipped = max(0.0, min(100.0, fre))
    return _clip01((100.0 - clipped) / 100.0)

def response_option_complexity(response_text, flesch_reading_ease_value, qas_problems_str):
    response_type, options = classify_response_type(response_text)

    if response_type in ("Binary", "Likert", "Numeric", "Categorical (nominal)"):
        return response_type, "Low", None

    if response_type == "Categorical (Grid)":
        return response_type, "Medium", None

    if response_type == "Categorical (ordinal/non-nominal)":
        fk_norm = _flesch_complexity_norm(flesch_reading_ease_value)
        qas_flag = 1.0 if "Response Categories" in str(qas_problems_str) else 0.0
        score = round(0.5 * fk_norm + 0.5 * qas_flag, 3)
        if score < 0.25:
            label = "Low"
        elif score < 0.6:
            label = "Medium"
        else:
            label = "High"
        return response_type, label, score

    # Open text / Structured-Matrix / Unstructured / No options -> original behavior
    return response_type, "High", None


# ---------------------------------------------------------------------------
# QAS (Question Appraisal System) problem flags -- question text + response options
#
# NOTE 3 (CHANGE): the original "Response Categories" rule only scanned the
# QUESTION text for keywords like "scale"/"1-5"/"likert", which almost never
# appear inside the question itself (those words describe the response
# options, not the question). That made the flag nearly always silent, which
# would have made rule 5's QAS term above almost always 0. It's been extended
# to also scan the response-options text, and to flag automatically when a
# question has more than 7 discrete response options. If you'd rather keep
# the original question-text-only behavior, revert this function.
# ---------------------------------------------------------------------------

QAS_RULES = {
    "Reading": ["however", "although", "therefore", "because", "whereas", "nevertheless", "nonetheless"],
    "Clarity": ["how often", "how many", "to what extent", "in general", "feel", "think", "believe"],
    "Knowledge/Memory": ["past week", "past 2 weeks", "last month", "last year", "in the past", "during this", "since", "remember"],
    "Sensitivity/Bias": ["mental", "emotional", "anxiety", "depression", "distress", "sexual", "drug",
                          "substance", "addiction", "violence", "suicide", "self-harm"],
    "Response Categories": ["scale", "rate", "1-5", "1-7", "strongly agree", "likert", "select all", "check all"],
    "Assumptions": ["when you", "your therapist", "your doctor", "your family", "your partner", "if you were", "since you"],
}

def classify_qas(question, response_categories=""):
    q_lower = str(question).lower()
    rc_lower = str(response_categories).lower()
    problems = []
    for category, keywords in QAS_RULES.items():
        haystack = q_lower if category != "Response Categories" else q_lower + " " + rc_lower
        if any(kw in haystack for kw in keywords):
            problems.append(category)
    if len(q_lower.split()) > 25:
        problems.append("Reading")
    if "?" not in q_lower and len(q_lower.split()) > 10:
        problems.append("Clarity")
    if len(parse_options(response_categories)) > 7:
        problems.append("Response Categories")
    return ", ".join(sorted(set(problems))) if problems else "None"


# ---------------------------------------------------------------------------
# Composite Cognitive Load score -- REWRITTEN as a weighted formula
#
# Weights (sum to 1.0): Flesch Reading Ease 0.20, Bloom's 0.05,
# Response Option Complexity 0.10, Time-span & Sensitivity 0.15, QAS problems 0.50.
#
# Each dimension is normalized to 0-1 (1 = highest cognitive load) before
# weighting:
#   - Flesch Reading Ease : (100 - clip(FRE,0,100)) / 100
#   - Bloom's / Response Option Complexity / Time-span & Sensitivity :
#         Low -> 0.0, Medium -> 0.5, High -> 1.0
#   - QAS problems : (number of distinct flags raised) / 6, capped at 1.0
#     (6 = total number of QAS categories checked)
#
# NOTE 4 (ASSUMPTION): "time-span complexity" in the requested weights is
# assumed to refer to the existing combined "Time-span and Sensitivity"
# column -- the pipeline has never scored these as two separate dimensions.
# Flag if you actually want them split into independent weighted terms.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "flesch": 0.20,
    "blooms": 0.05,
    "response_option": 0.10,
    "time_sensitivity": 0.15,
    "qas": 0.50,
}

LEVEL_TO_NORM = {"Low": 0.0, "Medium": 0.5, "High": 1.0}

QAS_TOTAL_CATEGORIES = 6  # Reading, Clarity, Knowledge/Memory, Sensitivity/Bias, Response Categories, Assumptions

def _qas_norm(qas_str):
    if qas_str == "None":
        return 0.0
    n = len(qas_str.split(","))
    return min(n / QAS_TOTAL_CATEGORIES, 1.0)

def composite_cognitive_load(row):
    flesch_component = _flesch_complexity_norm(row["Flesch Reading Ease"])
    bloom_component = LEVEL_TO_NORM[row["Blooms Score"]]
    response_component = LEVEL_TO_NORM.get(row["Response Option Complexity"], 0.5)
    time_component = LEVEL_TO_NORM[row["Time-span and Sensitivity"]]
    qas_component = _qas_norm(row["QAS problems"])

    score = (
        flesch_component * WEIGHTS["flesch"] +
        bloom_component * WEIGHTS["blooms"] +
        response_component * WEIGHTS["response_option"] +
        time_component * WEIGHTS["time_sensitivity"] +
        qas_component * WEIGHTS["qas"]
    )
    return round(score, 3)

def load_category(score):
    if score < 0.25:
        return "Low"
    elif score < 0.6:
        return "Medium"
    else:
        return "High"


# ---------------------------------------------------------------------------
# Excel writing helpers
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BODY_FONT = Font(name="Arial", size=10)
WRAP_ALIGN = Alignment(wrap_text=True, vertical="top")

# Question-role colours. These colours are based ONLY on Question Role,
# never merely on whether Skip/Display Logic is populated.
INDEPENDENT_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
MOTHER_FILL = PatternFill(start_color="D9EAF7", end_color="D9EAF7", fill_type="solid")
FOLLOWUP_FILL = PatternFill(start_color="E7E6E6", end_color="E7E6E6", fill_type="solid")
PHD_ADVISING_FILL = PatternFill(start_color="D9D2E9", end_color="D9D2E9", fill_type="solid")

OUT_COLS = [
    "Question #", "Section", "Question", "Question Role", "Parent Question #", "Response Categories", "Response Type",
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

def sanitize_sheet_name(name, used_names):
    clean = re.sub(r'[\\/*?:\[\]]', '-', str(name)).strip()
    clean = clean[:31] if clean else "Sheet"
    base = clean
    i = 2
    while clean in used_names:
        suffix = f" ({i})"
        clean = base[: 31 - len(suffix)] + suffix
        i += 1
    used_names.add(clean)
    return clean

def style_sheet(ws, cols, skip_logic_col_name="Skip/Display Logic"):
    """Header/body styling only. Question Role is written as plain text here --
    the three-way colour coding (yellow/blue/gray) is intentionally deferred
    to reorder_cognitive_load.py's output, not applied at scoring time."""
    for col_idx, col_name in enumerate(cols, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = WIDE_COLS.get(col_name, 16)

    for row_idx in range(2, ws.max_row + 1):
        for col_idx in range(1, len(cols) + 1):
            c = ws.cell(row=row_idx, column=col_idx)
            c.font = BODY_FONT
            c.alignment = WRAP_ALIGN


def write_readme_sheet(writer, total_count):
    notes = [
        ("HMS Question Cognitive-Load Scoring -- Read Me", ""),
        ("", ""),
        ("FRE Band (Flesch Reading Ease band)",
         "0-40 High reading complexity, 41-70 Moderate reading complexity, "
         "71-100 Low reading complexity (score clipped to 0-100 before banding)"),
        ("", ""),
        ("Response Option Complexity rules", ""),
        ("Binary", "Low"),
        ("Likert (5/7/9-pt, verified against anchor wording)", "Low"),
        ("Numeric", "Low"),
        ("Categorical, nominal", "Low"),
        ("Categorical, Grid/Matrix (multiple statements sharing one response scale)",
         "Fixed at Medium -- detected from the Notes/Citation column (text containing "
         "'matrix', e.g. 'Matrix table with 8 statements') and/or from 2+ ascending "
         "numbered sub-items folded into the Question text (e.g. '1. ... 2. ...'). "
         "Does not go through the Flesch+QAS formula below."),
        ("Categorical, ordinal/non-nominal",
         "0.5 x normalized Flesch Reading Ease + 0.5 x QAS 'Response Categories' flag "
         "(0-0.25 Low, 0.25-0.6 Medium, 0.6-1 High)"),
        ("Open text / structured (fill-in table) / unstructured",
         "Not covered by the rules above -- kept as 'High' (original script's free-recall assumption)"),
        ("", ""),
        ("Question roles and cognitive-load scoring",
         "Question roles are assigned: Independent Question, Mother Question, Follow-up Question, "
         "and PhD Advising Question. Independent, Mother, and PhD Advising questions receive "
         "cognitive-load calculations. Follow-up questions remain in the workbook with blank "
         "cognitive-load fields. Module-level inclusion notes do not by themselves create a "
         "follow-up relationship -- the sole exception is the Faculty Advising (PhD Students) "
         "question, which depends on the respondent's degree-program answer (a Demographics "
         "question, excluded from scoring) and is flagged as its own PhD Advising Question role "
         "rather than folded into Mother/Follow-up. Every other module-level inclusion/exclusion "
         "note is ignored entirely -- it no longer removes rows either (see apply_module_selection)."),
        ("Question-role colours",
         "This workbook (question_score.xlsx) is NOT colour-coded by role -- rows here are "
         "plain text so the raw scoring output stays easy to diff/review. Colour coding "
         "(Independent = light yellow, Mother = light blue, Follow-up = light gray, "
         "PhD Advising = light purple) is applied only in the final reordered workbook produced "
         "by reorder_cognitive_load.py, based on this same Question Role column."),
        ("Parent Question #", "Direct parent question number(s) identified from genuine question-level display logic. Multiple parents are separated by semicolons. This column is also used by the reordering script to keep dependency chains together."),
        ("Skip / Display Logic", ""),
        ("Skip/Display Logic column",
         "Verbatim text carried over from the Notes/Citation column whenever it describes "
         "conditional display or module inclusion. It is preserved for survey branching. "
         "The presence of text in this column does NOT determine the Question Role by itself."),
        ("", ""),
        ("Composite Cognitive Load weights", ""),
        ("Flesch Reading Ease", "0.20"),
        ("Bloom's Revised Taxonomy", "0.05"),
        ("Response Option Complexity", "0.10"),
        ("Time-span and Sensitivity", "0.15"),
        ("QAS problem flags", "0.50"),
        ("Band thresholds", "0-0.25 Low, 0.25-0.6 Medium, 0.6-1 High"),
        ("", ""),
        ("Normalization used for the composite (0-1, 1 = highest load)", ""),
        ("Flesch Reading Ease", "(100 - clip(FRE,0,100)) / 100"),
        ("Bloom's / Response Option Complexity / Time-span & Sensitivity",
         "Low=0.0, Medium=0.5, High=1.0"),
        ("QAS problems", "(# distinct flags raised) / 6, capped at 1.0"),
        ("", ""),
        ("Changes made from the previous version of this script", ""),
        ("1.", "Response Option Complexity rewritten to classify response TYPE first "
                "(Binary/Likert/Numeric/Categorical-nominal/Categorical-Grid/Categorical-ordinal), "
                "then only applies the Flesch+QAS formula to the ordinal/non-nominal case."),
        ("2.", "classify_qas()'s 'Response Categories' rule now also scans the response-options "
                "text (not just the question), and auto-flags when a question has more than 7 "
                "discrete response options. Previously this flag almost never fired."),
        ("3.", "Composite Cognitive Load switched from an equal-weighted 1-3 band average back to "
                "a weighted 0-1 formula using the weights above."),
        ("4.", "Output changed from a single CSV to this multi-sheet Excel workbook, one sheet per Module."),
        ("5.", "Flesch-Kincaid Grade and the FK Grade 13+ flag/sheet have been removed entirely -- "
                "only Flesch Reading Ease is computed."),
        ("6.", "Added generic matrix/grid detection (Notes-column keyword + numbered-sub-item "
                "pattern in the Question text) feeding a new 'Categorical (Grid)' response type. "
                "This replaces the old MECAMH-specific rule that force-labeled every MECAMH item "
                "'Likert' -- those items are matrix questions and are now classified as such."),
        ("7.", "Added a Notes and a Skip/Display Logic column so conditional-display rules from "
                "the source survey document survive into this workbook."),
        ("", ""),
        ("Total questions scored", str(total_count)),
        ("", ""),
        ("Open items for review", ""),
        ("A.", "All response-type classification and ordinal-keyword detection is heuristic "
                "(keyword/pattern based) -- spot-check the 'Response Type' column, especially "
                "rows classified as 'Categorical (ordinal/non-nominal)', since that bucket is the "
                "only one feeding the Flesch+QAS formula."),
        ("B.", "Matrix detection needs either a Notes/Citation column (with 'matrix' wording) or "
                "2+ ascending numbered sub-items in the Question text. A matrix question written as "
                "one flat statement with neither signal will be missed -- spot-check 'Categorical "
                "(Grid)' rows against the source document."),
        ("C.", "Bloom's classifier, time-span/sensitivity classifier, and the original QAS keyword "
                "lists are unchanged from your existing script."),
    ]
    ws = writer.book.create_sheet("Read Me", 0)
    for r_idx, (a, b) in enumerate(notes, start=1):
        ws.cell(row=r_idx, column=1, value=a).font = Font(name="Arial", bold=(b == "" and a != ""), size=10)
        ws.cell(row=r_idx, column=2, value=b).font = Font(name="Arial", size=10)
        ws.cell(row=r_idx, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 90



# ---------------------------------------------------------------------------
# Input helpers and response-type overrides
# ---------------------------------------------------------------------------

# These are the manually reviewed HMS corrections discussed earlier.
# Keys: (Module sheet name, Section, Question #)
# Values use THIS script's response-type labels.
RESPONSE_TYPE_OVERRIDES = {
    ("Mental Health Status", "Substance use", "22"): "Categorical (ordinal/non-nominal)",
    ("Mental Health Status", "Substance use", "24"): "Categorical (ordinal/non-nominal)",
    ("Academic Persistence, Retention", "Perceived competition", "3"): "Categorical (ordinal/non-nominal)",
    ("Academic Persistence, Retention", "Faculty Advising (Ph D Students)", "14"): "Likert",
}


def clean_key(value):
    if pd.isna(value):
        return ""
    text = str(value).strip()
    # Avoid "22.0" vs "22" mismatches.
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return text


def read_input_file(path):
    """
    Read CSV or XLSX.

    For XLSX:
      - each worksheet name becomes Module
      - Demographics is excluded here
      - auxiliary output sheets are ignored
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    if path.suffix.lower() in {".xlsx", ".xls"}:
        sheets = pd.read_excel(path, sheet_name=None)
        frames = []

        for sheet_name, sheet_df in sheets.items():
            module = str(sheet_name).strip()

            if module.lower() == "demographics":
                print(f"Skipping Demographics sheet: {sheet_name}")
                continue

            if sheet_df is None or sheet_df.empty:
                continue

            # Ignore known non-question sheets if a scored workbook is supplied.
            if module.lower() in {
                "read me",
                "flagged - fk grade 13+",
                "module summary",
                "all questions",
            }:
                continue

            if "Question" not in sheet_df.columns:
                continue

            sheet_df = sheet_df.copy()
            sheet_df["Module"] = module

            # Keep only actual question rows.
            sheet_df = sheet_df[
                sheet_df["Question"].fillna("").astype(str).str.strip() != ""
            ].copy()

            if not sheet_df.empty:
                frames.append(sheet_df)

        if not frames:
            raise ValueError("No question sheets were found in the workbook.")

        return pd.concat(frames, ignore_index=True)

    return pd.read_csv(path)


def classify_response_type_for_survey(response_text, survey_type, question_text="", notes_text=""):
    """
    Matrix/grid detection (see is_matrix_question) now runs generically for
    every survey inside classify_response_type() itself, so no per-survey
    override is needed here anymore. MECAMH items are single-scale
    statements folded into a numbered list within the Question cell, which
    is exactly what is_matrix_by_question_text() detects -- they resolve to
    "Categorical (Grid)" rather than being force-labeled "Likert".
    """
    return classify_response_type(response_text, question_text, notes_text)


def apply_response_type_override(row, detected_type):
    key = (
        clean_key(row.get("Module")),
        clean_key(row.get("Section")),
        clean_key(row.get("Question #")),
    )
    return RESPONSE_TYPE_OVERRIDES.get(key, detected_type)


# ---------------------------------------------------------------------------
# Display label mapping
#
# classify_response_type() above works with internal labels used throughout
# the scoring logic (complexity rules, overrides, matrix detection). The
# mentor-reviewed reference workbook (hms_question_score_modified_by_b.xlsx)
# uses a different, more specific naming convention for the same categories
# -- this function is the single place that translates internal -> display
# label, applied only once, right before writing "Response Type" to the
# output sheet, so none of the internal logic above has to change.
# ---------------------------------------------------------------------------

def display_response_type(internal_type, options):
    if internal_type == "Binary":
        return "Boolean"
    if internal_type == "Likert":
        n = len(options) if options else 0
        return f"Likert ({n}-point)" if n else "Likert"
    if internal_type == "Categorical (ordinal/non-nominal)":
        return "Categorical (Ordinal)"
    if internal_type == "Categorical (nominal)":
        return "Categorical (Nominal)"
    if internal_type in ("Categorical (Grid)", "Structured/Matrix"):
        return "Matrix"
    if internal_type in ("Open text", "No options / structured", "Unstructured/Other"):
        return "Open-ended / Other"
    # "Numeric" and anything unrecognized pass through unchanged.
    return internal_type


def response_option_complexity_from_type(response_type, response_text, fre, qas_problems):
    """
    Recalculate Response Option Complexity AFTER any Response Type override.

    This keeps Response Type, Response Option Complexity and the final
    Cognitive Load Score consistent.
    """
    detected_type, options = classify_response_type(response_text)

    # The existing function contains the original scoring implementation.
    # Temporarily reproduce its type-aware result using the corrected type.
    rt = str(response_type).strip()

    if rt in ("Binary", "Likert", "Numeric", "Categorical (nominal)"):
        return "Low", 0.0

    if rt == "Categorical (Grid)":
        return "Medium", 0.5

    if rt == "Categorical (ordinal/non-nominal)":
        fre_component = (100.0 - min(100.0, max(0.0, float(fre)))) / 100.0
        qas_text = str(qas_problems).lower()
        response_flag = 1.0 if "response categories" in qas_text else 0.0
        raw = 0.5 * fre_component + 0.5 * response_flag
        if raw < 0.25:
            return "Low", round(raw, 3)
        elif raw < 0.60:
            return "Medium", round(raw, 3)
        return "High", round(raw, 3)

    # Preserve the original script's high-complexity assumption for
    # open/structured/unstructured response formats.
    return "High", 1.0


def detect_survey_type(df, requested="auto"):
    if requested in ("hms", "mecamh"):
        return requested

    modules = " ".join(df["Module"].fillna("").astype(str).str.lower())
    if any(token in modules for token in ("climate anxiety", "sleep quality", "extreme weather")):
        return "mecamh"
    return "hms"


def parse_args():
    project_root = Path(__file__).resolve().parent.parent
    default_csvs = project_root / "csvs"
    default_outputs = project_root / "outputs"

    parser = argparse.ArgumentParser(
        description="Calculate question-level cognitive-load scores. "
        "With no --input given, automatically processes every known survey "
        "found in the csvs/ folder (hms_survey.xlsx and mecamhsurvey.xlsx) "
        "in a single run."
    )
    parser.add_argument(
        "--input", default=None,
        help="Process a single input file (overrides auto-discovery of both surveys).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output path for --input mode. Ignored in auto-discovery mode.",
    )
    parser.add_argument(
        "--survey",
        choices=["auto", "hms", "mecamh"],
        default="auto",
        help="Survey type for --input mode. Ignored in auto-discovery mode.",
    )
    args = parser.parse_args()
    args.project_root = project_root
    args.default_csvs = default_csvs
    args.default_outputs = default_outputs
    return args


# Known surveys auto-discovered when no --input is given. Each entry is
# (input filename under csvs/, output filename under outputs/, survey type).
# Add a new tuple here whenever a new survey is added to the pipeline --
# everything else (matrix detection, skip-logic, scoring) is survey-agnostic.
KNOWN_SURVEYS = [
    ("hms_survey.xlsx", "hms_question_score.xlsx", "hms"),
    ("mecamhsurvey.xlsx", "MECAMH_question_score.xlsx", "mecamh"),
]

# ---------------------------------------------------------------------------
# Sections to exclude
# ---------------------------------------------------------------------------

EXCLUDED_SECTIONS = {
    "Mental Health Status": {
        "Sexual Assault",
        "Intimate Partner Violence",
        "Racial Trauma",
        "Climate Anxiety"
    },

    "Mental Health Service Utilizati": {
        "Chronic Disease",
        "Healthcare Avoidance",
        "Healthcare Experiences",
        "Access to Care",
        "Refusal/Denial of Services",
    },
    "Overall Health":{
        "Sexual health and behavior"
    }
}


def remove_excluded_sections(df):
    """
    Remove specified sections from their corresponding modules.

    Matching is case-insensitive and ignores leading/trailing whitespace.
    """

    module_col = df["Module"].fillna("").astype(str).str.strip()
    section_col = df["Section"].fillna("").astype(str).str.strip()

    keep_mask = pd.Series(True, index=df.index)

    for module, sections in EXCLUDED_SECTIONS.items():
        module_mask = module_col.str.casefold() == module.casefold()

        section_names = {
            section.strip().casefold()
            for section in sections
        }

        section_mask = section_col.str.casefold().isin(section_names)

        keep_mask &= ~(module_mask & section_mask)

    return df.loc[keep_mask].copy()

# ---------------------------------------------------------------------------
# Question-role classification: Independent / Mother / Follow-up
# ---------------------------------------------------------------------------

# Modules selected for this HMS survey. Standard modules are always present;
# these are the elective modules selected for the current survey design.
SELECTED_HMS_MODULES = {
    "Demographics",
    "Mental Health Status",
    "Mental Health Service Utilization/Help-Seeking",
    "Overall Health",
    "Academic Persistence, Retention, and Competition",
    "Financial Stress",
}

MODULE_LEVEL_SKIP_HINTS = [
    "module not selected",
    "module selected",
    "included if",
    "not included if",
]


def _normalise_module_name(name):
    return re.sub(r"\s+", " ", str(name).strip().casefold())


def is_module_level_skip(skip_text):
    """Return True for module-selection/inclusion rules.

    These rules are NOT question-to-question dependencies. Surviving rows with
    module-level inclusion notes are therefore classified as Independent unless
    a separate question-level display rule establishes an actual dependency.
    """
    text = str(skip_text).strip().casefold()
    if not text or text == "nan":
        return False
    return any(hint in text for hint in MODULE_LEVEL_SKIP_HINTS)


def _normalise_question_text(text):
    """Normalise question text for reliable parent-question matching.

    The extracted HMS notes are not always character-for-character identical
    to the Question cell: punctuation, smart quotes, commas in dates, and
    small extraction differences are common. Normalising punctuation and
    whitespace makes those harmless differences disappear.
    """
    text = str(text).replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.casefold()

    # Routing/annotation asides like "[pipe in selected options from the
    # question, '...']" aren't part of the question itself, and can
    # accidentally contain a near-quote of a DIFFERENT question's wording --
    # which would otherwise cause a false parent match via containment.
    text = re.sub(r"\[[^\]]*\]", " ", text)

    # Common PDF extraction artefact: "12, months" -> "12 months".
    text = re.sub(r"(\d)\s*,\s*(?=\d|\bmonths?\b|\byears?\b)", r"\1 ", text)

    # Remove response-option codes when a quoted reference contains one.
    text = re.sub(r"\b\d+\s*=\s*", "", text)

    # Treat punctuation as whitespace so e.g. "question?" and "question"
    # compare equally.
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _quoted_strings(text):
    """Return text inside straight/curly single or double quotes."""
    text = str(text)
    if not text.strip() or text.casefold() == "nan":
        return []
    return [m.group(1).strip() for m in re.finditer(
        r"[\"'\u2018\u2019\u201c\u201d]([^\"'\u2018\u2019\u201c\u201d]+)[\"'\u2018\u2019\u201c\u201d]",
        text,
    ) if m.group(1).strip()]


def _question_similarity(a, b):
    """Conservative similarity score used only after exact normalised match."""
    from difflib import SequenceMatcher

    a = _normalise_question_text(a)
    b = _normalise_question_text(b)
    if not a or not b:
        return 0.0

    seq = SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    jaccard = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))

    # Sequence catches wording/extraction differences; token overlap protects
    # against matching two superficially similar questions.
    return 0.55 * seq + 0.45 * jaccard


def _find_parent_question_indices(skip_text, question_texts, current_idx=None):
    """Return all question indices explicitly referenced by display logic.

    Matching is deliberately conservative:
      1. "previous question is displayed" -> immediately preceding row.
      2. Exact normalized quoted-question match.
      3. Strong containment match (handles PDF extraction qualifiers/codes).
      4. Conservative fuzzy match.

    Module-level inclusion text does NOT suppress this search. A note can say
    both "Included if module X is not selected" and "dependent on question Y";
    in that case Y is still a real parent.
    """
    text = str(skip_text).strip()
    if not text or text.casefold() == "nan":
        return []

    if "previous question is displayed" in text.casefold():
        if current_idx is not None and current_idx > 0:
            return [current_idx - 1]

    raw_candidates = _quoted_strings(text)
    candidates = []
    for raw in raw_candidates:
        n = _normalise_question_text(raw)
        if len(n) < 20:
            continue
        # Only discard a quoted string when it is clearly just an option code.
        # Long strings are retained because HMS embeds the parent question
        # after the selected response in many notes.
        if len(n) < 40 and re.fullmatch(r"(?:\d+\s*=\s*)?[a-z ]+", n):
            # Do not discard ordinary short question stems.
            pass
        candidates.append(raw)

    if not candidates:
        return []

    normalised_questions = [_normalise_question_text(q) for q in question_texts]
    parents = []

    for candidate in candidates:
        cn = _normalise_question_text(candidate)
        if not cn:
            continue

        # Remove leading response-option code from the candidate for matching.
        cn_no_code = re.sub(r"^\d+\s*=\s*", "", cn).strip()

        # Exact.
        exact = [
            idx for idx, qn in enumerate(normalised_questions)
            if idx != current_idx and cn_no_code == qn
        ]
        if exact:
            for idx in exact:
                if idx not in parents:
                    parents.append(idx)
            continue

        # Containment. This is particularly important for notes such as:
        # "1=Yes is selected for [QUESTION]" where the extracted quoted
        # candidate can contain a little more/less text than the Question cell.
        containment = [
            idx for idx, qn in enumerate(normalised_questions)
            if idx != current_idx
            and len(cn_no_code) >= 30
            and (cn_no_code in qn or qn in cn_no_code)
        ]
        if containment:
            containment.sort(key=lambda idx: abs(len(normalised_questions[idx]) - len(cn_no_code)))
            best = containment[0]
            if best not in parents:
                parents.append(best)
            continue

        # Conservative fuzzy fallback.
        scored = []
        c_tokens = set(cn_no_code.split())
        for idx, qn in enumerate(normalised_questions):
            if idx == current_idx or not qn:
                continue
            score = _question_similarity(cn_no_code, qn)
            overlap = len(c_tokens & set(qn.split())) / max(1, len(c_tokens))
            scored.append((score, overlap, idx))

        scored.sort(reverse=True)
        if scored:
            score, overlap, idx = scored[0]
            if score >= 0.70 and overlap >= 0.55:
                if idx not in parents:
                    parents.append(idx)

    return parents


def is_phd_advising_dependency(skip_text):
    text = str(skip_text).strip().casefold()
    if not text or text == "nan":
        return False
    return "phd" in text and ("degree" in text or "doctoral" in text)


def classify_question_roles(df):
    """Assign Independent/Mother/Follow-up roles and direct parent numbers,
    purely from question-level display logic (quote/containment/conservative-
    fuzzy matching against actual question text) -- no hardcoded question-
    number table. See _find_parent_question_indices for the matching rules.
    """
    questions = df["Question"].fillna("").astype(str).tolist()
    skip_logic = df["Skip/Display Logic"].fillna("").astype(str).tolist()

    parent_map = {}

    for row_idx, skip_text in enumerate(skip_logic):
        parents = _find_parent_question_indices(
            skip_text, questions, current_idx=row_idx
        )
        parents = [p for p in parents if p != row_idx]
        if parents:
            parent_map[row_idx] = parents

    children_of = {}
    for child_idx, parents in parent_map.items():
        for parent_idx in parents:
            children_of.setdefault(parent_idx, []).append(child_idx)

    roles = ["Independent Question"] * len(df)

    for child_idx in parent_map:
        roles[child_idx] = "Follow-up Question"

    # Any in-workbook parent that has a child is a mother unless it is itself
    # a follow-up in a larger chain.
    for parent_idx in children_of:
        if parent_idx not in parent_map:
            roles[parent_idx] = "Mother Question"

    # The one special degree-conditional item remains separately labelled.
    for row_idx, skip_text in enumerate(skip_logic):
        if roles[row_idx] == "Independent Question" and is_phd_advising_dependency(skip_text):
            roles[row_idx] = "PhD Advising Question"

    qnums = [clean_key(x) for x in df["Question #"].tolist()]
    parent_labels = []
    for row_idx in range(len(df)):
        labels = [qnums[p] for p in parent_map.get(row_idx, []) if qnums[p]]
        parent_labels.append("; ".join(dict.fromkeys(labels)))

    df["Parent Question #"] = parent_labels
    return pd.Series(roles, index=df.index)


def module_inclusion_applies(skip_text, selected_modules):
    """Evaluate simple HMS module-level inclusion rules for this survey selection."""
    text = str(skip_text).strip()
    if not is_module_level_skip(text):
        return True

    modules = re.findall(
        r"[\u2018\u2019'\u201c\u201d\"]([^\u2018\u2019'\u201c\u201d\"]+)[\u2018\u2019'\u201c\u201d\"]",
        text,
    )
    if not modules:
        return True

    selected = {_normalise_module_name(m) for m in selected_modules}
    referenced = {_normalise_module_name(m) for m in modules}
    lower = text.casefold()

    if "not selected" in lower or "not included" in lower:
        return referenced.isdisjoint(selected)
    if "selected" in lower or "included" in lower:
        return referenced.issubset(selected)
    return True


def apply_module_selection(df, survey_type):
    """No-op by explicit instruction: the only module-level condition that
    matters is Faculty Advising (PhD Students), which is handled separately
    in classify_question_roles() via is_phd_advising_dependency(). Every
    other module-level inclusion/exclusion note (e.g. "Included if 'X'
    module not selected") is intentionally ignored now -- rows are kept
    regardless of what those notes say. Left in place (rather than deleted)
    in case module-based filtering is wanted again later.
    """
    return df
    # --- previous behaviour, no longer used ---
    if survey_type != "hms":
        return df

    selected = {_normalise_module_name(m) for m in SELECTED_HMS_MODULES}
    logic = df["Skip/Display Logic"].fillna("")
    keep = logic.apply(lambda x: module_inclusion_applies(x, selected))
    removed = int((~keep).sum())
    if removed:
        print(f"Removed {removed} questions excluded by the selected HMS module set.")
    return df.loc[keep].copy()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_survey(input_path, output_path, survey_requested="auto"):
    """
    Runs the full scoring + workbook-writing pipeline for one survey file.
    Shared by both --input single-file mode and the auto-discovery loop in
    main(), so HMS, MECAMH, and any future survey all go through identical
    logic -- no per-survey branching except detect_survey_type()'s override.
    """
    df = read_input_file(input_path)
    survey_type = detect_survey_type(df, survey_requested)

    # Normalize expected input columns.
    if "Response Categories (Answer Format)" in df.columns and "Response Categories" not in df.columns:
        df = df.rename(
            columns={"Response Categories (Answer Format)": "Response Categories"}
        )
    for alt in ("Citation/Notes", "Notes/Citation", "Citation", "NOTES/CITATION"):
        if alt in df.columns and "Notes" not in df.columns:
            df = df.rename(columns={alt: "Notes"})
            break

    required = ["Module", "Section", "Question", "Response Categories"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(
            f"{input_path}: missing required columns: " + ", ".join(missing)
        )

    df["Question"] = df["Question"].fillna("")
    df["Response Categories"] = df["Response Categories"].fillna("")
    if "Notes" not in df.columns:
        df["Notes"] = ""
    df["Notes"] = df["Notes"].fillna("")

    # Fill in gaps left by placeholder Section values (blank, ".", "N/A",
    # etc.) in the source spreadsheet -- these are data-entry omissions,
    # not real section boundaries. A placeholder row is assumed to belong
    # to the SAME section as the nearest preceding real section value
    # within its module, which is how these gaps consistently show up in
    # practice (e.g. two follow-up items left with Section="." sitting
    # directly between two other rows correctly labeled "Satisfaction with
    # counseling/therapy"). Forward-fill is scoped per-module so a gap at
    # the very start of a module (no preceding section to inherit) is left
    # as-is rather than bleeding in a section name from a different module.
    SECTION_PLACEHOLDER_VALUES = {"", ".", "n/a", "na", "none", "nan", "-", "--"}
    df["Section"] = df["Section"].astype(str).str.strip()
    is_placeholder = df["Section"].str.lower().isin(SECTION_PLACEHOLDER_VALUES)
    if is_placeholder.any():
        df.loc[is_placeholder, "Section"] = pd.NA
        df["Section"] = df.groupby("Module", sort=False)["Section"].ffill()
        df["Section"] = df["Section"].fillna("(Unlabeled section)")

    # Remove excluded sections before any scoring is performed.
    df = remove_excluded_sections(df).reset_index(drop=True)


    if "Question #" not in df.columns:
        df["Question #"] = df.groupby("Module", sort=False).cumcount() + 1
    else:
        blank_qnum = df["Question #"].isna() | (
            df["Question #"].astype(str).str.strip() == ""
        )
        if blank_qnum.any():
            df.loc[blank_qnum, "Question #"] = (
                df.loc[blank_qnum].groupby("Module", sort=False).cumcount() + 1
            )

    # Question-level scoring logic. Per spec, readability/Bloom's/time-span
    # scoring reflects the FULL item -- question stem plus response options
    # -- not the question text alone, since a reader has to process both to
    # answer. Response options are folded in as their parsed labels (e.g.
    # "Not at all. Several days. More than half the days...") rather than
    # the raw "1=Not at all 2=..." coded string, so digit-equals noise
    # doesn't distort word/syllable counts.
    df["_scoring_text"] = df.apply(
        lambda r: combined_item_text(r["Question"], r["Response Categories"]),
        axis=1,
    )

    df["Flesch Reading Ease"] = df["_scoring_text"].apply(flesch_reading_ease)
    df["FRE Band"] = df["Flesch Reading Ease"].apply(fre_band)
    df["Blooms Score"] = df["_scoring_text"].apply(blooms_complexity)
    df["Time-span and Sensitivity"] = df["_scoring_text"].apply(time_sensitivity_complexity)

    df["QAS problems"] = df.apply(
        lambda r: classify_qas(r["Question"], r["Response Categories"]),
        axis=1,
    )

    df["Skip/Display Logic"] = df["Notes"].apply(extract_skip_logic)

    # Apply the selected-module configuration before scoring. A module-level
    # inclusion rule is evaluated against the selected HMS modules; only rows
    # that actually belong in this survey remain.
    df = apply_module_selection(df, survey_type)

    # Classify using actual question-to-question dependencies. Module-level
    # inclusion notes do NOT make a question a follow-up.
    df["Question Role"] = classify_question_roles(df)
    df["_is_followup"] = df["Question Role"].eq("Follow-up Question")

    detected = df.apply(
        lambda r: classify_response_type_for_survey(
            r["Response Categories"], survey_type, r["Question"], r["Notes"]
        ),
        axis=1,
    )
    df["_response_type_internal"] = detected.apply(lambda t: t[0])
    df["_response_options"] = detected.apply(lambda t: t[1])

    # Apply the manually reviewed corrections before dependent metrics.
    df["_response_type_internal"] = df.apply(
        lambda r: apply_response_type_override(r, r["_response_type_internal"]),
        axis=1,
    )

    roc_results = df.apply(
        lambda r: response_option_complexity_from_type(
            r["_response_type_internal"],
            r["Response Categories"],
            r["Flesch Reading Ease"],
            r["QAS problems"],
        ),
        axis=1,
    )
    df["Response Option Complexity"] = roc_results.apply(lambda t: t[0])
    df["Response Option Complexity Score"] = roc_results.apply(lambda t: t[1])

    df["Cognitive Load Score"] = df.apply(composite_cognitive_load, axis=1)
    df["Cognitive Load Band"] = df["Cognitive Load Score"].apply(load_category)

    # Only mother/anchor questions receive cognitive-load scores. Follow-up
    # rows remain in the survey but do not contribute to cognitive load.
    followup_mask = df["_is_followup"]
    cognitive_cols = [
        "Flesch Reading Ease",
        "FRE Band",
        "Blooms Score",
        "Response Option Complexity",
        "Response Option Complexity Score",
        "Time-span and Sensitivity",
        "QAS problems",
        "Cognitive Load Score",
        "Cognitive Load Band",
    ]
    df.loc[followup_mask, cognitive_cols] = pd.NA

    # Translate to the mentor-reviewed display labels (Boolean, Likert
    # (N-point), Categorical (Ordinal/Nominal), Matrix, Open-ended / Other,
    # Numeric) only now -- everything above used the internal labels that
    # drive matrix detection, complexity rules, and overrides.
    df["Response Type"] = df.apply(
        lambda r: display_response_type(r["_response_type_internal"], r["_response_options"]),
        axis=1,
    )

    # Keep original module order and section order. Reordering is delegated
    # entirely to reorder_cognitive_load.py.
    df["_module_order"] = pd.factorize(df["Module"], sort=False)[0]
    df["_section_order"] = pd.factorize(
        df["Module"].astype(str) + "\u241f" + df["Section"].astype(str),
        sort=False,
    )[0]
    df = df.sort_values(["_module_order", "_section_order", "Question #"]).drop(
        columns=["_module_order", "_section_order", "_response_type_internal",
                 "_response_options", "_scoring_text", "_is_followup"]
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        used_names = set()

        for module in df["Module"].drop_duplicates():
            sheet_name = sanitize_sheet_name(module, used_names)
            sub = df[df["Module"] == module]

            sub.to_excel(
                writer,
                sheet_name=sheet_name,
                index=False,
                columns=OUT_COLS,
            )

            ws = writer.book[sheet_name]
            ws.freeze_panes = "A2"
            style_sheet(ws, OUT_COLS)

        write_readme_sheet(writer, len(df))

    matrix_count = int((df["Response Type"] == "Matrix").sum())
    skip_count = int((df["Skip/Display Logic"] != "").sum())
    print(f"[{survey_type.upper()}] {input_path} -> {output_path}")
    print(f"  Scored {len(df)} questions across {df['Module'].nunique()} modules")
    print(f"  Matrix/grid questions detected: {matrix_count}")
    print(f"  Questions with skip/display logic preserved: {skip_count}")

    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.input:
        # Single-file mode (explicit --input): process just that one file.
        output_path = args.output or (args.default_outputs / "question_score.xlsx")
        process_survey(args.input, output_path, args.survey)
        return

    # Auto-discovery mode (no --input given): process every known survey
    # found under csvs/ in one run, HMS and MECAMH included automatically.
    processed_any = False
    for input_name, output_name, survey_type in KNOWN_SURVEYS:
        input_path = args.default_csvs / input_name
        if not input_path.exists():
            print(f"Skipping {survey_type.upper()}: {input_path} not found")
            continue
        output_path = args.default_outputs / output_name
        process_survey(input_path, output_path, survey_type)
        processed_any = True

    if not processed_any:
        raise FileNotFoundError(
            f"No known survey input files found under {args.default_csvs}. "
            "Pass --input explicitly to score a single file."
        )


if __name__ == "__main__":
    main()