/* ==========================================================================
   capture_session.js  (STUB)
   ----------------------------------------------------------------------
   This file intentionally mirrors the shape described for Experiment 1's
   capture_session.js (serialized uploadQueue promise chain, data-attribute
   driven config) but does NOT contain your real recording/upload logic —
   that file wasn't shared with me. Port your actual implementation in here;
   the function names below are what app.js calls, so as long as you keep
   these signatures the rest of the site keeps working unchanged.

   Config is read the same way as Experiment 1: from data-* attributes on
   <body>, set server-side (or here, hardcoded for this static preview).
   ========================================================================== */

const CaptureSession = (() => {
  let webcamStream = null;
  let recording = false;

  // Serialized upload chain — keeps chunk uploads landing in order.
  // TODO: port your real implementation (fetch to your Django endpoint).
  let uploadQueue = Promise.resolve();

  function queueUpload(chunkMeta) {
    uploadQueue = uploadQueue.then(() =>
      // TODO: replace with real upload, e.g.
      // fetch('/api/upload-chunk/', { method: 'POST', body: chunkMeta })
      Promise.resolve().then(() => {
        console.log("[capture_session] (stub) would upload chunk:", chunkMeta);
      })
    );
    return uploadQueue;
  }

  async function startWebcamRecording() {
    const body = document.body;
    const shouldRecord = body.dataset.recordWebcam === "true";
    if (!shouldRecord) {
      console.log("[capture_session] webcam recording disabled for this session");
      return;
    }
    try {
      // TODO: your real implementation likely also captures screen share
      // via getDisplayMedia() when in RealEye "screen recording mode".
      webcamStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      recording = true;
      console.log("[capture_session] (stub) webcam recording started");
    } catch (err) {
      console.warn("[capture_session] could not start webcam recording:", err);
    }
  }

  function stopWebcamRecording() {
    if (webcamStream) {
      webcamStream.getTracks().forEach((t) => t.stop());
      webcamStream = null;
    }
    recording = false;
    console.log("[capture_session] (stub) webcam recording stopped");
  }

  function initRealEye() {
    // RealEye SDK is loaded globally in <head> as window.RealEye / window.reSdk,
    // same pattern as Experiment 1. Screen-recording mode + stimulus id should
    // be supplied by your backend the same way it is today.
    if (window.RealEye) {
      console.log("[capture_session] RealEye SDK detected — wire real calls here (e.g. window.RealEye.start()).");
    } else {
      console.log("[capture_session] RealEye SDK not loaded in this static preview (expected).");
    }
  }

  function isRecording() {
    return recording;
  }

  return {
    startWebcamRecording,
    stopWebcamRecording,
    initRealEye,
    queueUpload,
    isRecording
  };
})();

window.CaptureSession = CaptureSession;
