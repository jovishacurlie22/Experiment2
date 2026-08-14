/* ==========================================================================
   capture_session.js (MediaRecorder implementation)
   ----------------------------------------------------------------------
   Captures Webcam and Screen Recording in parallel using the native
   MediaRecorder API. Each stream records to its own MediaRecorder instance,
   emitting a Blob chunk every CHUNK_TIMESLICE_MS via ondataavailable, which
   is streamed to the Django backend on independent, per-stream upload
   queues (mirrors the original chunk-upload contract: video_chunk,
   stream_source, sequence, is_last, session_key).

   Why MediaRecorder instead of hand-rolled WebCodecs + MP4Box: that
   pipeline hit a reproducible issue where the encoder never emitted a
   keyframe (chunk.type stayed 'delta' even when a keyframe was explicitly
   requested), so MP4Box's segmentation never initialized and no chunk was
   ever produced. MediaRecorder sidesteps the keyframe/muxing layer
   entirely — the browser handles container framing internally.

   Frame-accurate timing is recovered *after* the fact instead of live:
   each recorded file is a real, valid (variable-frame-rate) video with
   accurate per-frame timestamps baked into its container, extracted
   afterward via ffprobe's pts_time (same technique already used for
   Experiment 1 in frame_align.py) rather than guaranteeing it frame-by-
   frame inside the browser.

   TEST MODE
   ----------------------------------------------------------------------
   Set `document.body.dataset.captureTestMode = "true"` (or the
   `data-capture-test-mode="true"` attribute in index.html) to turn this on.
   With it enabled, chunks are buffered locally instead of uploaded, and on
   stop() each stream is assembled into a downloadable file plus a console
   summary so you can play back and verify without the Django endpoint
   running. Production behavior (network upload) is unchanged either way.
   ========================================================================== */

const CaptureSession = (() => {
  // Set once per session by app.js right before calling start(), via
  // CaptureSession.start(sessionKey). Included on every uploaded chunk so
  // the Django backend can attribute it to the right StudySession.
  let currentSessionKey = null;

  let webcamStream = null;
  let screenStream = null;

  let webcamRecorder = null;
  let screenRecorder = null;

  let recording = false;
  let chunkSequence = { webcam: 0, screen: 0 };

  // Separate serialized upload chains per stream so a slow/backed-up screen
  // upload can't stall webcam chunk delivery, and vice versa.
  let webcamUploadQueue = Promise.resolve();
  let screenUploadQueue = Promise.resolve();

  // How often MediaRecorder hands us a Blob. Smaller = more frequent
  // uploads (less data at risk if the tab crashes) but more HTTP overhead.
  const CHUNK_TIMESLICE_MS = 2000;

  /* ---------------------------------------------------------------- */
  /* Test-mode state — local buffering                                  */
  /* ---------------------------------------------------------------- */

  function testModeEnabled() {
    return document.body.dataset.captureTestMode === "true";
  }

  // Independent per-stream buffers — webcam and screen never share an
  // array, so segments can never end up interleaved into the wrong file.
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

  // Assembles each stream's buffered chunks into one .webm blob, triggers
  // a download for both, and prints a summary. Safe to call even if
  // one/both streams captured zero chunks.
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
    formData.append('stream_source', trackType); // 'webcam' or 'screen'
    formData.append('sequence', chunkSequence[trackType]);
    formData.append('is_last', isLast ? 'true' : 'false');
    formData.append('session_key', currentSessionKey || '');

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
    return ''; // let the browser pick a default
  }

  /**
   * Main setup logic to kick off both capture instances simultaneously
   */
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
      StudyAPI.logEvent(currentSessionKey, 'webcam_recording_started');

      screenStream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: 1920, height: 1080, frameRate: { ideal: 60 } },
        audio: false
      });
      StudyAPI.logEvent(currentSessionKey, 'screen_recording_started');

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
      webcamRecorder.start(CHUNK_TIMESLICE_MS);

      screenRecorder = new MediaRecorder(
        screenStream,
        mimeType ? { mimeType, videoBitsPerSecond: 5000000 } : undefined
      );
      screenRecorder.ondataavailable = (e) => handleChunk('screen', e.data, false);
      screenRecorder.onerror = (e) => console.error('[capture_session] Screen MediaRecorder error:', e);
      screenRecorder.start(CHUNK_TIMESLICE_MS);

      console.log('[capture_session] Dual recording engine online (MediaRecorder, mimeType: ' + (mimeType || 'browser default') + ').');
    } catch (err) {
      console.warn('[capture_session] Critical error starting dual recording pipelines:', err);
      stopWebcamRecording();
    }
  }

  /**
   * Gracefully stop both recorders, flush their final chunk, and drain
   * upload queues before resolving.
   */
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
      StudyAPI.logEvent(currentSessionKey, 'webcam_recording_stopped');
    }
    if (screenStream) {
      screenStream.getTracks().forEach((t) => t.stop());
      screenStream = null;
      StudyAPI.logEvent(currentSessionKey, 'screen_recording_stopped');
    }

    webcamRecorder = null;
    screenRecorder = null;

    // Make sure any in-flight uploads for both streams settle before we
    // consider the session fully closed.
    await Promise.all([webcamUploadQueue, screenUploadQueue]);

    if (testModeEnabled()) {
      finalizeTestModeOutput();
    }

    console.log('[capture_session] Recorders and upload queues drained. Recording fully stopped.');
  }

  function initRealEye() {
    if (window.RealEye) {
      console.log('[capture_session] RealEye SDK detected. Wiring trigger loops.');
      // Bind to RealEye life cycle calls if required by your pipeline architecture
      // window.RealEye.start();
    } else {
      console.log('[capture_session] RealEye SDK not detected on this page.');
    }
  }

  return {
    start: startWebcamRecording,
    stop: stopWebcamRecording,
    initRealEye
  };
})();