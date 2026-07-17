/* ==========================================================================
   Study 2 — Healthcare Survey app logic
   ----------------------------------------------------------------------
   Static single-page preview. Screens are swapped inside #app-root.
   Session timer is anchored via sessionStorage per Experiment 1 convention
   (absolute end timestamp, survives in-tab navigation, resets on new tab).
   ========================================================================== */

(() => {
  const SESSION_MINUTES = 30;
  const TIMER_KEY = "study2_session_end_ts";
  const PARTICIPANT_IDS = Array.from({ length: 30 }, (_, i) => String(i + 1));
  // TODO: replace with your real auth check (this is a client-side stand-in
  // only — for production, verify against Django auth server-side instead).
  const STUDY_PASSWORD = "gaze2024";

  const questions = window.STUDY_QUESTIONS || [];
  const state = {
    screen: "login",
    participantId: null,
    currentIndex: 0,
    answers: {},   // qid -> option value
    effort: {},    // qid -> paas rating 1-9
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
  /* Timer                                                             */
  /* ---------------------------------------------------------------- */

  function ensureTimerStarted() {
    if (!sessionStorage.getItem(TIMER_KEY)) {
      const end = Date.now() + SESSION_MINUTES * 60 * 1000;
      sessionStorage.setItem(TIMER_KEY, String(end));
    }
  }

  function tickTimer() {
    const end = Number(sessionStorage.getItem(TIMER_KEY) || 0);
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
    const total = questions.length;
    const completed = Math.min(state.currentIndex, total);
    const percent = total ? Math.round((completed / total) * 100) : 0;
    const showBar = ["question", "rating"].includes(state.screen);
    progressWrap.style.display = showBar ? "block" : "none";
    if (!showBar) return;
    progressWrap.innerHTML = `
      <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
      <div class="progress-label">${completed} / ${total} questions</div>
    `;
  }

  /* ---------------------------------------------------------------- */
  /* Topbar / end-study control visibility                             */
  /* ---------------------------------------------------------------- */

  function renderChrome() {
    const showEndBtn = !["login", "end"].includes(state.screen);
    endStudyBtn.style.display = showEndBtn ? "inline-flex" : "none";
    renderProgress();
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

      const validId = PARTICIPANT_IDS.includes(id);
      const validPw = pw === STUDY_PASSWORD;

      if (!idRaw || !pw || !validId || !validPw) {
        err.classList.add("visible");
        return;
      }
      err.classList.remove("visible");
      state.participantId = id;
      ensureTimerStarted();
      CaptureSession.initRealEye();
      CaptureSession.startWebcamRecording();
      goTo("instructions");
    });
  }

  function renderInstructions() {
    root.innerHTML = `
      <div class="card instructions-card">
        <p class="study-eyebrow">Welcome, ${state.participantId}</p>
        <h1 class="study-title">Before you begin</h1>
        <p class="study-lede">
          You'll be asked ${questions.length} short healthcare survey questions. After each question,
          you'll rate how much mental effort it took to answer. The study takes about 30 minutes.
          Your webcam and screen are being recorded for this session.
        </p>
        <p class="study-lede">
          You can end the study at any time using the <strong>End Study</strong> button at the top of
          the page — this will stop the recording and close the session immediately.
        </p>
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-start">Start Study</button>
        </div>
      </div>
    `;
    document.getElementById("btn-start").addEventListener("click", () => {
      state.currentIndex = 0;
      goTo("question");
    });
  }

  function renderQuestion() {
    const q = questions[state.currentIndex];
    if (!q) {
      finishStudy("completed");
      return;
    }
    const isBinary = q.type === "binary";
    const listClass = isBinary ? "choice-list choice-list-binary" : "choice-list";
    const optionsHtml = q.options
      .map(
        (opt, i) => `
        <label class="choice-option">
          <input type="radio" name="answer" value="${opt.value}" ${
          state.answers[q.id] === opt.value ? "checked" : ""
        } />
          <span class="option-text">${opt.label}</span>
        </label>`
      )
      .join("");

    root.innerHTML = `
      <div class="card question-card">
        <p class="question-meta">Question ${state.currentIndex + 1} of ${questions.length}</p>
        <p class="question-stem">${q.stem}</p>
        <div class="${listClass}" id="choice-list">${optionsHtml}</div>
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-next" ${
            state.answers[q.id] ? "" : "disabled"
          }>Next</button>
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
      goTo("rating");
    });
  }

  function renderRating() {
    const q = questions[state.currentIndex];
    const current = state.effort[q.id] || 5;

    root.innerHTML = `
      <div class="card rating-card">
        <p class="question-meta">Question ${state.currentIndex + 1} of ${questions.length} — Mental effort</p>
        <p class="question-stem">How much mental effort did it take you to answer that question?</p>
        <div class="paas-scale">
          <div class="paas-endpoints">
            <span>Very, very low effort</span>
            <span>Very, very high effort</span>
          </div>
          <input type="range" min="1" max="9" step="1" value="${current}" class="effort-slider" id="effort-slider" />
          <div class="effort-tick-labels">
            ${[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => `<span>${n}</span>`).join("")}
          </div>
          <div class="effort-value" id="effort-value">${current}</div>
        </div>
        <div class="btn-row">
          <button class="btn btn-primary" id="btn-rating-next">
            ${state.currentIndex + 1 < questions.length ? "Next Question" : "Finish Study"}
          </button>
        </div>
      </div>
    `;

    const slider = document.getElementById("effort-slider");
    const valueEl = document.getElementById("effort-value");
    slider.addEventListener("input", () => {
      valueEl.textContent = slider.value;
    });

    document.getElementById("btn-rating-next").addEventListener("click", () => {
      state.effort[q.id] = Number(slider.value);
      state.currentIndex += 1;
      if (state.currentIndex >= questions.length) {
        finishStudy("completed");
      } else {
        goTo("question");
      }
    });
  }

  function renderEnd() {
    const early = state.endReason === "manual" || state.endReason === "timeout";
    const heading = early ? "Study Ended" : "Thank you!";
    const icon = early ? "⏹️" : "✅";
    const message =
      state.endReason === "manual"
        ? "The study was ended early by the participant. Recording has stopped."
        : state.endReason === "timeout"
        ? "Time is up — the session has ended automatically. Recording has stopped."
        : "You've completed all the questions. Recording has stopped. Thank you for participating!";

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
    else if (screen === "question") renderQuestion();
    else if (screen === "rating") renderRating();
    else if (screen === "end") renderEnd();
  }

  function finishStudy(reason) {
    if (state.ended) return;
    state.ended = true;
    state.endReason = reason;
    CaptureSession.stopWebcamRecording();
    sessionStorage.removeItem(TIMER_KEY);
    goTo("end");
  }

  /* ---------------------------------------------------------------- */
  /* End-study modal                                                   */
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