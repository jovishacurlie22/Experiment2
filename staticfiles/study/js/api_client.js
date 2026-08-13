/* ==========================================================================
   api_client.js — thin wrapper around fetch() for talking to the Django
   backend (study/views.py). Handles CSRF token attachment (Django's
   CsrfViewMiddleware requires the csrftoken cookie value echoed back as an
   X-CSRFToken header on every unsafe request) and gives app.js /
   capture_session.js small named functions instead of repeating fetch
   boilerplate everywhere.

   All functions are best-effort / non-throwing from the caller's point of
   view for logging calls (log_event, server-hit-style calls) — a dropped
   log shouldn't break the study for the participant. login/submitResponse/
   finishSession reject on failure so callers can react (e.g. show the
   login error state).
   ========================================================================== */

window.StudyAPI = (() => {
  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[2]) : null;
  }

  function csrfHeaders(extra) {
    return Object.assign({ "X-CSRFToken": getCookie("csrftoken") }, extra || {});
  }

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body)
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${url} failed with status ${res.status}: ${text}`);
    }
    return res.json();
  }

  async function postForm(url, formData) {
    const res = await fetch(url, {
      method: "POST",
      headers: csrfHeaders(), // no Content-Type: browser sets multipart boundary
      body: formData
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${url} failed with status ${res.status}: ${text}`);
    }
    return res.json();
  }

  function nowIso() {
    return new Date().toISOString();
  }

  return {
    getCookie,
    csrfHeaders,
    nowIso,

    // POST /api/login/  -> { session_key }
    login(participantId, password) {
      return postJSON("/api/login/", {
        participant_id: participantId,
        password,
        client_timestamp: nowIso()
      });
    },

    // POST /api/log-event/ — fire-and-forget; logs to console on failure
    // rather than throwing, so a flaky log call never blocks the study.
    logEvent(sessionKey, eventType, { screenName, detail, sourcePath } = {}) {
      return postJSON("/api/log-event/", {
        session_key: sessionKey,
        event_type: eventType,
        screen_name: screenName || "",
        detail: detail || {},
        source_path: sourcePath || location.pathname,
        client_timestamp: nowIso()
      }).catch((err) => console.warn("[StudyAPI] logEvent failed:", err));
    },

    // POST /api/submit-response/  -> { ok, response_id }
    submitResponse(sessionKey, { moduleId, sectionId, questionId, answerValue, effortRating, presentedAt, answeredAt }) {
      return postJSON("/api/submit-response/", {
        session_key: sessionKey,
        module_id: moduleId,
        section_id: sectionId,
        question_id: questionId,
        answer_value: answerValue,
        effort_rating: effortRating,
        presented_at: presentedAt,
        answered_at: answeredAt
      });
    },

    // POST /api/upload-chunk/ (multipart) -> { ok, chunk_id }
    uploadChunk(formData) {
      return postForm("/api/upload-chunk/", formData);
    },

    // POST /api/finish-session/  -> { ok }
    finishSession(sessionKey, endReason) {
      return postJSON("/api/finish-session/", {
        session_key: sessionKey,
        end_reason: endReason,
        client_timestamp: nowIso()
      });
    }
  };
})();
