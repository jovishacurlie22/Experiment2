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
  // upload small (well under proxy body-size limits) and fast (well under
  // Cloudflare's ~100s per-request limit, which applies to named tunnels
  // too), instead of buffering a full 30-minute session and sending it as
  // one request.
  const CHUNK_FLUSH_INTERVAL_MS = 15000; // flush at least every 15s
  const CHUNK_FLUSH_SIZE_BYTES = 4 * 1024 * 1024; // or every ~4MB, whichever first

  // If uploads ever fall behind (slow uplink, server hiccup), the backlog is
  // split into pieces of at most this size so no single request gets huge.
  const CHUNK_MAX_BYTES = 6 * 1024 * 1024;

  // A chunk is retried until the server acknowledges it -- it is never
  // silently dropped. Backoff grows 2s, 4s, 8s, 16s... capped at the max.
  const UPLOAD_RETRY_BASE_MS = 1000;
  const UPLOAD_RETRY_MAX_MS = 15000;
  // Abort (and retry) a request that hangs, staying under the ~100s edge limit.
  const UPLOAD_REQUEST_TIMEOUT_MS = 90000;
  // When the session ends, keep trying to drain the backlog for this long.
  // Anything still unsent after that is saved to the participant's disk
  // instead of being lost (see saveUndeliveredLocally).
  const STOP_DRAIN_TIMEOUT_MS = 10 * 60 * 1000;

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
      flushChain: Promise.resolve(), // strictly serializes flushes; nothing is ever dropped
      flushTimer: null,
      totalFrameCount: 0,
      firstTimestampUs: null,
      lastTimestampUs: null,
      sizeFlushQueued: false,        // at most one size-triggered flush waiting at a time
      undelivered: [],               // chunks the server never acknowledged (fallback save)
    };
  }

  let pipelines = { webcam: null, screen: null };
  let recording = false;

  // Uploads keep retrying until this timestamp (Infinity while recording;
  // set to now + STOP_DRAIN_TIMEOUT_MS once the session is stopping).
  let uploadDeadline = Infinity;
  // Flush tasks queued or in flight. Used to warn before the tab is closed.
  let pendingUploadCount = 0;

  window.addEventListener('beforeunload', (e) => {
    if (recording || pendingUploadCount > 0) {
      e.preventDefault();
      e.returnValue = '';
      return '';
    }
    return undefined;
  });

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

        if (pipeline.chunked && pipeline.pendingBytes >= CHUNK_FLUSH_SIZE_BYTES && !pipeline.sizeFlushQueued) {
          // Only one size-triggered flush may wait at a time, so a stalled
          // upload can't pile up thousands of queued no-op tasks.
          pipeline.sizeFlushQueued = true;
          flushPipelineChunk(trackType, pipeline, false)
            .catch((err) =>
              console.error(`[capture_session] Size-triggered flush error (${trackType}):`, err)
            )
            .finally(() => { pipeline.sizeFlushQueued = false; });
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

  function computeAvgFps(pipeline) {
    const durationS = (pipeline.firstTimestampUs !== null && pipeline.lastTimestampUs !== null)
      ? (pipeline.lastTimestampUs - pipeline.firstTimestampUs) / 1e6
      : 0;
    return durationS > 0 ? pipeline.totalFrameCount / durationS : pipeline.targetFps;
  }

  /**
   * Drain whatever's currently buffered in the pipeline (encoded bytes +
   * sidecar frames since the last flush) and POST it. Safe to call
   * repeatedly during recording (isFinal=false) and once more after the
   * encoder is closed (isFinal=true) to send the tail.
   *
   * Every call is chained onto pipeline.flushChain, so flushes run strictly
   * in order and none is ever dropped. If uploads have fallen behind, the
   * backlog is split into pieces of at most CHUNK_MAX_BYTES so one request
   * never becomes huge. Each piece is retried until acknowledged; a piece
   * that can't be delivered goes to pipeline.undelivered (never discarded).
   *
   * The final piece carries total_chunks / total_frames so the server can
   * verify it received everything.
   */
  function flushPipelineChunk(trackType, pipeline, isFinal) {
    pendingUploadCount++;
    const task = async () => {
      try {
        if (pipeline.encodedParts.length === 0 && pipeline.sidecar.length === 0 && !isFinal) return;

        const partsToSend = pipeline.encodedParts;
        const sidecarToSend = pipeline.sidecar;
        pipeline.encodedParts = [];
        pipeline.sidecar = [];
        pipeline.pendingBytes = 0;

        pipeline.totalFrameCount += sidecarToSend.length;
        if (sidecarToSend.length > 0) {
          if (pipeline.firstTimestampUs === null) {
            pipeline.firstTimestampUs = sidecarToSend[0].timestamp_us;
          }
          pipeline.lastTimestampUs = sidecarToSend[sidecarToSend.length - 1].timestamp_us;
        }

        // Group encoded parts so no group exceeds CHUNK_MAX_BYTES.
        const groups = [];
        let current = [];
        let currentBytes = 0;
        for (const part of partsToSend) {
          if (current.length > 0 && currentBytes + part.byteLength > CHUNK_MAX_BYTES) {
            groups.push(current);
            current = [];
            currentBytes = 0;
          }
          current.push(part);
          currentBytes += part.byteLength;
        }
        if (current.length > 0 || groups.length === 0) groups.push(current);

        for (let g = 0; g < groups.length; g++) {
          const isLastGroup = g === groups.length - 1;
          const finalHere = !!isFinal && isLastGroup;
          const chunkIndex = pipeline.chunkIndex++;

          const rawBlob = new Blob(groups[g], { type: 'video/H264' });
          // The server aggregates frames across chunks in order, so the
          // sidecar frames simply ride on the first piece of this flush.
          const sidecarChunkJson = {
            session_key: currentSessionKey || '',
            stream_source: trackType,
            chunk_index: chunkIndex,
            is_final: finalHere,
            target_fps: pipeline.targetFps,
            frame_count: g === 0 ? sidecarToSend.length : 0,
            frames: g === 0 ? sidecarToSend : [],
          };
          if (finalHere) {
            sidecarChunkJson.total_chunks = chunkIndex + 1;
            sidecarChunkJson.total_frames = pipeline.totalFrameCount;
            sidecarChunkJson.avg_fps = computeAvgFps(pipeline);
          }

          const delivered = await uploadRecordingChunk(trackType, chunkIndex, finalHere, rawBlob, sidecarChunkJson);
          if (!delivered) {
            pipeline.undelivered.push({ chunkIndex, rawBlob, sidecar: sidecarChunkJson });
          }
        }
      } finally {
        pendingUploadCount--;
      }
    };

    // .then(task, task) means the next flush runs whether the previous
    // one resolved or rejected -- one bad chunk can't wedge the queue.
    const next = pipeline.flushChain.then(task, task);
    pipeline.flushChain = next;
    return next;
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

      const avgFps = computeAvgFps(pipeline);

      return {
        chunked: true,
        undelivered: pipeline.undelivered,
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

  // Uploads one chunk and only returns once the server has acknowledged it
  // (true), or when it is clearly pointless to keep trying (false):
  //   - the stop-time drain deadline has passed, or
  //   - the server rejected the chunk with a permanent 4xx error.
  // Network errors, timeouts, 5xx (incl. 502/504) and 408/409/429 are
  // retried with capped exponential backoff. The server stores each chunk
  // under its chunk_index, so re-sending a chunk is always safe. raw_bytes
  // lets the server reject a truncated upload (409) so it gets re-sent.
  async function uploadRecordingChunk(trackType, chunkIndex, isFinal, rawBlob, sidecarChunkJson) {
    const formData = new FormData();
    formData.append('video_raw', rawBlob, `${trackType}-${chunkIndex}.h264`);
    formData.append('sidecar_json', JSON.stringify(sidecarChunkJson));
    formData.append('stream_source', trackType);
    formData.append('session_key', currentSessionKey || '');
    formData.append('chunk_index', String(chunkIndex));
    formData.append('is_final', isFinal ? 'true' : 'false');
    formData.append('raw_bytes', String(rawBlob.size));

    for (let attempt = 1; ; attempt++) {
      if (Date.now() > uploadDeadline) {
        console.error(`[capture_session] Giving up on ${trackType} chunk ${chunkIndex}: upload deadline passed.`);
        return false;
      }

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), UPLOAD_REQUEST_TIMEOUT_MS);
      try {
        const resp = await fetch('/api/upload-recording/', {
          method: 'POST',
          body: formData,
          credentials: 'same-origin',
          headers: { 'X-CSRFToken': getCookie('csrftoken') },
          signal: controller.signal,
        });
        clearTimeout(timer);

        if (resp.ok) {
          console.log(
            `[capture_session] Uploaded ${trackType} chunk ${chunkIndex}` +
            `${isFinal ? ' (final)' : ''} (${rawBlob.size} bytes, ${sidecarChunkJson.frame_count} frames).`
          );
          return true;
        }

        const permanent = resp.status >= 400 && resp.status < 500 &&
          ![408, 409, 429].includes(resp.status);
        console.error(
          `[capture_session] Chunk upload failed for ${trackType} #${chunkIndex}: HTTP ${resp.status}` +
          `${permanent ? ' (permanent, not retrying)' : ` (attempt ${attempt}, will retry)`}`
        );
        if (permanent) return false;
      } catch (err) {
        clearTimeout(timer);
        console.error(`[capture_session] Chunk upload error for ${trackType} #${chunkIndex} (attempt ${attempt}, will retry):`, err);
      }

      await sleep(Math.min(UPLOAD_RETRY_MAX_MS, UPLOAD_RETRY_BASE_MS * 2 ** Math.min(attempt, 5)));
    }
  }

  // Last resort: chunks the server never acknowledged are written to the
  // participant's Downloads folder as one .h264 plus a JSON index, so the
  // footage still exists and can be merged in later.
  function saveUndeliveredLocally(trackType, undelivered) {
    if (!undelivered || undelivered.length === 0) return;
    const sorted = undelivered.slice().sort((a, b) => a.chunkIndex - b.chunkIndex);

    const index = [];
    let offset = 0;
    for (const c of sorted) {
      index.push({
        chunk_index: c.chunkIndex,
        byte_offset: offset,
        byte_length: c.rawBlob.size,
        is_final: c.sidecar.is_final,
        frame_count: c.sidecar.frame_count,
        frames: c.sidecar.frames,
      });
      offset += c.rawBlob.size;
    }

    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const base = `UNSENT-${currentSessionKey || 'nosession'}-${trackType}-${stamp}`;
    triggerDownload(new Blob(sorted.map((c) => c.rawBlob), { type: 'video/H264' }), `${base}.h264`);
    triggerDownload(
      new Blob(
        [JSON.stringify({ session_key: currentSessionKey || '', stream_source: trackType, chunks: index }, null, 2)],
        { type: 'application/json' }
      ),
      `${base}.json`
    );
    console.error(
      `[capture_session] ${sorted.length} ${trackType} chunk(s) could not be uploaded; saved locally as ${base}.*`
    );
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
    uploadDeadline = Infinity;

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
    const stopRequestedEpochMs = Date.now();
    uploadDeadline = stopRequestedEpochMs + STOP_DRAIN_TIMEOUT_MS;
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
        // recording_stop is logged after the upload backlog drains; this is
        // when capture actually ended.
        stop_requested_epoch_ms: stopRequestedEpochMs,
      });

      if (result.chunked) {
        // Bytes already went up incrementally during recording via
        // flushPipelineChunk(). Anything the server never acknowledged is
        // saved locally rather than lost.
        saveUndeliveredLocally(trackType, result.undelivered);
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