/* ==========================================================================
   capture_session.js (MediaRecorder implementation)
   ========================================================================== */
 
const CaptureSession = (() => {
  let currentSessionKey = null;
 
  let webcamStream = null;
  let screenStream = null;
 
  let webcamRecorder = null;
  let screenRecorder = null;
 
  let recording = false;
  let chunkSequence = { webcam: 0, screen: 0 };
 
  let webcamUploadQueue = Promise.resolve();
  let screenUploadQueue = Promise.resolve();
 
  const CHUNK_TIMESLICE_MS = 2000;
 
  /* ---------------------------------------------------------------- */
  /* Activity/epoch logging                                            */
  /* ---------------------------------------------------------------- */
 
  // Single choke point for all timestamped events. epoch_ms is captured
  // with Date.now() at the exact call site (not inside StudyAPI, to avoid
  // any queueing/network delay skewing the timestamp), then sent via
  // sendBeacon so it survives page unload during module transitions.
  function logActivityEvent(eventType, streamSource = null, meta = {}) {
    const epochMs = Date.now();
    const payload = {
      session_key: currentSessionKey || '',
      event_type: eventType,
      epoch_ms: epochMs,
      stream_source: streamSource,
      meta: meta,
    };
    console.log(`[capture_session] ${eventType}${streamSource ? ' (' + streamSource + ')' : ''} @ ${epochMs}`);
    navigator.sendBeacon(
      '/log-activity-event/',
      new Blob([JSON.stringify(payload)], { type: 'application/json' })
    );
    return epochMs;
  }
 
  /* ---------------------------------------------------------------- */
  /* Test-mode state — local buffering                                  */
  /* ---------------------------------------------------------------- */
 
  function testModeEnabled() {
    return document.body.dataset.captureTestMode === "true";
  }
 
  let localBuffers = { webcam: [], screen: [] };
  let localChunkCounts = { webcam: 0, screen: 0 };
 
  function resetTestModeState() {
    localBuffers = { webcam: [], screen: [] };
    localChunkCounts = { webcam: 0, screen: 0 };
  }
 
  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
 
  function finalizeTestModeOutput() {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
 
    ["webcam", "screen"].forEach((trackType) => {
      const parts = localBuffers[trackType];
      if (parts.length === 0) return;
      const blob = new Blob(parts, { type: "video/webm" });
      triggerDownload(blob, `${trackType}-${stamp}.webm`);
    });
 
    console.log(
      "[capture_session][TEST MODE] Recording pipeline report:\n" +
        `  webcam: ${localChunkCounts.webcam} chunks\n` +
        `  screen: ${localChunkCounts.screen} chunks`
    );
  }
 
  /* ---------------------------------------------------------------- */
  /* Upload plumbing                                                    */
  /* ---------------------------------------------------------------- */
 
  function queueUpload(queueRef, formData, streamLabel) {
    const nextQueue = queueRef.then(() =>
      StudyAPI.uploadChunk(formData)
        .then(() => {
          console.log(
            `[capture_session] Uploaded ${streamLabel} chunk sequence: ${formData.get('sequence')}`
          );
        })
        .catch((err) => {
          console.error(`[capture_session] Error pushing ${streamLabel} chunk to Django backend:`, err);
        })
    );
    return nextQueue;
  }
 
  function handleChunk(trackType, blob, isLast) {
    if (!blob || blob.size === 0) return;
 
    chunkSequence[trackType]++;
 
    if (testModeEnabled()) {
      localBuffers[trackType].push(blob);
      localChunkCounts[trackType]++;
      return;
    }
 
    const formData = new FormData();
    formData.append('video_chunk', blob, `${trackType}-${chunkSequence[trackType]}.webm`);
    formData.append('stream_source', trackType);
    formData.append('sequence', chunkSequence[trackType]);
    formData.append('is_last', isLast ? 'true' : 'false');
    formData.append('session_key', currentSessionKey || '');
    // epoch_ms of when THIS CHUNK was handed to us — lets you reconstruct
    // approximate per-chunk wall-clock coverage independent of the
    // recording_start anchor, as a cross-check.
    formData.append('chunk_epoch_ms', Date.now());
 
    const targetQueue = trackType === 'webcam' ? webcamUploadQueue : screenUploadQueue;
    const updated = queueUpload(targetQueue, formData, trackType);
    if (trackType === 'webcam') {
      webcamUploadQueue = updated;
    } else {
      screenUploadQueue = updated;
    }
  }
 
  function pickMimeType() {
    const candidates = [
      'video/webm;codecs=vp9',
      'video/webm;codecs=vp8',
      'video/webm'
    ];
    for (const type of candidates) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(type)) {
        return type;
      }
    }
    return '';
  }
 
  async function startWebcamRecording(sessionKey) {
    const body = document.body;
    const shouldRecord = body.dataset.recordWebcam === 'true';
    currentSessionKey = sessionKey || currentSessionKey;
 
    if (!shouldRecord) {
      console.log('[capture_session] Recording disabled via data-record-webcam="false" — skipping.');
      return;
    }
 
    if (!window.MediaRecorder) {
      console.warn('[capture_session] MediaRecorder is not available in this browser — recording skipped.');
      return;
    }
 
    try {
      console.log('[capture_session] Initializing capture devices...');
 
      webcamStream = await navigator.mediaDevices.getUserMedia({
        video: { width: 1280, height: 720, frameRate: { ideal: 30 } },
        audio: false
      });
      // NOTE: getUserMedia resolving is device-permission time, not
      // recording start — no log here anymore. See onstart below.
 
      screenStream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: 1920, height: 1080, frameRate: { ideal: 60 } },
        audio: false
      });
 
      recording = true;
      chunkSequence = { webcam: 0, screen: 0 };
      webcamUploadQueue = Promise.resolve();
      screenUploadQueue = Promise.resolve();
      if (testModeEnabled()) {
        resetTestModeState();
        console.log('[capture_session] TEST MODE enabled — recordings will be buffered locally and downloaded on stop.');
      }
 
      const mimeType = pickMimeType();
 
      webcamRecorder = new MediaRecorder(
        webcamStream,
        mimeType ? { mimeType, videoBitsPerSecond: 2000000 } : undefined
      );
      webcamRecorder.ondataavailable = (e) => handleChunk('webcam', e.data, false);
      webcamRecorder.onerror = (e) => console.error('[capture_session] Webcam MediaRecorder error:', e);
      // onstart fires the instant the recorder actually begins producing
      // data — this is the true anchor for offset_webcam_ms math, not the
      // moment .start() was called (there can be a few ms of setup lag).
      webcamRecorder.onstart = () => logActivityEvent('recording_start', 'webcam');
      webcamRecorder.start(CHUNK_TIMESLICE_MS);
 
      screenRecorder = new MediaRecorder(
        screenStream,
        mimeType ? { mimeType, videoBitsPerSecond: 5000000 } : undefined
      );
      screenRecorder.ondataavailable = (e) => handleChunk('screen', e.data, false);
      screenRecorder.onerror = (e) => console.error('[capture_session] Screen MediaRecorder error:', e);
      screenRecorder.onstart = () => logActivityEvent('recording_start', 'screen');
      screenRecorder.start(CHUNK_TIMESLICE_MS);
 
      console.log('[capture_session] Dual recording engine online (MediaRecorder, mimeType: ' + (mimeType || 'browser default') + ').');
    } catch (err) {
      console.warn('[capture_session] Critical error starting dual recording pipelines:', err);
      stopWebcamRecording();
    }
  }
 
  async function stopWebcamRecording() {
    recording = false;
    console.log('[capture_session] Initiating session shutdown sequence...');
 
    const stopPromises = [];
 
    if (webcamRecorder && webcamRecorder.state !== 'inactive') {
      stopPromises.push(
        new Promise((resolve) => {
          webcamRecorder.ondataavailable = (e) => {
            handleChunk('webcam', e.data, true);
            resolve();
          };
          webcamRecorder.stop();
        })
      );
    }
    if (screenRecorder && screenRecorder.state !== 'inactive') {
      stopPromises.push(
        new Promise((resolve) => {
          screenRecorder.ondataavailable = (e) => {
            handleChunk('screen', e.data, true);
            resolve();
          };
          screenRecorder.stop();
        })
      );
    }
 
    await Promise.all(stopPromises);
 
    if (webcamStream) {
      webcamStream.getTracks().forEach((t) => t.stop());
      webcamStream = null;
      logActivityEvent('recording_stop', 'webcam');
    }
    if (screenStream) {
      screenStream.getTracks().forEach((t) => t.stop());
      screenStream = null;
      logActivityEvent('recording_stop', 'screen');
    }
 
    webcamRecorder = null;
    screenRecorder = null;
 
    await Promise.all([webcamUploadQueue, screenUploadQueue]);
 
    if (testModeEnabled()) {
      finalizeTestModeOutput();
    }
 
    console.log('[capture_session] Recorders and upload queues drained. Recording fully stopped.');
  }
 
  function initRealEye() {
    if (window.RealEye) {
      console.log('[capture_session] RealEye SDK detected. Wiring trigger loops.');
    } else {
      console.log('[capture_session] RealEye SDK not detected on this page.');
    }
  }
 
  return {
    start: startWebcamRecording,
    stop: stopWebcamRecording,
    initRealEye,
    logEvent: logActivityEvent, // exposed so app.js can log module_start/module_end, login_complete, etc.
  };
})();
 