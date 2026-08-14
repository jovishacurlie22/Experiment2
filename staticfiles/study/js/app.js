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

  const ALL_MODULES = window.STUDY_MODULES || [];
  const ACTIVE_MODULE_IDS = (window.STUDY_CONFIG && window.STUDY_CONFIG.activeModuleIds) || [];
  const MAX_QUESTIONS_ESTIMATE = StudyEngine.estimateMaxQuestions(ALL_MODULES, ACTIVE_MODULE_IDS);
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
    screen: "login",
    participantId: null,
    sessionKey: null, // set on successful server-side login (StudySession.session_key)
    answers: {},   // qid -> option value
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
  /* Back-navigation trap                                              */
  /* ---------------------------------------------------------------- */

  let backTrapActive = false;

  function trapPopState() {
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

  function renderLogin() {
    root.innerHTML = `
      <div class="card login-card">
        <p class="study-eyebrow">Eye Gaze Study</p>
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
      StudyAPI.login(id, pw)
        .then(({ session_key }) => {
          state.participantId = id;
          state.sessionKey = session_key;

          ensureTimerStarted();
          CaptureSession.start(session_key);
          goTo("instructions");
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
          You'll work through several short healthcare survey modules (up to ${MAX_QUESTIONS_ESTIMATE}
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
      StudyEngine.init(ALL_MODULES, ACTIVE_MODULE_IDS, (qid) => state.answers[qid]);
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

  function renderQuestion() {
    const ctx = StudyEngine.getContext();
    const q = ctx.question;
    if (!q) {
      finishStudy("completed");
      return;
    }
    state.questionPresentedAt = StudyAPI.nowIso();
    const groupBadge = q.group ? `<span class="group-badge">Follow-up</span>` : "";
    const optionsMarkup = q.type === "ordinal" ? renderOrdinalScale(q) : renderChoiceList(q);

    root.innerHTML = `
      <div class="card question-card">
        <p class="question-meta">${ctx.section.title} ${groupBadge}</p>
        <p class="question-stem">${q.stem}</p>
        ${optionsMarkup}
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-next" disabled>Next</button>
        </div>
      </div>
    `;

    const nextBtn = document.getElementById("btn-next");
    document.querySelectorAll('input[name="answer"]').forEach((input) => {
      input.addEventListener("change", (e) => {
        state.answers[q.id] = e.target.value;
        nextBtn.disabled = false;
      });
    });
    nextBtn.addEventListener("click", () => {
      if (nextBtn.disabled) return; // guard: no answer selected, can't advance
      goTo("rating");
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
        <p class="question-stem">Indicate the amount of mental effort you spent in the previous question.</p>
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
        nextBtn.disabled = false;
      });
    });

    nextBtn.addEventListener("click", () => {
      const selected = document.querySelector('input[name="effort"]:checked');
      if (!selected) return; // guard: shouldn't fire since button starts disabled
      state.effort[q.id] = Number(selected.value);

      // Persist the full answer + effort rating for this question, with
      // both the presented-at and answered-at timestamps, to Django.
      StudyAPI.submitResponse(state.sessionKey, {
        moduleId: ctx.module.id,
        sectionId: ctx.section.id,
        questionId: q.id,
        answerValue: state.answers[q.id],
        effortRating: state.effort[q.id],
        presentedAt: state.questionPresentedAt,
        answeredAt: StudyAPI.nowIso()
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
        ? "The study was ended early by the participant. Your recording has been saved."
        : state.endReason === "timeout"
        ? "Time is up — the session has ended automatically. Your recording has been saved."
        : "You've completed all the modules. Your recording has been saved. Thank you for participating!";

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
    if (screen === "login") renderLogin();
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
    sessionStorage.removeItem(TIMER_KEY);
    exitFullscreen();
    goTo("end");
  }

  /* ---------------------------------------------------------------- */
  /* End-study modal ("give up" button)                                 */
  /* ---------------------------------------------------------------- */

  endStudyBtn.addEventListener("click", () => {
    modalBackdrop.classList.add("visible");
  });

  document.getElementById("modal-cancel").addEventListener("click", () => {
    modalBackdrop.classList.remove("visible");
  });

  document.getElementById("modal-confirm-end").addEventListener("click", () => {
    modalBackdrop.classList.remove("visible");
    finishStudy("manual");
  });

  /* ---------------------------------------------------------------- */
  /* Boot                                                               */
  /* ---------------------------------------------------------------- */

  goTo("login");
})();