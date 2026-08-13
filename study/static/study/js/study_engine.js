/* ==========================================================================
   study_engine.js — Branching traversal over STUDY_MODULES
   ----------------------------------------------------------------------
   Owns a single cursor {m, s, q} into the active modules/sections/questions
   and resolves it forward, skipping anything hidden by showIf/skipIf, one
   step at a time. Branching is evaluated live against whatever has been
   answered so far — nothing is precomputed/flattened up front, so answers
   given mid-study can open or close later branches correctly.

   REORDERING
   ----------
   Controlled entirely from js/study_config.js — none of it requires moving
   content blocks around in study_schema.js:
     - Module order   -> STUDY_CONFIG.activeModuleIds (array order = run order)
     - Section order  -> STUDY_CONFIG.sectionOrder[moduleId] = [id, id, ...]
     - Question order -> STUDY_CONFIG.questionOrder[sectionId] = [id, id, ...]
   If a module/section is missing from those maps, the engine falls back to
   an inline `sectionOrder`/`questionOrder` array on that module/section in
   study_schema.js if one is present there, and finally to plain schema
   array order (mod.sections / sec.questions) if neither is set anywhere.

   Usage (see app.js):
     StudyEngine.init(window.STUDY_MODULES, window.STUDY_CONFIG.activeModuleIds,
                       (questionId) => state.answers[questionId]);
     StudyEngine.start();                 // seek to first visible question
     StudyEngine.getContext();            // { module, section, question, ... }
     StudyEngine.next();                  // advance past current question
     StudyEngine.isComplete();
     StudyEngine.getProgress();           // { percent, moduleNumber, ... }
   ========================================================================== */

const StudyEngine = (() => {
  let modules = [];
  let getAnswer = () => undefined;
  let cursor = { m: 0, s: 0, q: 0 };

  /* ---------------------------------------------------------------- */
  /* Ordering helpers                                                   */
  /* ---------------------------------------------------------------- */

  // Reorders `items` (each with an `.id`) according to `orderIds` if given;
  // otherwise returns `items` as-is. Unknown ids in orderIds are silently
  // dropped; items missing from orderIds are silently dropped too — an
  // order array is treated as authoritative once present.
  function applyOrder(items, orderIds) {
    if (!orderIds) return items;
    const byId = new Map(items.map((item) => [item.id, item]));
    return orderIds.map((id) => byId.get(id)).filter(Boolean);
  }

  // Ordering priority: js/study_config.js (STUDY_CONFIG.sectionOrder /
  // .questionOrder) first, falling back to an inline sectionOrder/
  // questionOrder written directly on the module/section in
  // study_schema.js (legacy/local override), falling back to plain
  // schema array order if neither is present.
  function configSectionOrder(mod) {
    const cfg = window.STUDY_CONFIG && window.STUDY_CONFIG.sectionOrder;
    return (cfg && cfg[mod.id]) || mod.sectionOrder;
  }

  function configQuestionOrder(sec) {
    const cfg = window.STUDY_CONFIG && window.STUDY_CONFIG.questionOrder;
    return (cfg && cfg[sec.id]) || sec.questionOrder;
  }

  function orderedSections(mod) {
    return applyOrder(mod.sections, configSectionOrder(mod));
  }

  function orderedQuestions(sec) {
    return applyOrder(sec.questions, configQuestionOrder(sec));
  }

  /* ---------------------------------------------------------------- */
  /* Condition evaluation                                              */
  /* ---------------------------------------------------------------- */

  function evalCondition(cond) {
    const val = getAnswer(cond.questionId);
    if (val === undefined) return false;
    if ("equals" in cond) return val === cond.equals;
    if ("notEquals" in cond) return val !== cond.notEquals;
    if ("in" in cond) return cond.in.includes(val);
    if ("notIn" in cond) return !cond.notIn.includes(val);
    // Unrecognized condition shape — fail safe by showing the item rather
    // than silently hiding study content.
    console.warn("[StudyEngine] Unrecognized condition shape:", cond);
    return true;
  }

  // showIf: item is shown only when the condition is true. No showIf -> always shown.
  function shouldShow(cond) {
    if (!cond) return true;
    return evalCondition(cond);
  }

  // skipIf: item is skipped when the condition is true. No skipIf -> never skipped.
  function shouldSkip(cond) {
    if (!cond) return false;
    return evalCondition(cond);
  }

  /* ---------------------------------------------------------------- */
  /* Cursor resolution                                                  */
  /* ---------------------------------------------------------------- */

  // Walks the cursor forward (never backward) until it lands on a visible
  // question, or falls off the end of the last module. Returns true if a
  // visible question is now under the cursor, false if the study is done.
  // cursor.s / cursor.q index into the *ordered* sections/questions lists,
  // not necessarily the raw array position in study_schema.js.
  function resolveCursor() {
    while (cursor.m < modules.length) {
      const mod = modules[cursor.m];

      if (shouldSkip(mod.skipIf)) {
        cursor.m += 1;
        cursor.s = 0;
        cursor.q = 0;
        continue;
      }

      const sections = orderedSections(mod);

      if (cursor.s >= sections.length) {
        cursor.m += 1;
        cursor.s = 0;
        cursor.q = 0;
        continue;
      }

      const sec = sections[cursor.s];

      if (shouldSkip(sec.skipIf)) {
        cursor.s += 1;
        cursor.q = 0;
        continue;
      }

      const questions = orderedQuestions(sec);

      if (cursor.q >= questions.length) {
        cursor.s += 1;
        cursor.q = 0;
        continue;
      }

      const q = questions[cursor.q];

      if (!shouldShow(q.showIf)) {
        cursor.q += 1;
        continue;
      }

      return true; // cursor points at a valid, visible question
    }
    return false;
  }

  /* ---------------------------------------------------------------- */
  /* Public API                                                         */
  /* ---------------------------------------------------------------- */

  // Module run order is dictated entirely by activeModuleIds — modules are
  // looked up by id and placed in that array's order, not their position in
  // allModules. Reordering the study is just reordering this id list.
  function init(allModules, activeModuleIds, answerGetter) {
    const byId = new Map((allModules || []).map((m) => [m.id, m]));
    modules = (activeModuleIds || []).map((id) => byId.get(id)).filter(Boolean);
    getAnswer = typeof answerGetter === "function" ? answerGetter : () => undefined;
    cursor = { m: 0, s: 0, q: 0 };
  }

  function start() {
    cursor = { m: 0, s: 0, q: 0 };
    return resolveCursor();
  }

  // Call after recording the answer to the current question.
  function next() {
    cursor.q += 1;
    return resolveCursor();
  }

  function isComplete() {
    return !resolveCursor();
  }

  function getContext() {
    if (!resolveCursor()) {
      return { module: null, section: null, question: null };
    }
    const module = modules[cursor.m];
    const section = orderedSections(module)[cursor.s];
    const question = orderedQuestions(section)[cursor.q];
    return { module, section, question, moduleIndex: cursor.m, sectionIndex: cursor.s, questionIndex: cursor.q };
  }

  function currentQuestionId() {
    const ctx = getContext();
    return ctx.question ? ctx.question.id : null;
  }

  // Rough progress estimate. Because branching means the true remaining
  // question count isn't knowable in advance, this is based on module +
  // section position rather than question count — monotonic and good
  // enough for a progress bar, capped below 100% until actually complete.
  function getProgress() {
    const total = modules.length || 1;
    if (!resolveCursor()) {
      return { percent: 100, moduleNumber: total, moduleTotal: total, moduleTitle: "", sectionNumber: 0, sectionTotal: 0, sectionTitle: "" };
    }
    const mod = modules[cursor.m];
    const sections = orderedSections(mod);
    const sectionTotal = sections.length || 1;
    const moduleFrac = cursor.m / total;
    const sectionFrac = (cursor.s / sectionTotal) / total;
    const percent = Math.min(99, Math.round((moduleFrac + sectionFrac) * 100));
    return {
      percent,
      moduleNumber: cursor.m + 1,
      moduleTotal: total,
      moduleTitle: mod.title,
      sectionNumber: cursor.s + 1,
      sectionTotal: sectionTotal,
      sectionTitle: sections[cursor.s].title
    };
  }

  // Upper-bound question count ignoring all branching (i.e. as if every
  // showIf/skipIf were satisfied). Useful for "up to N questions" copy on
  // an instructions screen — not an exact count, since real branching will
  // always show fewer than this.
  function estimateMaxQuestions(allModules, activeModuleIds) {
    const byId = new Map((allModules || []).map((m) => [m.id, m]));
    return (activeModuleIds || [])
      .map((id) => byId.get(id))
      .filter(Boolean)
      .reduce((total, m) => total + orderedSections(m).reduce((s, sec) => s + orderedQuestions(sec).length, 0), 0);
  }

  return {
    init,
    start,
    next,
    isComplete,
    getContext,
    currentQuestionId,
    getProgress,
    estimateMaxQuestions
  };
})();