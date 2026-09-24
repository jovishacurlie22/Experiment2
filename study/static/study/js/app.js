/* ==========================================================================
   Study 2 — Healthcare Survey app logic
   ----------------------------------------------------------------------
   Static single-page preview. Screens are swapped inside #app-root.
   Session timer is anchored via sessionStorage per Experiment 1 convention
   (absolute end timestamp, survives in-tab navigation, resets on new tab).

   Navigation is driven by StudyEngine (js/study_engine.js) walking the
   module/section/question schema in js/study_schema.js — see those files
   for the data model, branching rules, and reordering hooks. This file
   only renders whatever question StudyEngine currently points at and
   records the answer.

   Session integrity behaviors in this file:
     - Fullscreen is required, not just requested: a full-viewport gate
       blocks every interaction on every post-login screen whenever the
       browser isn't in fullscreen, and only the gate's own button (a
       fresh user gesture) can dismiss it, since browsers won't let JS
       silently re-enter fullscreen.
     - The PAAS effort scale is click-to-select (radio-style, 1–9), not a
       slider — nothing is pre-selected, so clicking "5" is itself a valid,
       explicit answer. Next stays disabled until something is clicked.
     - Browser back/forward is trapped for the duration of the study so a
       participant can't navigate to a previous question.
     - On both the in-app "End Study" button and an unexpected tab close,
       we make a best-effort call to flush/stop the recording pipeline
       before the page goes away.
   ========================================================================== */

(() => {
  const SESSION_MINUTES = 30;
  const TIMER_KEY = "study2_session_end_ts";
  // Participant IDs + password are validated for real server-side in
  // study/views.py:login_view. window.STUDY_PARTICIPANT_IDS is injected by
  // the Django template (study/index.html) purely so the login form can
  // give instant "unknown ID" feedback before round-tripping to the server.
  const PARTICIPANT_IDS = window.STUDY_PARTICIPANT_IDS || Array.from({ length: 30 }, (_, i) => String(i + 1));

  // Real schema/config (study_schema.js, study_config.js) vs. the demo
  // schema/config (demo_study_schema.js) used only for the "DEMO" login --
  // both are always loaded on the page (see index.html) under distinct
  // globals so neither file has to be swapped in/out or edited.
  const REAL_MODULES = window.STUDY_MODULES || [];
  const DEMO_MODULES = window.DEMO_STUDY_MODULES || [];
  const REAL_CONFIG_ASC = window.STUDY_CONFIG_ASC || window.STUDY_CONFIG || { activeModuleIds: [] };
  const REAL_CONFIG_DESC = window.STUDY_CONFIG_DESC || REAL_CONFIG_ASC;
  const DEMO_CONFIG_ASC = window.DEMO_STUDY_CONFIG_ASC || { activeModuleIds: [] };
  const DEMO_CONFIG_DESC = window.DEMO_STUDY_CONFIG_DESC || DEMO_CONFIG_ASC;

  // Must match settings.DEMO_PARTICIPANT_ID server-side (views.py compares
  // case-insensitively; the login form always uppercases what's typed --
  // see the click handler on #btn-login below -- so "DEMO" is what
  // state.participantId will actually hold).
  const DEMO_PARTICIPANT_ID = "DEMO";

  function isDemoParticipant() {
    return (state.participantId || "").toUpperCase() === DEMO_PARTICIPANT_ID;
  }

  // Resolved once we know who's logged in, not at script-load time.
  function getActiveModules() {
    return isDemoParticipant() ? DEMO_MODULES : REAL_MODULES;
  }

  // Only used for the "up to N questions" estimate on the instructions
  // screen — that count is order-independent, so the ASC config for
  // whichever schema is active will do here.
  function getMaxQuestionsEstimate() {
    const config = isDemoParticipant() ? DEMO_CONFIG_ASC : REAL_CONFIG_ASC;
    return StudyEngine.estimateMaxQuestions(getActiveModules(), config.activeModuleIds);
  }

  // Flat id -> question lookup across every module/section, built lazily
  // (only once we know which schema is active) from the raw schema (not the
  // branching-aware StudyEngine view, since a pipe-in source question is
  // very often NOT the current question). Used only to resolve pipeInItems
  // (see resolvePipeInItems below) against the source question's option list.
  let _questionsById = null;
  function getQuestionsById() {
    if (!_questionsById) {
      _questionsById = new Map();
      (getActiveModules() || []).forEach((mod) => {
        (mod.sections || []).forEach((sec) => {
          (sec.questions || []).forEach((q) => _questionsById.set(q.id, q));
        });
      });
    }
    return _questionsById;
  }

  // Resolves a matrix question's rows for "pipe-in" survey items -- i.e.
  // matrix rows that aren't fixed at schema-authoring time but are instead
  // whatever the participant selected earlier in a prior multi-select
  // question (survey-tool "carry forward choices" pattern; CCMH source
  // Notes describe these as "[pipe in selected options from: ...]").
  //
  // q.pipeInItems: { fromQuestionId, template?, filter? }
  //   - no `template`: one row per selected option of fromQuestionId,
  //     labeled with that option's own label (e.g. q26 rows = the places
  //     the participant said they got counseling from).
  //   - `template` present: cross-product of the selected options with a
  //     fixed list of sub-items, one row per (selected option x template
  //     item) -- see resolvePipeInGroups below for the one-screen-per-
  //     option alternative to flattening these into a single big matrix.
  function resolveSelectedPipeInOptions(q) {
    const source = getQuestionsById().get(q.pipeInItems.fromQuestionId);
    const rawAnswer = state.answers[q.pipeInItems.fromQuestionId];
    const selectedValues = Array.isArray(rawAnswer) ? rawAnswer : rawAnswer != null ? [rawAnswer] : [];
    if (!source || selectedValues.length === 0) return [];
    const optionsByValue = new Map((source.options || []).map((opt) => [opt.value, opt]));
    let selectedOptions = selectedValues.map((v) => optionsByValue.get(v)).filter(Boolean);

    // Optional filter: narrow selectedOptions to only those whose
    // corresponding row in ANOTHER pipe-in matrix (keyed the same way,
    // "<matrixQuestionId>-pipe-<value>") currently holds one of a given
    // set of answers -- e.g. q29 only wants the places q26 marked as
    // remote/both, not every place selected in q25.
    if (q.pipeInItems.filter) {
      const { matrixQuestionId, in: allowedValues } = q.pipeInItems.filter;
      const matrixAnswers = state.answers[matrixQuestionId];
      selectedOptions = selectedOptions.filter((opt) => {
        const rowId = `${matrixQuestionId}-pipe-${opt.value}`;
        const rowValue = matrixAnswers && typeof matrixAnswers === "object" ? matrixAnswers[rowId] : undefined;
        return allowedValues.includes(rowValue);
      });
    }
    return selectedOptions;
  }

  // Whether ONE piped-in place counts as a digital resource. That isn't a
  // property of the place itself -- it's the participant's own answer for
  // that place in the earlier "how were your sessions conducted" matrix
  // (keyed "<matrixQuestionId>-pipe-<value>", same as the q.pipeInItems.filter
  // lookup above). q.pipeInItems.hideForDigitalWhen names that matrix and
  // the answer codes that mean remote-only.
  function isDigitalPlace(q, opt) {
    const cond = q.pipeInItems.hideForDigitalWhen;
    if (!cond) return false;
    const answers = state.answers[cond.matrixQuestionId];
    const rowValue = answers && typeof answers === "object" ? answers[`${cond.matrixQuestionId}-pipe-${opt.value}`] : undefined;
    return cond.in.includes(rowValue);
  }

  // The template rows that apply to ONE piped-in place. A row flagged
  // hideForDigital (source annotation: "Location [Do not display for
  // digital resources]") is dropped when that place is a digital one. Hidden
  // rows never render and are never required, so they don't block "Next" or
  // appear in the stored answer.
  function templateRowsFor(q, opt) {
    const digital = isDigitalPlace(q, opt);
    return q.pipeInItems.template.filter((tmpl) => !(tmpl.hideForDigital && digital));
  }

  // Whether one ordinary (non-piped) matrix row should currently be shown.
  // item.showIfWhen: { questionId, in: [...] } -- shown only once the
  // named question's own answer includes one of the given option codes.
  // The named question is typically a multi-select earlier in the same
  // section (e.g. Financing education's "how did you pay for school"
  // question), so its stored answer is an array of selected codes; a
  // single-value answer is normalized into a one-element array so the
  // same check works either way. No showIfWhen -> always shown. This is
  // the item-level counterpart to StudyEngine's question-level showIf --
  // a whole matrix question can have some always-shown rows alongside
  // rows conditional on an earlier answer (see the loan-repayment worry
  // statements, which only apply if loans were selected as a funding
  // source above them).
  function itemVisible(item) {
    const cond = item.showIfWhen;
    if (!cond) return true;
    const val = state.answers[cond.questionId];
    if (val === undefined || val === null) return false;
    const arr = Array.isArray(val) ? val : [val];
    return cond.in.some((code) => arr.includes(code));
  }

  // Falls back to q.items (or []) for ordinary, non-piped matrix questions,
  // filtered by itemVisible() so a conditional row (showIfWhen) never
  // renders, blocks "Next", or gets stored until its condition is met.
  // For a template pipe-in, this is the flattened "one big matrix" shape
  // (rows labeled "<aspect> — <place>") -- see resolvePipeInGroups for the
  // one-screen-per-place alternative, which is what renderQuestion() below
  // actually uses for template pipe-ins now.
  function resolvePipeInItems(q) {
    if (!q.pipeInItems) return (q.items || []).filter(itemVisible);
    const selectedOptions = resolveSelectedPipeInOptions(q);

    if (!q.pipeInItems.template) {
      return selectedOptions.map((opt) => ({ id: `${q.id}-pipe-${opt.value}`, label: opt.label }));
    }
    const rows = [];
    selectedOptions.forEach((opt) => {
      templateRowsFor(q, opt).forEach((tmpl) => {
        rows.push({ id: `${q.id}-pipe-${opt.value}-${tmpl.id}`, label: `${tmpl.label} — ${opt.label}` });
      });
    });
    return rows;
  }

  // One screen per selected pipe-in option, for a template pipe-in matrix
  // (q28-style: 6 aspects, once per place selected) -- e.g. a participant
  // who picked 3 places gets 3 short screens (just the 6 aspects each),
  // instead of one 18-row wall with the place name repeated in every row
  // label. The place name goes in the question stem instead (see
  // q.pipeInItems.stemTemplate, substituted in renderQuestion() below).
  // Returns null for anything that isn't a template pipe-in (an ordinary
  // matrix, or a simple pipe-in like q26 where rows already ARE the
  // places) -- callers use that null to fall back to the normal
  // resolvePipeInItems() + row-count pagination path.
  function resolvePipeInGroups(q) {
    if (!q.pipeInItems || !q.pipeInItems.template) return null;
    const selectedOptions = resolveSelectedPipeInOptions(q);
    return selectedOptions.map((opt) => ({
      value: opt.value,
      label: opt.label,
      rows: templateRowsFor(q, opt).map((tmpl) => ({ id: `${q.id}-pipe-${opt.value}-${tmpl.id}`, label: tmpl.label })),
    }));
  }

  // Odd participant IDs -> ascending cognitive-load order.
  // Even participant IDs -> descending cognitive-load order.
  function getActiveConfig() {
    const idNum = parseInt(state.participantId, 10);
    const isEven = Number.isFinite(idNum) && idNum % 2 === 0;
    if (isDemoParticipant()) {
      return isEven ? DEMO_CONFIG_DESC : DEMO_CONFIG_ASC;
    }
    return isEven ? REAL_CONFIG_DESC : REAL_CONFIG_ASC;
  }
  // Standard 9-point PAAS mental-effort scale — every point gets its own
  // explicit label (not just the two endpoints) so nothing is ambiguous.
  const EFFORT_SCALE = [
    { value: 1, label: "very, very low mental effort" },
    { value: 2, label: "very low mental effort" },
    { value: 3, label: "low mental effort" },
    { value: 4, label: "rather low mental effort" },
    { value: 5, label: "neither low nor high mental effort" },
    { value: 6, label: "rather high mental effort" },
    { value: 7, label: "high mental effort" },
    { value: 8, label: "very high mental effort" },
    { value: 9, label: "very, very high mental effort" }
  ];

  const state = {
    screen: "consent",
    consentGivenAt: null,
    participantId: null,
    sessionKey: null, // set on successful server-side login (StudySession.session_key)
    answers: {},   // qid -> option value
    answerLastChangedAt: {}, // qid -> ISO timestamp of the most recent change to that answer
    answerSubmittedAtMs: {},     // qid -> epoch ms of the click on the question screen's Next button
    questionPresentedAtMs: null, // epoch ms: when the current question screen appeared
    effort: {},    // qid -> paas rating 1-9
    currentModuleId: null, // last module id a module-intro screen was shown for
    questionPresentedAt: null, // ISO timestamp: when the current question screen appeared
    ended: false,
    endReason: null
  };

  const root = document.getElementById("app-root");
  const topbar = document.getElementById("topbar");
  const progressWrap = document.getElementById("progress-wrap");
  const timerEl = document.getElementById("session-timer");
  const endStudyBtn = document.getElementById("btn-end-study");
  const modalBackdrop = document.getElementById("modal-backdrop");

  /* ---------------------------------------------------------------- */
  /* Fullscreen gate — hard block, not a nudge                         */
  /* ---------------------------------------------------------------- */

  // Screens that require fullscreen to interact with at all. Login is
  // deliberately excluded — that's where the first fullscreen request is
  // made, from the login button's own click gesture.
  const FULLSCREEN_REQUIRED_SCREENS = ["instructions", "moduleIntro", "question", "rating", "saving"];

  function isFullscreenActive() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function enterFullscreen() {
    const el = document.documentElement;
    const request = el.requestFullscreen || el.webkitRequestFullscreen || el.msRequestFullscreen;
    if (!request) return;
    request.call(el).catch((err) => {
      // Fullscreen can be rejected (e.g. embedded in an iframe without the
      // `allow="fullscreen"` attribute, or browser/OS policy). The gate
      // below will stay up and let the participant retry via its button.
      console.warn("[app] Could not enter fullscreen:", err);
    });
  }

  function exitFullscreen() {
    if (!isFullscreenActive()) return;
    const exit = document.exitFullscreen || document.webkitExitFullscreen;
    if (exit) exit.call(document).catch(() => {});
  }

  // Full-viewport blocking overlay. Sits above everything (z-index in CSS)
  // so nothing underneath is clickable while it's visible — the only
  // interactive element is its own "Enter Fullscreen" button.
  const fullscreenGate = document.createElement("div");
  fullscreenGate.className = "fullscreen-gate";
  fullscreenGate.innerHTML = `
    <div class="fullscreen-gate-card">
      <h2>Fullscreen Required</h2>
      <p>This study must be completed in fullscreen mode. Please click below to continue — you won't be able to proceed until fullscreen is active.</p>
      <button class="btn btn-primary" id="btn-enter-fullscreen-gate">Enter Fullscreen</button>
    </div>
  `;
  document.body.appendChild(fullscreenGate);
  fullscreenGate.querySelector("#btn-enter-fullscreen-gate").addEventListener("click", enterFullscreen);

  function updateFullscreenGate() {
    const required = FULLSCREEN_REQUIRED_SCREENS.includes(state.screen);
    fullscreenGate.classList.toggle("visible", required && !isFullscreenActive());
  }

  document.addEventListener("fullscreenchange", updateFullscreenGate);
  document.addEventListener("webkitfullscreenchange", updateFullscreenGate);

  // Separate listener purely for activity logging, so it fires only on
  // actual fullscreen transitions (not on every updateFullscreenGate()
  // call from renderChrome/goTo).
  function logFullscreenChange() {
    if (!state.sessionKey) return;
    StudyAPI.logEvent(
      state.sessionKey,
      isFullscreenActive() ? "fullscreen_entered" : "fullscreen_exited",
      { screenName: state.screen }
    );
  }
  document.addEventListener("fullscreenchange", logFullscreenChange);
  document.addEventListener("webkitfullscreenchange", logFullscreenChange);

  /* ---------------------------------------------------------------- */
  /* Generic UI-interaction logging (one row per event)                */
  /* ---------------------------------------------------------------- */

  function logUI(eventType, detail) {
    if (!state.sessionKey) return;
    StudyAPI.logEvent(state.sessionKey, eventType, {
      screenName: state.screen,
      detail: detail || {}
    });
  }

  document.addEventListener("visibilitychange", () =>
    logUI(document.hidden ? "tab_hidden" : "tab_visible"));
  window.addEventListener("blur", () => logUI("window_blur"));
  window.addEventListener("focus", () => logUI("window_focus"));
  document.addEventListener("contextmenu", () => logUI("context_menu"));
  ["copy", "paste", "cut"].forEach((action) =>
    document.addEventListener(action, () => logUI("clipboard", { action })));

  // Every click on a button/input/select/link (radio labels fire a click on
  // their input, so choices are covered too). Password values are never sent.
  document.addEventListener("click", (e) => {
    const el = e.target.closest("button, input, select, a");
    if (!el) return;
    logUI("click", {
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      name: el.name || "",
      value: el.type === "password" ? "" : (el.value || ""),
      text: (el.innerText || "").trim().slice(0, 60)
    });
  }, true);

  // Text/numeric answers: one event ~800 ms after the last keystroke, with
  // the last keystroke's own time in detail.lastKeystrokeAt.
  let textLogTimer = null;
  function logTextInput(q, ctx, value) {
    clearTimeout(textLogTimer);
    const lastKeystrokeAt = StudyAPI.nowIso();
    textLogTimer = setTimeout(() => {
      StudyAPI.logEvent(state.sessionKey, "text_input", {
        screenName: "question",
        detail: {
          moduleId: ctx.module.id, sectionId: ctx.section.id,
          questionId: q.id, value, lastKeystrokeAt
        }
      });
    }, 800);
  }

  /* ---------------------------------------------------------------- */
  /* Back-navigation trap                                              */
  /* ---------------------------------------------------------------- */

  let backTrapActive = false;

  function trapPopState() {
    logUI("back_attempt");
    // Immediately re-push forward so the URL/history position never
    // actually moves backward while the trap is active.
    history.pushState({ studyTrap: true }, "", location.href);
  }

  function enableBackTrap() {
    if (backTrapActive) return;
    backTrapActive = true;
    history.pushState({ studyTrap: true }, "", location.href);
    window.addEventListener("popstate", trapPopState);
  }

  function disableBackTrap() {
    if (!backTrapActive) return;
    backTrapActive = false;
    window.removeEventListener("popstate", trapPopState);
  }

  /* ---------------------------------------------------------------- */
  /* Save-on-exit (End Study button + unexpected tab close)            */
  /* ---------------------------------------------------------------- */

  function isSessionActive() {
    return !state.ended && ["moduleIntro", "question", "rating", "saving"].includes(state.screen);
  }

  // Warn before an accidental tab close/refresh while a session is active.
  window.addEventListener("beforeunload", (e) => {
    if (!isSessionActive()) return;
    e.preventDefault();
    e.returnValue = "";
  });

  // Best-effort flush if the tab is actually closing. Browsers don't
  // guarantee async work finishes after pagehide/unload, but this is a
  // secondary safety net — MP4 segments are already uploaded incrementally
  // during recording (see onSegment in capture_session.js), so at most the
  // final in-progress segment is at risk, not the whole recording.
  window.addEventListener("pagehide", () => {
    logUI("page_hide");
    if (isSessionActive()) {
      CaptureSession.stop();
    }
  });

  /* ---------------------------------------------------------------- */
  /* Timer                                                             */
  /* ---------------------------------------------------------------- */

  function ensureTimerStarted() {
    if (!sessionStorage.getItem(TIMER_KEY)) {
      const end = Date.now() + SESSION_MINUTES * 60 * 1000;
      sessionStorage.setItem(TIMER_KEY, String(end));
    }
  }

  function tickTimer() {
    const raw = sessionStorage.getItem(TIMER_KEY);
    if (!raw) {
      // Timer hasn't started yet (still on login) — leave the static 30:00 as-is.
      return;
    }
    const end = Number(raw);
    const remainingMs = end - Date.now();
    if (remainingMs <= 0) {
      timerEl.textContent = "00:00";
      timerEl.classList.add("timer-warning");
      if (!state.ended && state.screen !== "login" && state.screen !== "end") {
        finishStudy("timeout");
      }
      return;
    }
    const totalSec = Math.floor(remainingMs / 1000);
    const min = String(Math.floor(totalSec / 60)).padStart(2, "0");
    const sec = String(totalSec % 60).padStart(2, "0");
    timerEl.textContent = `${min}:${sec}`;
    timerEl.classList.toggle("timer-warning", totalSec <= 60);
  }

  setInterval(tickTimer, 1000);

  /* ---------------------------------------------------------------- */
  /* Progress bar                                                      */
  /* ---------------------------------------------------------------- */

  function renderProgress() {
    const showBar = ["moduleIntro", "question", "rating"].includes(state.screen);
    progressWrap.style.display = showBar ? "block" : "none";
    if (!showBar) return;
    const p = StudyEngine.getProgress();
    progressWrap.innerHTML = `
      <div class="progress-track"><div class="progress-fill" style="width:${p.percent}%"></div></div>
      <div class="progress-label">Module ${p.moduleNumber} of ${p.moduleTotal} · ${p.moduleTitle} — Section ${p.sectionNumber} of ${p.sectionTotal} · ${p.sectionTitle}</div>
    `;
  }

  /* ---------------------------------------------------------------- */
  /* Topbar / end-study control visibility                             */
  /* ---------------------------------------------------------------- */

  function renderChrome() {
    const showEndBtn = ["moduleIntro", "question", "rating"].includes(state.screen);
    endStudyBtn.style.display = showEndBtn ? "inline-flex" : "none";
    renderProgress();
    updateFullscreenGate();
  }

  /* ---------------------------------------------------------------- */
  /* Screens                                                            */
  /* ---------------------------------------------------------------- */

     function renderConsent() {
    root.innerHTML = `
      <div class="card login-card">
        <p class="study-eyebrow">Step 2 of 2</p>
        <h1 class="study-title">Disclaimer and Consent</h1>
        <p class="study-lede">
          During this study, your webcam feed and screen activity may be recorded for research
          purposes while you complete the experiment tasks. Recorded clips are used only for
          study analysis.
        </p>
        <p class="study-lede">
          Participation is voluntary. You may stop at any point before beginning the experiment.
          By continuing below, you confirm that you understand the recording setup and consent
          to participate in the study.
        </p>
        <div class="field checkbox-field">
          <input type="checkbox" id="consent-checkbox" />
          <label for="consent-checkbox">I have read the above and consent to participate.</label>
        </div>
        <div class="field-error" id="consent-error">Please check the box to continue.</div>
        <button class="btn btn-primary btn-block" id="btn-consent-continue">Agree and Continue</button>
      </div>
    `;
    document.getElementById("btn-consent-continue").addEventListener("click", () => {
      const checked = document.getElementById("consent-checkbox").checked;
      const err = document.getElementById("consent-error");
      if (!checked) {
        err.classList.add("visible");
        return;
      }
      err.classList.remove("visible");
      state.consentGivenAt = new Date().toISOString();
      // Attaches consent_given_at to the StudySession created at login,
      // keyed by session_key -- see log_consent in views.py.
      StudyAPI.logConsent(state.sessionKey, state.consentGivenAt);
      // Recording (webcam/screen + RealEye gaze) starts only now that consent
      // has actually been given -- previously this fired at login, before the
      // participant had seen or agreed to the consent screen.
      CaptureSession.initRealEye();
      CaptureSession.start(state.sessionKey);
      goTo("instructions");
    });
  }

   function renderLogin() {
    root.innerHTML = `
      <div class="card login-card">
        <p class="study-eyebrow">Step 1 of 2</p>
        <h1 class="study-title">Participant Login</h1>
        <p class="study-lede">Please enter your assigned participant ID and the study password to begin.</p>
        <div class="field">
          <label for="participant-id">Participant Username / ID</label>
          <input type="text" id="participant-id" placeholder="e.g. 1" autocomplete="off" />
        </div>
        <div class="field">
          <label for="participant-password">Password</label>
          <input type="password" id="participant-password" placeholder="Enter password" />
          <div class="field-error" id="login-error">Please check your participant ID and password and try again.</div>
        </div>
        <button class="btn btn-primary btn-block" id="btn-login">Login &amp; Continue</button>
      </div>
    `;
    document.getElementById("btn-login").addEventListener("click", () => {
      const idRaw = document.getElementById("participant-id").value.trim();
      const id = idRaw.toUpperCase();
      const pw = document.getElementById("participant-password").value;
      const err = document.getElementById("login-error");
      const loginBtn = document.getElementById("btn-login");

      // Quick client-side check purely for instant UX feedback — the real
      // gate is the server-side check in StudyAPI.login() below.
      const validId = PARTICIPANT_IDS.includes(id);
      if (!idRaw || !pw || !validId) {
        err.classList.add("visible");
        return;
      }
      err.classList.remove("visible");

      // Must be called synchronously inside this click handler — browsers
      // only grant fullscreen in direct response to a user gesture. If it
      // fails or hasn't resolved yet by the time the next screen renders,
      // the fullscreen gate takes over and blocks progress until it's on.
      enterFullscreen();

      loginBtn.disabled = true;
      // consent_given_at isn't known yet -- consent comes next, and
      // StudyAPI.logConsent() will attach it to this session by session_key
      // once given.
      StudyAPI.login(id, pw, {})
        .then(({ session_key }) => {
          state.participantId = id;
          state.sessionKey = session_key;

          // Redundant safety net -- the timer actually starts at script
          // boot now (see bottom of file), not here. Left in case boot
          // ever runs before sessionStorage is writable for some reason.
          ensureTimerStarted();
          goTo("consent");
        })
        .catch((loginErr) => {
          console.error("[app] Login failed:", loginErr);
          loginBtn.disabled = false;
          err.classList.add("visible");
        });
    });
  }

  function renderInstructions() {
    root.innerHTML = `
      <div class="card instructions-card">
        <p class="study-eyebrow">Welcome, ${state.participantId}</p>
        <h1 class="study-title">Before you begin</h1>
        <p class="study-lede">
                    You'll work through several short healthcare survey modules (up to ${getMaxQuestionsEstimate()}
          questions in total — some are skipped automatically based on your earlier answers).
          After each question, you'll rate how much mental effort it took to answer. The study
          must be completed in fullscreen and takes about 30 minutes. Your webcam and screen are
          being recorded for this session, and once you begin you won't be able to go back to a
          previous question.
        </p>
        <p class="study-lede">
          You can end the study at any time using the <strong>End Study</strong> button at the top of
          the page — this will stop and save the recording before closing your session.
        </p>
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-start">Start Study</button>
        </div>
      </div>
    `;
      document.getElementById("btn-start").addEventListener("click", () => {
      // study_engine.js reads section/question order overrides straight off
      // window.STUDY_CONFIG, so swap the global to the variant this
      // participant's ID selects before initializing.
      window.STUDY_CONFIG = getActiveConfig();
            StudyEngine.init(getActiveModules(), window.STUDY_CONFIG.activeModuleIds, (qid) => state.answers[qid]);
      const hasQuestion = StudyEngine.start();
      if (!hasQuestion) {
        finishStudy("completed");
        return;
      }
      enableBackTrap();
      const ctx = StudyEngine.getContext();
      state.currentModuleId = ctx.module.id;
      goTo("moduleIntro");
    });
  }

  // Dedicated per-module title screen — shown once whenever the cursor
  // enters a new module, before that module's first section/question.
  function renderModuleIntro() {
    const ctx = StudyEngine.getContext();
    const mod = ctx.module;
    root.innerHTML = `
      <div class="card module-intro-card">
        <p class="study-eyebrow">Module ${ctx.moduleIndex + 1} of ${StudyEngine.getProgress().moduleTotal}</p>
        <h1 class="study-title module-intro-title">${mod.title}</h1>
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-module-continue">Continue</button>
        </div>
      </div>
    `;
    document.getElementById("btn-module-continue").addEventListener("click", () => {
      goTo("question");
    });
  }

  // Renders the stacked-card choice list shared by binary/nominal/categorical.
  function renderChoiceList(q) {
    const isBinary = q.type === "binary";
    const listClass = isBinary ? "choice-list choice-list-binary" : "choice-list";
    const optionsHtml = q.options
      .map(
        (opt) => `
        <label class="choice-option">
          <input type="radio" name="answer" value="${opt.value}" ${
          state.answers[q.id] === opt.value ? "checked" : ""
        } />
          <span class="option-text">${opt.label}</span>
        </label>`
      )
      .join("");
    return `<div class="${listClass}" id="choice-list">${optionsHtml}</div>`;
  }

  // Renders the ordered horizontal scale used for "ordinal" questions, so
  // the low->high order defined in the schema is visually obvious.
    // Renders the ordered horizontal scale used for "ordinal" questions, so
  // the low->high order defined in the schema is visually obvious.
  function renderOrdinalScale(q) {
    const optionsHtml = q.options
      .map(
        (opt) => `
        <label class="choice-option ordinal-option">
          <input type="radio" name="answer" value="${opt.value}" ${
          state.answers[q.id] === opt.value ? "checked" : ""
        } />
          <span class="option-text">${opt.label}</span>
        </label>`
      )
      .join("");
    return `<div class="choice-list choice-list-ordinal" id="choice-list">${optionsHtml}</div>`;
  }

  // "Select all that apply" checkbox list. Answer is stored as an array of
  // option values.
  function renderMultiList(q) {
    const selected = Array.isArray(state.answers[q.id]) ? state.answers[q.id] : [];
    const optionsHtml = q.options
      .map(
        (opt) => `
        <label class="choice-option choice-option-multi">
          <input type="checkbox" name="answer" value="${opt.value}" ${
          selected.includes(opt.value) ? "checked" : ""
        } />
          <span class="option-text">${opt.label}</span>
        </label>`
      )
      .join("");
    return `<div class="choice-list choice-list-multi" id="choice-list">${optionsHtml}</div>`;
  }

  // Single numeric input (e.g. age).
  function renderNumericInput(q) {
    const val = state.answers[q.id] || "";
    return `<div class="text-input-wrap"><input type="number" id="text-answer" inputmode="numeric" value="${val}" /></div>`;
  }

  // Single open-text input.
  function renderTextInput(q) {
    const val = state.answers[q.id] || "";
    return `<div class="text-input-wrap"><input type="text" id="text-answer" value="${val}" /></div>`;
  }

  // Single-select dropdown for a small fixed set of numeric/ordinal
  // values (e.g. hours of sleep) where a full choice-card list would be
  // too visually heavy. A dropdown can carry either a fixed q.options list
  // (the normal case), or, when the upper bound is per-participant rather
  // than fixed in the schema (e.g. the Concussion/TBI "how old were you"
  // follow-ups, capped at whatever age the participant entered in
  // Demographics), q.minValue + q.maxRef instead — see
  // dynamicRangeOptions() below.
  function renderDropdown(q) {
    const val = state.answers[q.id] || "";
    const options = q.options && q.options.length > 0 ? q.options : dynamicRangeOptions(q);
    const optionsHtml = options
      .map(
        (opt) => `<option value="${opt.value}" ${opt.value === val ? "selected" : ""}>${opt.label}</option>`
      )
      .join("");
    return `<div class="text-input-wrap"><select id="text-answer"><option value="" disabled ${
      val ? "" : "selected"
    }>Select…</option>${optionsHtml}</select></div>`;
  }

  // Builds a q.minValue..N option list for a dropdown whose upper bound
  // isn't known until the participant has answered an earlier question
  // (q.maxRef names that question's id -- always Demographics Age today,
  // but not hardcoded to it). Falls back to an empty list (dropdown shows
  // only the "Select…" placeholder, Next stays disabled via the empty-
  // value guard already in bindInputs) if that answer isn't a valid
  // number yet -- shouldn't normally happen since Demographics always
  // runs first, but fails safe rather than throwing or showing a bogus
  // range.
  function dynamicRangeOptions(q) {
    if (q.minValue === undefined || !q.maxRef) return [];
    const max = parseInt(state.answers[q.maxRef], 10);
    if (!Number.isFinite(max) || max < q.minValue) return [];
    const options = [];
    for (let n = q.minValue; n <= max; n++) {
      options.push({ value: String(n), label: String(n) });
    }
    return options;
  }

  // A shared response scale (q.options) rated once per sub-item (q.items).
  // Answer is stored as an object keyed by item id.
   // A shared response scale (q.options) rated once per sub-item (q.items),
  // rendered as a real 2D table (rows = items, columns = the shared scale)
  // rather than a stacked list -- standard survey grid layout, and avoids
  // the wrapping/misalignment a flex-based row layout risks on longer
  // option labels. Answer is stored as an object keyed by item id. input
  // name/value attributes are unchanged, so the existing event-binding
  // code in renderQuestion() below needs no changes.
  function renderMatrix(q, items) {
    const current = state.answers[q.id] && typeof state.answers[q.id] === "object" ? state.answers[q.id] : {};
    const headerCells = q.options.map((opt) => `<th class="matrix-col-label">${opt.label}</th>`).join("");
    const rows = (items || [])
      .map((item) => {
        const cells = q.options
          .map(
            (opt) => `
          <td class="matrix-cell">
            <input type="radio" name="matrix-${item.id}" value="${opt.value}" ${
              current[item.id] === opt.value ? "checked" : ""
            } />
          </td>`
          )
          .join("");
        return `<tr data-item-id="${item.id}">
          <th scope="row" class="matrix-row-label">${item.label}</th>
          ${cells}
        </tr>`;
      })
      .join("");
    return `
      <div class="matrix-wrap" id="matrix-grid">
        <table class="matrix-table">
          <thead><tr><th class="matrix-corner"></th>${headerCells}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  // Gaze-tracking calibration is tied to fixed on-screen coordinates, so
  // NOTHING on this page is allowed to scroll -- regardless of how many
  // matrix rows a question has. If a rendered card would overflow the
  // available viewport height, scale it down in small steps (rather than
  // truncating content or introducing a scrollbar) until it fits.
  function fitCardToViewport(cardSelector) {
    const card = document.querySelector(cardSelector);
    const container = document.querySelector(".container");
    if (!card || !container) return;
    card.style.transform = "";
    card.style.transformOrigin = "top center";
    let scale = 1;
    for (let i = 0; i < 20; i++) {
      if (card.scrollHeight <= container.clientHeight || scale <= 0.55) break;
      scale -= 0.05;
      card.style.transform = `scale(${scale})`;
    }
  }

  // How many matrix rows actually fit is content-dependent (row-label
  // length, number of scale columns, viewport size) -- rather than guess
  // a fixed row count, this measures the fully-rendered matrix (every
  // row) and only asks for a split if fitCardToViewport() would have to
  // scale it down aggressively to avoid a scrollbar. A ~6-row PSQI-style
  // grid keeps rendering as one page exactly as before; a 20-statement
  // Ojala 2012 Coping Responses grid gets split into as many same-sized
  // pages as it takes for each page to fit at (near) full scale.
  // Returns a row-per-page count, or null if no split is needed.
  function paginateMatrixIfNeeded() {
    const card = document.querySelector(".question-card");
    const container = document.querySelector(".container");
    const table = document.querySelector(".matrix-table");
    const tbody = table && table.querySelector("tbody");
    if (!card || !container || !tbody) return null;
    if (card.scrollHeight <= container.clientHeight) return null; // fits already

    const rowEls = Array.from(tbody.children);
    if (rowEls.length <= 1) return null; // nothing left to split

    const rowHeight = tbody.scrollHeight / rowEls.length;
    // Everything in the card that isn't table rows: stem, question-meta,
    // table header, Next button, card padding. Assumed constant across
    // pages of the same question (reasonable -- only the row count changes).
    const overhead = card.scrollHeight - tbody.scrollHeight;
    const budget = container.clientHeight * 0.95; // small safety margin
    const rowsPerPage = Math.max(1, Math.floor((budget - overhead) / rowHeight));

    return rowsPerPage < rowEls.length ? rowsPerPage : null;
  }

  // Called on every answer change, including a participant revising an
  // earlier selection (e.g. picking a different matrix row, or unchecking
  // one multi-select box and checking another). Updates the last-changed
  // timestamp used for response-time calculation, and -- for discrete
  // option-based inputs, not raw text keystrokes -- also logs a
  // question_answered activity event so the full revision history is on
  // the server, not just the final value.
  function recordAnswerChange(q, ctx, value, { logIt = true } = {}) {
    const now = StudyAPI.nowIso();
    state.answerLastChangedAt[q.id] = now;
    if (logIt && state.sessionKey) {
      StudyAPI.logEvent(state.sessionKey, "question_answered", {
        screenName: "question",
        detail: {
          moduleId: ctx.module.id,
          sectionId: ctx.section.id,
          questionId: q.id,
          value
        }
      });
    }
  }

  function renderQuestion() {
    const ctx = StudyEngine.getContext();
    const q = ctx.question;
    if (!q) {
      finishStudy("completed");
      return;
    }
    const presentedMs = Date.now();
    state.questionPresentedAtMs = presentedMs;
    state.questionPresentedAt = new Date(presentedMs).toISOString();
    const groupBadge = q.group ? `<span class="group-badge">Follow-up</span>` : "";

    // Matrix questions that don't fit on one screen even after
    // fitCardToViewport()'s scale-down (e.g. a 20-statement Ojala 2012
    // Coping Responses grid) get split into row-chunks the participant
    // pages through via "Next" before the underlying study question
    // actually advances. matrixPage stays null (i.e. "show every row")
    // until paginateMatrixIfNeeded() says otherwise; it's a local to
    // this call, so a genuinely new question always starts unpaginated.
    //
    // A template pipe-in matrix (q28-style) is a special case of this: it
    // ALWAYS splits, one screen per selected place regardless of whether
    // it'd fit on one screen, since flattening it into one big matrix is
    // the exact "messy, place name repeated in every row" layout this is
    // meant to avoid. pipeInGroups is null for anything else (an ordinary
    // matrix, or a simple pipe-in like q26), in which case matrixPage
    // falls through to the normal viewport-driven pagination below.
    const pipeInGroups = q.type === "matrix" ? resolvePipeInGroups(q) : null;
    let matrixPage = pipeInGroups && pipeInGroups.length > 0
      ? { index: 0, totalPages: pipeInGroups.length } // { start, size } unused/irrelevant in this mode
      : null; // { start, size, index, totalPages }

    function currentMatrixItems() {
      if (q.type !== "matrix") return null;
      if (pipeInGroups) {
        const group = pipeInGroups[matrixPage ? matrixPage.index : 0];
        return group ? group.rows : [];
      }
      const allItems = resolvePipeInItems(q);
      if (!matrixPage) return allItems;
      return allItems.slice(matrixPage.start, matrixPage.start + matrixPage.size);
    }

    // The stem to actually display -- for a template pipe-in, substitutes
    // the current screen's place name into q.pipeInItems.stemTemplate's
    // "{option}" placeholder ("...aspects of your therapy at {option}?"
    // -> "...at YourDost?"). Falls back to appending the place name after
    // q.stem if an older-generated schema doesn't have stemTemplate yet.
    function currentStem() {
      if (pipeInGroups) {
        const group = pipeInGroups[matrixPage ? matrixPage.index : 0];
        if (group) {
          return q.pipeInItems.stemTemplate
            ? q.pipeInItems.stemTemplate.replace("{option}", group.label)
            : `${q.stem} — ${group.label}`;
        }
      }
      return q.stem;
    }

    function renderCard() {
      let optionsMarkup;
      if (q.type === "ordinal") optionsMarkup = renderOrdinalScale(q);
      else if (q.type === "multi") optionsMarkup = renderMultiList(q);
      else if (q.type === "numeric") optionsMarkup = renderNumericInput(q);
      else if (q.type === "text") optionsMarkup = renderTextInput(q);
      else if (q.type === "dropdown") optionsMarkup = renderDropdown(q);
      else if (q.type === "matrix") optionsMarkup = renderMatrix(q, currentMatrixItems());
      else optionsMarkup = renderChoiceList(q); // binary / nominal / categorical

      const pageBadge =
        matrixPage && matrixPage.totalPages > 1
          ? `<span class="group-badge">Part ${matrixPage.index + 1} of ${matrixPage.totalPages}</span>`
          : "";

      root.innerHTML = `
        <div class="card question-card">
          <p class="question-meta">${ctx.section.title} ${groupBadge}${pageBadge}</p>
          <p class="question-stem">${currentStem()}</p>
          ${optionsMarkup}
          <div class="btn-row">
            <button class="btn btn-primary" id="btn-next" disabled>Next</button>
          </div>
        </div>
      `;
    }

    function bindInputs() {
      const nextBtn = document.getElementById("btn-next");

      if (q.type === "multi") {
        // Options flagged `exclusive: true` in the schema (e.g. "No,
        // never been diagnosed...", "Don't know", "None of the above")
        // can't coexist with any other selection in this question:
        // checking one clears every other checkbox, and checking
        // anything else clears any exclusive option that was previously
        // checked.
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
            recordAnswerChange(q, ctx, checked);
            nextBtn.disabled = checked.length === 0;
          });
        });
      } else if (q.type === "numeric" || q.type === "text") {
        const input = document.getElementById("text-answer");
        input.addEventListener("input", () => {
          state.answers[q.id] = input.value;
          recordAnswerChange(q, ctx, input.value, { logIt: false });
          logTextInput(q, ctx, input.value);
          nextBtn.disabled = input.value.trim() === "";
        });
      } else if (q.type === "dropdown") {
        const input = document.getElementById("text-answer");
        input.addEventListener("change", () => {
          state.answers[q.id] = input.value;
          recordAnswerChange(q, ctx, input.value);
          nextBtn.disabled = input.value === "";
        });
      } else if (q.type === "matrix") {
        const items = currentMatrixItems();
        // Seed from whatever's already been recorded for this question
        // (earlier matrix pages included) rather than starting blank --
        // starting blank meant the first change on a later page used to
        // overwrite state.answers[q.id] with just that one row, silently
        // dropping every row answered on an earlier page.
        const existing = state.answers[q.id] && typeof state.answers[q.id] === "object" ? state.answers[q.id] : {};
        const answerObj = { ...existing };
        const checkComplete = () => {
          nextBtn.disabled = items.some((item) => answerObj[item.id] === undefined);
        };
        items.forEach((item) => {
          document.querySelectorAll(`input[name="matrix-${item.id}"]`).forEach((input) => {
            input.addEventListener("change", (e) => {
              answerObj[item.id] = e.target.value;
              state.answers[q.id] = answerObj;
              recordAnswerChange(q, ctx, answerObj);
              checkComplete();
            });
          });
        });
        checkComplete(); // covers re-entering a page whose rows are all already answered
      } else {
        document.querySelectorAll('input[name="answer"]').forEach((input) => {
          input.addEventListener("change", (e) => {
            state.answers[q.id] = e.target.value;
            recordAnswerChange(q, ctx, e.target.value);
            nextBtn.disabled = false;
          });
        });
      }

      nextBtn.addEventListener("click", () => {
        if (nextBtn.disabled) return; // guard: no answer selected, can't advance
        if (matrixPage && matrixPage.index < matrixPage.totalPages - 1) {
          // More row-chunks left in this same question -- advance the
          // page and re-render, but don't touch the engine cursor or go
          // to the effort-rating screen yet; that only happens once the
          // last chunk's rows are answered too.
          logUI("matrix_page_next", { questionId: q.id, fromPage: matrixPage.index, toPage: matrixPage.index + 1 });
          matrixPage = { ...matrixPage, index: matrixPage.index + 1, start: matrixPage.start + matrixPage.size };
          renderCard();
          bindInputs();
          requestAnimationFrame(() => fitCardToViewport(".question-card"));
          return;
        }
        // The participant's final "Next" on this question: this is the
        // answer-submitted time. Stamped here, before the rating screen.
        state.answerSubmittedAtMs[q.id] = Date.now();
        goTo("rating");
      });
    }

    renderCard();

    requestAnimationFrame(() => {
      if (q.type === "matrix" && !matrixPage) {
        const rowsPerPage = paginateMatrixIfNeeded();
        if (rowsPerPage) {
          matrixPage = { start: 0, size: rowsPerPage, index: 0, totalPages: Math.ceil(resolvePipeInItems(q).length / rowsPerPage) };
          renderCard();
        }
      }
      bindInputs();
      fitCardToViewport(".question-card");
    });
  }

  // PAAS mental-effort rating: click-to-select 1–9, nothing pre-selected.
  // Clicking "5" is a first-class explicit answer, same as clicking any
  // other value — there's no slider to "not bother moving."
  function renderRating() {
    const ctx = StudyEngine.getContext();
    const q = ctx.question;

    const optionsHtml = EFFORT_SCALE.map(
      (opt) => `
        <label class="choice-option ordinal-option paas-option">
          <input type="radio" name="effort" value="${opt.value}" />
          <span class="option-text"><span class="paas-number">${opt.value}</span></span>
          <span class="paas-label">${opt.label}</span>
        </label>`
    ).join("");

    root.innerHTML = `
      <div class="card rating-card">
        <p class="question-meta">${ctx.section.title} — Mental effort</p>
        <p class="question-stem">How much mental effort did you put in for the previous question?</p>
        <div class="paas-scale">
          <div class="choice-list choice-list-ordinal paas-option-list">${optionsHtml}</div>
        </div>
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-rating-next" disabled>Next</button>
        </div>
      </div>
    `;

    const nextBtn = document.getElementById("btn-rating-next");
    document.querySelectorAll('input[name="effort"]').forEach((input) => {
      input.addEventListener("change", () => {
        logUI("rating_selected", { questionId: q.id, value: input.value });
        nextBtn.disabled = false;
      });
    });

    nextBtn.addEventListener("click", () => {
      const selected = document.querySelector('input[name="effort"]:checked');
      if (!selected) return; // guard: shouldn't fire since button starts disabled
      state.effort[q.id] = Number(selected.value);

      // Persist the full answer + effort rating for this question, with
      // both the presented-at and answered-at timestamps, to Django.
            const rawAnswer = state.answers[q.id];
      const serializedAnswer = typeof rawAnswer === "string" ? rawAnswer : JSON.stringify(rawAnswer);

      const answeredMs = state.answerSubmittedAtMs[q.id] || state.questionPresentedAtMs;

      StudyAPI.submitResponse(state.sessionKey, {
        moduleId: ctx.module.id,
        sectionId: ctx.section.id,
        questionId: q.id,
        answerValue: serializedAnswer,
        effortRating: state.effort[q.id],
        presentedAt: state.questionPresentedAt,
        // The moment "Next" was clicked on the question screen -- not the
        // last answer change, and not anything on this rating screen.
        answeredAt: new Date(answeredMs).toISOString(),
        // Exact integer epoch ms, captured at the moment of the events:
        presentedEpochMs: state.questionPresentedAtMs,
        answeredEpochMs: answeredMs
      }).catch((err) => console.error("[app] Failed to submit response:", err));

      const hasNext = StudyEngine.next();
      if (!hasNext) {
        finishStudy("completed");
        return;
      }
      const nextCtx = StudyEngine.getContext();
      if (nextCtx.module.id !== state.currentModuleId) {
        state.currentModuleId = nextCtx.module.id;
        goTo("moduleIntro");
      } else {
        goTo("question");
      }
    });
  }

  function renderSaving() {
    root.innerHTML = `
      <div class="card saving-card">
        <div class="saving-spinner" aria-hidden="true"></div>
        <h1 class="study-title">Saving your recording…</h1>
        <p class="study-lede">Please don't close this window — this will only take a moment.</p>
      </div>
    `;
  }

  function renderEnd() {
    const early = state.endReason === "manual" || state.endReason === "timeout";
    const heading = early ? "Study Ended" : "Thank you!";
    const icon = early ? "⏹️" : "✅";
    const message =
      state.endReason === "manual"
        ? "Your recording has been saved.Thank you for your time!"
        : state.endReason === "timeout"
        ? "Your recording has been saved.Thank you for your time!"
        : "Your recording has been saved.Thank you for your time!";

    root.innerHTML = `
      <div class="card end-card">
        <div class="end-icon">${icon}</div>
        <h1 class="study-title">${heading}</h1>
        <p class="study-lede">${message}</p>
      </div>
    `;
  }

  /* ---------------------------------------------------------------- */
  /* Navigation / finish                                               */
  /* ---------------------------------------------------------------- */

  function goTo(screen) {
    state.screen = screen;
    renderChrome();
    if (screen === "consent") renderConsent();
    else if (screen === "login") renderLogin();
    else if (screen === "instructions") renderInstructions();
    else if (screen === "moduleIntro") renderModuleIntro();
    else if (screen === "question") renderQuestion();
    else if (screen === "rating") renderRating();
    else if (screen === "saving") renderSaving();
    else if (screen === "end") renderEnd();

    // Every screen appearance is logged with its own timestamp, once a
    // session exists (nothing to log yet while still on the login screen).
    if (state.sessionKey) {
      const ctx = ["moduleIntro", "question", "rating"].includes(screen)
        ? StudyEngine.getContext()
        : null;
      StudyAPI.logEvent(state.sessionKey, "screen_shown", {
        screenName: screen,
        detail: ctx
          ? { moduleId: ctx.module && ctx.module.id, sectionId: ctx.section && ctx.section.id, questionId: ctx.question && ctx.question.id }
          : {}
      });
    }
  }

  async function finishStudy(reason) {
    if (state.ended) return;
    state.ended = true;
    state.endReason = reason;
    // Stop the timer the instant End Study is confirmed (or timeout fires),
    // not after the recording finishes saving -- CaptureSession.stop() and
    // StudyAPI.finishSession() below can take a few seconds, and the timer
    // must not keep counting through that.
    sessionStorage.removeItem(TIMER_KEY);
    disableBackTrap();
    goTo("saving"); // show a holding screen while the recording flushes/uploads
    await CaptureSession.stop();
    if (state.sessionKey) {
      try {
        await StudyAPI.finishSession(state.sessionKey, reason);
      } catch (err) {
        console.error("[app] Failed to notify server of session end:", err);
      }
    }
    exitFullscreen();
    goTo("end");
  }

  /* ---------------------------------------------------------------- */
  /* End-study modal ("give up" button)                                 */
  /* ---------------------------------------------------------------- */

  endStudyBtn.addEventListener("click", () => {
    logUI("end_study_opened");
    modalBackdrop.classList.add("visible");
  });

  document.getElementById("modal-cancel").addEventListener("click", () => {
    logUI("end_study_cancelled");
    modalBackdrop.classList.remove("visible");
  });

  document.getElementById("modal-confirm-end").addEventListener("click", () => {
    logUI("end_study_confirmed");
    modalBackdrop.classList.remove("visible");
    finishStudy("manual");
  });

  /* ---------------------------------------------------------------- */
  /* Boot                                                               */
  /* ---------------------------------------------------------------- */

  // Timer starts the moment this script runs (i.e. as soon as the page
  // appears), not when login succeeds -- ensureTimerStarted() is a no-op
  // if a timer is already running in this sessionStorage, so this is safe
  // to also be a no-op-safe call from renderLogin() below.
  ensureTimerStarted();
  goTo("login");
})();