/* ==========================================================================
   capture_session.js (WebCodecs raw elementary stream implementation)
   ========================================================================== */

const CaptureSession = (() => {
  let currentSessionKey = null;

  let webcamStream = null;
  let screenStream = null;

  const WEBCODECS_SUPPORTED =
    typeof window.MediaStreamTrackProcessor !== 'undefined' &&
    typeof window.VideoEncoder !== 'undefined';

  // Per-stream pipeline state
  function freshPipeline() {
    return {
      encoder: null,
      trackProcessor: null,
      reader: null,
      pumpDone: null,       // promise resolved when pump loop exits
      frameIndex: 0,
      sidecar: [],          // [{frame_index, timestamp_us, capture_perf_ms, is_keyframe}]
      encodedParts: [],     // raw Annex-B byte chunks, in order
      firstFrameLogged: false,
      targetFps: null,
    };
  }

  let pipelines = { webcam: null, screen: null };
  let recording = false;

  /* ---------------------------------------------------------------- */
  /* Activity/epoch logging                                            */
  /* ---------------------------------------------------------------- */

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

  function finalizeTestModeOutput(trackType, rawBlob, sidecarJson) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    triggerDownload(rawBlob, `${trackType}-${stamp}.h264`);
    triggerDownload(
      new Blob([JSON.stringify(sidecarJson, null, 2)], { type: 'application/json' }),
      `${trackType}-${stamp}-sidecar.json`
    );
    console.log(
      `[capture_session][TEST MODE] ${trackType}: ${sidecarJson.frames.length} frames, ` +
      `avg fps ${sidecarJson.avg_fps.toFixed(2)}`
    );
  }

  /* ---------------------------------------------------------------- */
  /* Codec negotiation                                                  */
  /* ---------------------------------------------------------------- */

  // Try a small set of AVC (H.264) baseline/high level strings and return
  // the first one the browser actually supports for this geometry, via
  // VideoEncoder.isConfigSupported rather than guessing blind.
  async function pickAvcConfig(width, height, framerate, bitrate) {
    const candidates = [
      'avc1.640033', // High @ L5.1 — generous headroom for 1080p60
      'avc1.4d0033', // Main @ L5.1
      'avc1.42003e', // Baseline @ L6.2 — very permissive fallback
    ];
    for (const codec of candidates) {
      const config = {
        codec,
        width,
        height,
        bitrate,
        framerate,
        avc: { format: 'annexb' },
      };
      try {
        const support = await VideoEncoder.isConfigSupported(config);
        if (support.supported) return config;
      } catch (e) {
        // fall through and try the next candidate
      }
    }
    return null;
  }

  /* ---------------------------------------------------------------- */
  /* Pipeline: capture -> encode -> buffer                              */
  /* ---------------------------------------------------------------- */

  async function startPipeline(trackType, stream, width, height, framerate, bitrate) {
    const track = stream.getVideoTracks()[0];
    const config = await pickAvcConfig(width, height, framerate, bitrate);
    if (!config) {
      console.error(`[capture_session] No supported AVC config for ${trackType} at ${width}x${height}@${framerate}`);
      return null;
    }

    const pipeline = freshPipeline();
    pipeline.targetFps = framerate;

    pipeline.encoder = new VideoEncoder({
      output: (chunk, metadata) => {
        const buf = new Uint8Array(chunk.byteLength);
        chunk.copyTo(buf);
        pipeline.encodedParts.push(buf);
      },
      error: (e) => console.error(`[capture_session] VideoEncoder error (${trackType}):`, e),
    });
    pipeline.encoder.configure(config);

    pipeline.trackProcessor = new MediaStreamTrackProcessor({ track });
    pipeline.reader = pipeline.trackProcessor.readable.getReader();

    pipeline.pumpDone = (async () => {
      try {
        while (true) {
          const { value: frame, done } = await pipeline.reader.read();
          if (done) break;

          if (!pipeline.firstFrameLogged) {
            pipeline.firstFrameLogged = true;
            // True capture-pipeline start: the first frame actually pulled
            // off the track, not the moment we asked for the stream.
            logActivityEvent('recording_start', trackType);
          }

          const idx = pipeline.frameIndex++;
          // Force a keyframe periodically so the eventual mp4 is seekable
          // and so a dropped upload doesn't invalidate the whole stream.
          const keyFrame = idx % (framerate * 2) === 0; // ~every 2s
          pipeline.sidecar.push({
            frame_index: idx,
            timestamp_us: frame.timestamp,
            capture_perf_ms: performance.now(),
            is_keyframe: keyFrame,
          });

          pipeline.encoder.encode(frame, { keyFrame });
          frame.close();
        }
      } catch (err) {
        console.error(`[capture_session] Pump loop error (${trackType}):`, err);
      }
    })();

    return pipeline;
  }

  async function stopPipeline(trackType, pipeline) {
    if (!pipeline) return null;

    // Cancelling the reader unblocks the pump loop's `await read()`.
    try { await pipeline.reader.cancel(); } catch (e) { /* already closed */ }
    await pipeline.pumpDone;

    if (pipeline.encoder && pipeline.encoder.state !== 'closed') {
      await pipeline.encoder.flush();
      pipeline.encoder.close();
    }

    const rawBlob = new Blob(pipeline.encodedParts, { type: 'video/H264' });
    const frameCount = pipeline.sidecar.length;
    const durationS = frameCount > 0
      ? (pipeline.sidecar[frameCount - 1].timestamp_us - pipeline.sidecar[0].timestamp_us) / 1e6
      : 0;
    const avgFps = durationS > 0 ? frameCount / durationS : pipeline.targetFps;

    const sidecarJson = {
      session_key: currentSessionKey || '',
      stream_source: trackType,
      target_fps: pipeline.targetFps,
      avg_fps: avgFps,
      frame_count: frameCount,
      frames: pipeline.sidecar,
    };

    return { rawBlob, sidecarJson };
  }

  /* ---------------------------------------------------------------- */
  /* Upload                                                             */
  /* ---------------------------------------------------------------- */

  function getCookie(name) {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match.pop()) : '';
  }

  async function uploadRecording(trackType, rawBlob, sidecarJson) {
    const formData = new FormData();
    formData.append('video_raw', rawBlob, `${trackType}.h264`);
    formData.append('sidecar_json', JSON.stringify(sidecarJson));
    formData.append('stream_source', trackType);
    formData.append('session_key', currentSessionKey || '');

    try {
      const resp = await fetch('/api/upload-recording/', {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': getCookie('csrftoken') },
      });
      if (!resp.ok) {
        console.error(`[capture_session] Upload failed for ${trackType}: HTTP ${resp.status}`);
      } else {
        console.log(`[capture_session] Uploaded ${trackType} recording (${rawBlob.size} bytes, ${sidecarJson.frame_count} frames).`);
      }
    } catch (err) {
      console.error(`[capture_session] Upload error for ${trackType}:`, err);
    }
  }

  /* ---------------------------------------------------------------- */
  /* Public: start / stop                                              */
  /* ---------------------------------------------------------------- */

  async function startWebcamRecording(sessionKey) {
    const body = document.body;
    const shouldRecord = body.dataset.recordWebcam === 'true';
    currentSessionKey = sessionKey || currentSessionKey;

    if (!shouldRecord) {
      console.log('[capture_session] Recording disabled via data-record-webcam="false" — skipping.');
      return;
    }

    if (!WEBCODECS_SUPPORTED) {
      console.warn('[capture_session] WebCodecs (MediaStreamTrackProcessor/VideoEncoder) not available in this browser — recording skipped.');
      return;
    }

    try {
      console.log('[capture_session] Initializing capture devices...');

      webcamStream = await navigator.mediaDevices.getUserMedia({
        video: { width: 1280, height: 720, frameRate: { ideal: 30 } },
        audio: false,
      });
      screenStream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: 1920, height: 1080, frameRate: { ideal: 60 } },
        audio: false,
      });

      recording = true;

      pipelines.webcam = await startPipeline('webcam', webcamStream, 1280, 720, 30, 2_000_000);
      pipelines.screen = await startPipeline('screen', screenStream, 1920, 1080, 60, 5_000_000);

      console.log('[capture_session] WebCodecs dual capture pipelines online (raw AVC Annex-B, server-side muxing).');
    } catch (err) {
      console.warn('[capture_session] Critical error starting dual capture pipelines:', err);
      await stopWebcamRecording();
    }
  }

  async function stopWebcamRecording() {
    recording = false;
    console.log('[capture_session] Initiating session shutdown sequence...');

    const results = {};
    for (const trackType of ['webcam', 'screen']) {
      const pipeline = pipelines[trackType];
      pipelines[trackType] = null;
      if (!pipeline) continue;
      results[trackType] = await stopPipeline(trackType, pipeline);
    }

    if (webcamStream) {
      webcamStream.getTracks().forEach((t) => t.stop());
      webcamStream = null;
    }
    if (screenStream) {
      screenStream.getTracks().forEach((t) => t.stop());
      screenStream = null;
    }

    for (const trackType of ['webcam', 'screen']) {
      const result = results[trackType];
      if (!result) continue;

      logActivityEvent('recording_stop', trackType, {
        frame_count: result.sidecarJson.frame_count,
        avg_fps: result.sidecarJson.avg_fps,
      });

      if (testModeEnabled()) {
        finalizeTestModeOutput(trackType, result.rawBlob, result.sidecarJson);
      } else {
        await uploadRecording(trackType, result.rawBlob, result.sidecarJson);
      }
    }

    console.log('[capture_session] Recorders stopped, encoders flushed, uploads complete.');
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
    logEvent: logActivityEvent,
  };
})();