#!/usr/bin/env python3
"""
Two independent fixes, applied together:

1. app.js: multi-select ("choice-list-multi") checkbox questions now honor
   `option.exclusive === true` from the schema. Checking an exclusive option
   (e.g. "No, never been diagnosed...") clears every other checkbox in the
   question; checking anything else clears any exclusive option that was
   previously checked. This is generic -- it applies to every multi-select
   question in the schema that has an `exclusive: true` option, not just
   the chronic-disease question that surfaced the bug.

   Also adds a `dropdown` question type (renderer + change handler) for
   use by the sleep-hours schema fix below.

2. study_schema.js:
   - hms-mental-health-status-q20 / q21 (hours of sleep on weeknights /
     weekend nights) switch from free-text `type: "text"` to a `dropdown`
     with whole-hour options 0-12, "12 or more hours" as the top bucket.
   - hms-overall-health-q2 (chronic disease): "Don't know" (value "15")
     is marked `exclusive: true` alongside "No, never been diagnosed..."
     (value "14"), since neither should coexist with a specific diagnosis.

3. styles.css: adds `.text-input-wrap select` styling so the new dropdown
   matches the existing text/number input look.

Usage:
    python3 apply_multi_exclusive_and_dropdown_patch.py \
        /path/to/app.js /path/to/study_schema.js /path/to/styles.css
"""
import sys


def patch_file(path, steps):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    for label, old, new in steps:
        if old not in src:
            print(f"ERROR ({path}): could not find exact block for: {label}")
            sys.exit(1)
        if src.count(old) > 1:
            print(f"ERROR ({path}): block for {label} is not unique, aborting")
            sys.exit(1)
        src = src.replace(old, new)
        print(f"SUCCESS ({path}): patched {label}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)


def patch_app_js(path):
    steps = []

    # --- 1. Add renderDropdown() right after renderTextInput() ---
    old_render_text = '''  // Single open-text input.
  function renderTextInput(q) {
    const val = state.answers[q.id] || "";
    return `<div class="text-input-wrap"><input type="text" id="text-answer" value="${val}" /></div>`;
  }'''
    new_render_text = old_render_text + '''

  // Single-select dropdown for a small fixed set of numeric/ordinal
  // values (e.g. hours of sleep) where a full choice-card list would be
  // too visually heavy.
  function renderDropdown(q) {
    const val = state.answers[q.id] || "";
    const optionsHtml = q.options
      .map(
        (opt) => `<option value="${opt.value}" ${opt.value === val ? "selected" : ""}>${opt.label}</option>`
      )
      .join("");
    return `<div class="text-input-wrap"><select id="text-answer"><option value="" disabled ${
      val ? "" : "selected"
    }>Select…</option>${optionsHtml}</select></div>`;
  }'''
    steps.append(("add renderDropdown()", old_render_text, new_render_text))

    # --- 2. Wire up the dispatcher ---
    old_dispatch = '''    else if (q.type === "text") optionsMarkup = renderTextInput(q);
    else if (q.type === "matrix") optionsMarkup = renderMatrix(q);'''
    new_dispatch = '''    else if (q.type === "text") optionsMarkup = renderTextInput(q);
    else if (q.type === "dropdown") optionsMarkup = renderDropdown(q);
    else if (q.type === "matrix") optionsMarkup = renderMatrix(q);'''
    steps.append(("wire dropdown into render dispatcher", old_dispatch, new_dispatch))

    # --- 3. Multi-select exclusivity + dropdown change handler ---
    old_handlers = '''    if (q.type === "multi") {
      document.querySelectorAll('input[name="answer"]').forEach((input) => {
        input.addEventListener("change", () => {
          const checked = Array.from(document.querySelectorAll('input[name="answer"]:checked')).map((i) => i.value);
          state.answers[q.id] = checked;
          nextBtn.disabled = checked.length === 0;
        });
      });
    } else if (q.type === "numeric" || q.type === "text") {
      const input = document.getElementById("text-answer");
      input.addEventListener("input", () => {
        state.answers[q.id] = input.value;
        nextBtn.disabled = input.value.trim() === "";
      });
    } else if (q.type === "matrix") {'''
    new_handlers = '''    if (q.type === "multi") {
      // Options flagged `exclusive: true` in the schema (e.g. "No, never
      // been diagnosed...", "Don't know", "None of the above") can't
      // coexist with any other selection in this question: checking one
      // clears every other checkbox, and checking anything else clears
      // any exclusive option that was previously checked.
      const exclusiveValues = new Set(q.options.filter((opt) => opt.exclusive).map((opt) => opt.value));
      document.querySelectorAll('input[name="answer"]').forEach((input) => {
        input.addEventListener("change", (e) => {
          const allInputs = document.querySelectorAll('input[name="answer"]');
          if (e.target.checked) {
            if (exclusiveValues.has(e.target.value)) {
              allInputs.forEach((other) => {
                if (other !== e.target) other.checked = false;
              });
            } else {
              allInputs.forEach((other) => {
                if (exclusiveValues.has(other.value)) other.checked = false;
              });
            }
          }
          const checked = Array.from(document.querySelectorAll('input[name="answer"]:checked')).map((i) => i.value);
          state.answers[q.id] = checked;
          nextBtn.disabled = checked.length === 0;
        });
      });
    } else if (q.type === "numeric" || q.type === "text") {
      const input = document.getElementById("text-answer");
      input.addEventListener("input", () => {
        state.answers[q.id] = input.value;
        nextBtn.disabled = input.value.trim() === "";
      });
    } else if (q.type === "dropdown") {
      const input = document.getElementById("text-answer");
      input.addEventListener("change", () => {
        state.answers[q.id] = input.value;
        nextBtn.disabled = input.value === "";
      });
    } else if (q.type === "matrix") {'''
    steps.append(("add multi-select exclusivity + dropdown handler", old_handlers, new_handlers))

    patch_file(path, steps)


def patch_study_schema_js(path):
    steps = []

    old_q20 = '''        {
          id: "hms-mental-health-status-q20",
          type: "text",
          stem: "On average, how many hours of sleep, on average, do you get on weeknights?",
          options: [],
        },'''
    new_q20 = '''        {
          id: "hms-mental-health-status-q20",
          type: "dropdown",
          stem: "On average, how many hours of sleep, on average, do you get on weeknights?",
          options: [
            { value: "0", label: "0 hours" },
            { value: "1", label: "1 hour" },
            { value: "2", label: "2 hours" },
            { value: "3", label: "3 hours" },
            { value: "4", label: "4 hours" },
            { value: "5", label: "5 hours" },
            { value: "6", label: "6 hours" },
            { value: "7", label: "7 hours" },
            { value: "8", label: "8 hours" },
            { value: "9", label: "9 hours" },
            { value: "10", label: "10 hours" },
            { value: "11", label: "11 hours" },
            { value: "12", label: "12 or more hours" },
          ],
        },'''
    steps.append(("hms-mental-health-status-q20 -> dropdown", old_q20, new_q20))

    old_q21 = '''        {
          id: "hms-mental-health-status-q21",
          type: "text",
          stem: "On average, year, how many hours of sleep, on average, do you get on weekend nights?",
          options: [],
        },'''
    new_q21 = '''        {
          id: "hms-mental-health-status-q21",
          type: "dropdown",
          stem: "On average, year, how many hours of sleep, on average, do you get on weekend nights?",
          options: [
            { value: "0", label: "0 hours" },
            { value: "1", label: "1 hour" },
            { value: "2", label: "2 hours" },
            { value: "3", label: "3 hours" },
            { value: "4", label: "4 hours" },
            { value: "5", label: "5 hours" },
            { value: "6", label: "6 hours" },
            { value: "7", label: "7 hours" },
            { value: "8", label: "8 hours" },
            { value: "9", label: "9 hours" },
            { value: "10", label: "10 hours" },
            { value: "11", label: "11 hours" },
            { value: "12", label: "12 or more hours" },
          ],
        },'''
    steps.append(("hms-mental-health-status-q21 -> dropdown", old_q21, new_q21))

    old_dont_know = '''            { value: "14", label: "No, never been diagnosed with a chronic disease.", exclusive: true },
            { value: "15", label: "Don\u2019t know" },'''
    new_dont_know = '''            { value: "14", label: "No, never been diagnosed with a chronic disease.", exclusive: true },
            { value: "15", label: "Don\u2019t know", exclusive: true },'''
    steps.append(('chronic-disease "Don\'t know" -> exclusive', old_dont_know, new_dont_know))

    patch_file(path, steps)


def patch_styles_css(path):
    old_css = '''.text-input-wrap input {
  width: 100%;
  padding: 0.85rem 1rem;
  font-size: 1rem;
  border: 1px solid var(--border-color, #d0d5dd);
  border-radius: 0.5rem;
  box-sizing: border-box;
}'''
    new_css = old_css + '''
.text-input-wrap select {
  width: 100%;
  padding: 0.85rem 1rem;
  font-size: 1rem;
  border: 1px solid var(--border-color, #d0d5dd);
  border-radius: 0.5rem;
  box-sizing: border-box;
  background: #fff;
  font-family: inherit;
  cursor: pointer;
}'''
    patch_file(path, [("add .text-input-wrap select styling", old_css, new_css)])


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 apply_multi_exclusive_and_dropdown_patch.py "
              "/path/to/app.js /path/to/study_schema.js /path/to/styles.css")
        sys.exit(1)
    app_js_path, schema_path, css_path = sys.argv[1:4]
    patch_app_js(app_js_path)
    patch_study_schema_js(schema_path)
    patch_styles_css(css_path)
    print("\nAll patches applied successfully.")
