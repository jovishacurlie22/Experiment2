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

  // Chunked-upload tuning. We flush (and POST) whichever comes first:
  // a time interval or an accumulated byte threshold. This keeps each
  // upload small (well under typical body-size limits) and fast (well
  // under the Cloudflare quick-tunnel's ~100s request timeout), instead
  // of buffering a full 30-minute session and sending it as one request.
  const CHUNK_FLUSH_INTERVAL_MS = 15000; // flush at least every 15s
  const CHUNK_FLUSH_SIZE_BYTES = 4 * 1024 * 1024; // or every ~4MB, whichever first
  const CHUNK_UPLOAD_MAX_RETRIES = 2;

  // Per-stream pipeline state
  function freshPipeline() {
    return {
      encoder: null,
      trackProcessor: null,
      reader: null,
      pumpDone: null,       // promise resolved when pump loop exits
      frameIndex: 0,
      sidecar: [],          // frames buffered since the last flush
      encodedParts: [],     // raw Annex-B byte chunks buffered since the last flush
      firstFrameLogged: false,
      targetFps: null,

      // Chunked-upload state (production mode only; test mode still
      // buffers everything and downloads one file at the end).
      chunked: false,
      chunkIndex: 0,
      pendingBytes: 0,
      flushing: false,
      flushTimer: null,
      totalFrameCount: 0,
      firstTimestampUs: null,
      lastTimestampUs: null,
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
    pipeline.chunked = !testModeEnabled();

    pipeline.encoder = new VideoEncoder({
      output: (chunk, metadata) => {
        const buf = new Uint8Array(chunk.byteLength);
        chunk.copyTo(buf);
        pipeline.encodedParts.push(buf);
        pipeline.pendingBytes += buf.byteLength;

        if (pipeline.chunked && !pipeline.flushing && pipeline.pendingBytes >= CHUNK_FLUSH_SIZE_BYTES) {
          flushPipelineChunk(trackType, pipeline, false).catch((err) =>
            console.error(`[capture_session] Size-triggered flush error (${trackType}):`, err)
          );
        }
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

    if (pipeline.chunked) {
      pipeline.flushTimer = setInterval(() => {
        flushPipelineChunk(trackType, pipeline, false).catch((err) =>
          console.error(`[capture_session] Periodic flush error (${trackType}):`, err)
        );
      }, CHUNK_FLUSH_INTERVAL_MS);
    }

    return pipeline;
  }

  /**
   * Drain whatever's currently buffered in the pipeline (encoded bytes +
   * sidecar frames since the last flush) and POST it as one small chunk.
   * Safe to call repeatedly during recording (isFinal=false) and once
   * more after the encoder is closed (isFinal=true) to send the tail.
   */
  async function flushPipelineChunk(trackType, pipeline, isFinal) {
    if (pipeline.flushing) return;
    if (pipeline.encodedParts.length === 0 && pipeline.sidecar.length === 0 && !isFinal) return;
    pipeline.flushing = true;

    const partsToSend = pipeline.encodedParts;
    const sidecarToSend = pipeline.sidecar;
    pipeline.encodedParts = [];
    pipeline.sidecar = [];
    pipeline.pendingBytes = 0;

    const chunkIndex = pipeline.chunkIndex++;

    pipeline.totalFrameCount += sidecarToSend.length;
    if (sidecarToSend.length > 0) {
      if (pipeline.firstTimestampUs === null) {
        pipeline.firstTimestampUs = sidecarToSend[0].timestamp_us;
      }
      pipeline.lastTimestampUs = sidecarToSend[sidecarToSend.length - 1].timestamp_us;
    }

    const rawBlob = new Blob(partsToSend, { type: 'video/H264' });
    const sidecarChunkJson = {
      session_key: currentSessionKey || '',
      stream_source: trackType,
      chunk_index: chunkIndex,
      is_final: !!isFinal,
      frame_count: sidecarToSend.length,
      frames: sidecarToSend,
    };

    try {
      await uploadRecordingChunk(trackType, chunkIndex, isFinal, rawBlob, sidecarChunkJson);
    } finally {
      pipeline.flushing = false;
    }
  }

  async function stopPipeline(trackType, pipeline) {
    if (!pipeline) return null;

    // Cancelling the reader unblocks the pump loop's `await read()`.
    try { await pipeline.reader.cancel(); } catch (e) { /* already closed */ }
    await pipeline.pumpDone;

    if (pipeline.flushTimer) {
      clearInterval(pipeline.flushTimer);
      pipeline.flushTimer = null;
    }

    if (pipeline.encoder && pipeline.encoder.state !== 'closed') {
      await pipeline.encoder.flush();
      pipeline.encoder.close();
    }

    if (pipeline.chunked) {
      // Send the tail end as the final chunk, then report summary stats
      // only — the bytes themselves already went up incrementally.
      await flushPipelineChunk(trackType, pipeline, true);

      const durationS = (pipeline.firstTimestampUs !== null && pipeline.lastTimestampUs !== null)
        ? (pipeline.lastTimestampUs - pipeline.firstTimestampUs) / 1e6
        : 0;
      const avgFps = durationS > 0 ? pipeline.totalFrameCount / durationS : pipeline.targetFps;

      return {
        chunked: true,
        sidecarJson: {
          session_key: currentSessionKey || '',
          stream_source: trackType,
          target_fps: pipeline.targetFps,
          avg_fps: avgFps,
          frame_count: pipeline.totalFrameCount,
        },
      };
    }

    // Test mode: unchanged full-buffer behaviour, local download only.
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

    return { chunked: false, rawBlob, sidecarJson };
  }

  /* ---------------------------------------------------------------- */
  /* Upload                                                             */
  /* ---------------------------------------------------------------- */

  function getCookie(name) {
    const match = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return match ? decodeURIComponent(match.pop()) : '';
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // Uploads one small chunk. Server-side, /api/upload-recording/ needs to
  // branch on `chunk_index`/`is_final` in the POST body: append this
  // chunk's video_raw bytes and sidecar frames to the session's
  // in-progress files (by chunk_index order), and only kick off the
  // existing mux/finalize step once is_final=true arrives.
  async function uploadRecordingChunk(trackType, chunkIndex, isFinal, rawBlob, sidecarChunkJson) {
    const formData = new FormData();
    formData.append('video_raw', rawBlob, `${trackType}-${chunkIndex}.h264`);
    formData.append('sidecar_json', JSON.stringify(sidecarChunkJson));
    formData.append('stream_source', trackType);
    formData.append('session_key', currentSessionKey || '');
    formData.append('chunk_index', String(chunkIndex));
    formData.append('is_final', isFinal ? 'true' : 'false');

    for (let attempt = 0; attempt <= CHUNK_UPLOAD_MAX_RETRIES; attempt++) {
      try {
        const resp = await fetch('/api/upload-recording/', {
          method: 'POST',
          body: formData,
          credentials: 'same-origin',
          headers: { 'X-CSRFToken': getCookie('csrftoken') },
        });
        if (resp.ok) {
          console.log(
            `[capture_session] Uploaded ${trackType} chunk ${chunkIndex}` +
            `${isFinal ? ' (final)' : ''} (${rawBlob.size} bytes, ${sidecarChunkJson.frame_count} frames).`
          );
          return;
        }
        console.error(`[capture_session] Chunk upload failed for ${trackType} #${chunkIndex}: HTTP ${resp.status}`);
      } catch (err) {
        console.error(`[capture_session] Chunk upload error for ${trackType} #${chunkIndex}:`, err);
      }
      if (attempt < CHUNK_UPLOAD_MAX_RETRIES) await sleep(1000 * (attempt + 1));
    }
    console.error(`[capture_session] Giving up on ${trackType} chunk ${chunkIndex} after ${CHUNK_UPLOAD_MAX_RETRIES + 1} attempts.`);
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

      if (result.chunked) {
        // Bytes already went up incrementally during recording via
        // flushPipelineChunk(); nothing left to upload here.
      } else if (testModeEnabled()) {
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