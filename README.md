# Study 2 — Healthcare Survey (Eye Gaze Study)

A working static preview of Experiment 2, built to match Experiment 1's
visual style (design tokens, layout shell, component patterns) as described
in your style guide.

## Try it
Open `index.html` directly in a browser — the whole flow works client-side
with no backend:

Login → Instructions → (Question → Paas mental-effort rating) × 12 → End

## What's real vs. stubbed

**Real / working:**
- Login screen, 30 participant IDs (P01–P30)
- 30-minute session timer, sessionStorage-anchored (survives in-tab nav, resets on new tab) — same convention as Experiment 1
- Progress bar (question N of total)
- MCQ and binary question types, rendered as the same hidden-radio +
  styled-label "choice card" pattern as Experiment 1
- Paas 9-point mental-effort rating after every question, built with the
  same styled-range-slider + tick-labels pattern your guide documents
- **End Study** button on every screen except login/end — confirms, then
  stops recording and ends the session immediately
- Auto-end when the 30-minute timer expires
- Same color tokens, background gradient, fonts, card/button styles as
  Experiment 1

**Stubbed — needs your real implementation:**
- `js/capture_session.js` — I don't have your actual webcam-recording /
  chunk-upload code, so this is a stand-in with the same function shape
  (`startWebcamRecording`, `stopWebcamRecording`, serialized `uploadQueue`).
  Port your real logic in here; `app.js` already calls these functions at
  the right moments (login → start, end/timeout/complete → stop).
- RealEye SDK config in `index.html` `<head>` — `stimulusId` and
  `forceRun` are placeholders. Set `forceRun` to whatever value your
  RealEye project uses for screen-recording mode, and pull `stimulusId`
  from your Django context like Experiment 1 does.
- No backend calls anywhere — answers and Paas ratings are kept in memory
  only (`state.answers`, `state.effort` in `app.js`). You'll want to POST
  these to your Django views, most likely on each "Next" click and on
  `finishStudy()`.

## Swapping in real questions
Edit `js/questions.js` only — nothing else needs to change. Each entry:

```js
{
  id: "q01",
  type: "binary" | "mcq",
  stem: "Question text",
  options: [{ value: "yes", label: "Yes" }, { value: "no", label: "No" }]
}
```

## Porting into your Django app
This maps directly onto your existing `base.html` conventions:
- `index.html`'s `<head>`/`<body>` → merge into `base.html`, keep the
  `data-record-webcam` / `data-reset-capture-session` attributes driven by
  template variables like Experiment 1
- `css/styles.css` → append to (or replace) `styles.css`, tokens are
  identical so it won't clash
- The single-page screen-switching in `app.js` can either stay as-is (one
  Django view + template) or be split into separate Django views/templates
  per screen — the timer/progress-bar conventions work either way since
  they're sessionStorage-anchored, not page-state-anchored
