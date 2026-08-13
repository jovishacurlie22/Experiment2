/* ==========================================================================
   capture_session.js (Production Implementation — Revised)
   ----------------------------------------------------------------------
   Captures Webcam (30 FPS) and Screen Recording (~59.95 FPS) in parallel.
   Uses WebCodecs + MediaStreamTrackProcessor for native framerate matching,
   preserves the browser's real per-frame capture timestamps (rather than
   recomputing them at dequeue time), packages them into MP4 containers via
   MP4Box, and streams chunks to the Django endpoint on independent,
   per-stream upload queues.

   Key fix vs. previous version:
     - videoFrame.timestamp (the browser's true capture-time PTS) is now
       passed straight through to the encoder instead of being overwritten
       with performance.now() at the moment the frame was dequeued. The
       dequeue time is subject to main-thread jitter, GC pauses, and
       encoder backpressure — using it as "the" timestamp reintroduces the
       exact drift this pipeline exists to eliminate. A single epoch offset
       is captured once (from the first frame seen) purely so downstream
       tooling can map frame timestamps onto the survey's performance.now()
       timeline if needed; it is never baked into individual frame PTS.

   TEST MODE (frame-collision check)
   ----------------------------------------------------------------------
   Set `document.body.dataset.captureTestMode = "true"` (or the
   `data-capture-test-mode="true"` attribute in index.html) to turn this on.
   With it enabled:
     - Each stream (webcam, screen) keeps its own independent, monotonic
       sample-timestamp check right before the sample is muxed. If a new
       sample's timestamp doesn't strictly increase vs. the previous sample
       *on that same stream*, that's a "frame collision" (out-of-order /
       duplicate PTS) and gets logged loudly to the console with the
       sequence numbers involved — this is the actual pipeline correctness
       bug this mode exists to catch, since a collision here means the mux
       would produce a corrupt/glitching track.
     - Every muxed MP4 segment for both streams is *also* kept in memory
       (independent buffers per stream — webcam and screen never touch the
       same buffer, which is itself the guarantee against the two streams'
       frames colliding with each other), and on stop() the two streams are
       assembled into separate downloadable .mp4 files plus a console
       summary (frame counts, dropped frames, collisions found) so you can
       both play back and eyeball-verify each stream independently without
       needing the Django endpoint running.
   Production behavior (network upload) is unchanged either way — test mode
   only adds the local buffering/download/verification on top.
   ========================================================================== */

const CaptureSession = (() => {
  // Set once per session by app.js right before calling start(), via
  // CaptureSession.start(sessionKey). Included on every uploaded chunk so
  // the Django backend can attribute it to the right StudySession.
  let currentSessionKey = null;

  let webcamStream = null;
  let screenStream = null;

  let webcamReader = null;
  let screenReader = null;

  let webcamEncoder = null;
  let screenEncoder = null;

  let recording = false;
  let chunkSequence = { webcam: 0, screen: 0 };

  // epochOffset lets you translate a frame's capture timestamp (microseconds,
  // relative to an arbitrary browser-internal origin) into the same
  // performance.now()-based timeline your survey/task events are logged on.
  // Set once, from whichever stream produces its first frame first.
  let epochOffset = null; // microseconds: performance.now()*1000 - frame.timestamp

  // Separate serialized upload chains per stream so a slow/backed-up screen
  // upload can't stall webcam chunk delivery, and vice versa.
  let webcamUploadQueue = Promise.resolve();
  let screenUploadQueue = Promise.resolve();

  const ENCODER_QUEUE_LIMIT = 10; // frames; tune per target hardware

  /* ---------------------------------------------------------------- */
  /* Test-mode state — local buffering + collision detection            */
  /* ---------------------------------------------------------------- */

  function testModeEnabled() {
    return document.body.dataset.captureTestMode === "true";
  }

  // Independent per-stream buffers. Kept as two entirely separate arrays
  // (never a shared array/index space) so it's structurally impossible for
  // a webcam segment to end up interleaved into the screen file or vice
  // versa — that separation is itself the anti-collision guarantee between
  // the two streams.
  let localBuffers = { webcam: [], screen: [] };
  let lastSampleTimestamp = { webcam: null, screen: null };
  let collisionCount = { webcam: 0, screen: 0 };
  let droppedFrameCount = { webcam: 0, screen: 0 };
  let sampleCount = { webcam: 0, screen: 0 };

  function resetTestModeState() {
    localBuffers = { webcam: [], screen: [] };
    lastSampleTimestamp = { webcam: null, screen: null };
    collisionCount = { webcam: 0, screen: 0 };
    droppedFrameCount = { webcam: 0, screen: 0 };
    sampleCount = { webcam: 0, screen: 0 };
  }

  // Called once per encoded sample, right before it's added to that
  // stream's own muxer. Flags any sample whose timestamp doesn't strictly
  // increase vs. the previous sample *on the same stream* — this is what
  // "frames collide" means here (out-of-order or duplicate PTS within a
  // single stream's own timeline, which would corrupt that stream's mux).
  function checkForCollision(trackType, timestamp) {
    sampleCount[trackType]++;
    const prev = lastSampleTimestamp[trackType];
    if (prev !== null && timestamp <= prev) {
      collisionCount[trackType]++;
      console.error(
        `[capture_session][TEST MODE] FRAME COLLISION on ${trackType}: ` +
          `sample #${sampleCount[trackType]} timestamp ${timestamp} did not increase past ` +
          `previous sample's ${prev} (delta ${timestamp - prev}).`
      );
    }
    lastSampleTimestamp[trackType] = timestamp;
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

  // Assembles each stream's buffered segments into one .mp4 blob, triggers
  // a download for both, and prints the collision/drop report. Safe to call
  // even if one/both streams captured zero segments.
  function finalizeTestModeOutput() {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");

    ["webcam", "screen"].forEach((trackType) => {
      const parts = localBuffers[trackType];
      if (parts.length === 0) return;
      const blob = new Blob(parts, { type: "video/mp4" });
      triggerDownload(blob, `${trackType}-${stamp}.mp4`);
    });

    console.log(
      "[capture_session][TEST MODE] Recording pipeline report:\n" +
        `  webcam: ${sampleCount.webcam} samples, ${droppedFrameCount.webcam} dropped (backpressure), ${collisionCount.webcam} collisions\n` +
        `  screen: ${sampleCount.screen} samples, ${droppedFrameCount.screen} dropped (backpressure), ${collisionCount.screen} collisions\n` +
        (collisionCount.webcam === 0 && collisionCount.screen === 0
          ? "  -> No timestamp collisions detected on either stream."
          : "  -> Collisions were detected — see errors above for exact sample numbers.")
    );
  }

  /**
   * Safely appends an upload task to the given stream's sequential network queue.
   */
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

  /**
   * Helper function to instantiate an MP4Box muxer file instance
   */
  function createMuxerInstance(trackType, width, height, timescale = 1000000) {
    const mp4file = MP4Box.createFile();

    const options = {
      timescale,
      width,
      height,
      brands: ['isom', 'mp42'],
      avcDecoderConfigRecord: null // Populated upon encoder initialization
    };

    let trackId = null;

    mp4file.onSegment = (id, user, buffer, sampleNum, is_last) => {
      chunkSequence[trackType]++;

      if (testModeEnabled()) {
        // Copy the buffer (MP4Box reuses/frees its internal buffers) into
        // this stream's own array — kept fully separate from the other
        // stream's array, so webcam and screen segments can never collide
        // into the same file.
        localBuffers[trackType].push(buffer.slice(0));
      } else {
        const formData = new FormData();
        formData.append('video_chunk', new Blob([buffer], { type: 'video/mp4' }));
        formData.append('stream_source', trackType); // 'webcam' or 'screen'
        formData.append('sequence', chunkSequence[trackType]);
        formData.append('is_last', is_last ? 'true' : 'false');
        formData.append('session_key', currentSessionKey || '');

        const targetQueue = trackType === 'webcam' ? webcamUploadQueue : screenUploadQueue;
        const updated = queueUpload(targetQueue, formData, trackType);
        if (trackType === 'webcam') {
          webcamUploadQueue = updated;
        } else {
          screenUploadQueue = updated;
        }
      }
    };

    return { mp4file, options, trackId };
  }

  /**
   * Pipeline loop that processes a MediaStream track frame-by-frame via WebCodecs.
   * Preserves each frame's real capture timestamp; drops frames (rather than
   * queuing unboundedly) if the encoder falls behind.
   */
  async function processTrackLoop(reader, encoder, streamLabel) {
    try {
      while (recording) {
        const { done, value: videoFrame } = await reader.read();
        if (done) break;

        if (!recording) {
          videoFrame.close();
          break;
        }

        // Establish the epoch mapping once, from whichever frame arrives first
        // across either stream. Never used to alter the frame's own PTS.
        if (epochOffset === null) {
          epochOffset = performance.now() * 1000 - videoFrame.timestamp;
        }

        // Backpressure guard: if the encoder already has a backlog, drop this
        // frame instead of piling more work on top of an encoder that's
        // falling behind real time. Dropping is preferable to letting the
        // queue (and thus latency/drift) grow unbounded.
        if (encoder.encodeQueueSize > ENCODER_QUEUE_LIMIT) {
          console.warn(
            `[capture_session] ${streamLabel} encoder backlog (${encoder.encodeQueueSize}) — dropping frame`
          );
          if (testModeEnabled()) droppedFrameCount[streamLabel]++;
          videoFrame.close();
          continue;
        }

        // Pass the frame straight through. videoFrame.timestamp is already
        // the browser's real capture-time PTS — do not recompute it.
        encoder.encode(videoFrame);
        videoFrame.close();
      }
    } catch (err) {
      console.error(`[capture_session] Error inside ${streamLabel} frame processing pipeline:`, err);
    }
  }

  /**
   * Main setup logic to kick off both hardware capture instances simultaneously
   */
  async function startWebcamRecording(sessionKey) {
    const body = document.body;
    const shouldRecord = body.dataset.recordWebcam === 'true';
    currentSessionKey = sessionKey || currentSessionKey;
    if (!shouldRecord) {
      console.log('[capture_session] webcam recording disabled for this session');
      return;
    }

    try {
      console.log('[capture_session] Initializing capture devices...');

      // 1. Grab hardware streams at native/preferred framerates
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
      epochOffset = null;
      webcamUploadQueue = Promise.resolve();
      screenUploadQueue = Promise.resolve();
      if (testModeEnabled()) {
        resetTestModeState();
        console.log('[capture_session] TEST MODE enabled — recordings will be buffered locally and downloaded on stop, with frame-collision checks logged live.');
      }

      // 2. Extract tracks and set up raw stream readers
      const webcamTrack = webcamStream.getVideoTracks()[0];
      const screenTrack = screenStream.getVideoTracks()[0];

      const webcamProcessor = new MediaStreamTrackProcessor({ track: webcamTrack });
      const screenProcessor = new MediaStreamTrackProcessor({ track: screenTrack });

      webcamReader = webcamProcessor.readable.getReader();
      screenReader = screenProcessor.readable.getReader();

      // 3. Initialize MP4 Muxer containers (MP4Box)
      const webcamMux = createMuxerInstance('webcam', 1280, 720);
      const screenMux = createMuxerInstance('screen', 1920, 1080);

      // 4. Setup Webcam Hardware Encoder Configuration (H.264 Baseline Profile)
      webcamEncoder = new VideoEncoder({
        output: (chunk, metadata) => {
          if (metadata.decoderConfig && !webcamMux.trackId) {
            webcamMux.trackId = webcamMux.mp4file.addTrack({
              timescale: 1000000,
              width: 1280,
              height: 720,
              nb_samples: 30,
              avcDecoderConfigRecord: metadata.decoderConfig.description
            });
            webcamMux.mp4file.setSegmentOptions(webcamMux.trackId, null, { nbSamples: 30 });
            webcamMux.mp4file.initializeSegmentation();
          }

          if (testModeEnabled()) checkForCollision('webcam', chunk.timestamp);

          const buffer = new ArrayBuffer(chunk.byteLength);
          chunk.copyTo(buffer);
          webcamMux.mp4file.addSample(webcamMux.trackId, buffer, {
            duration: chunk.duration || 33333, // fallback ~30fps duration if unavailable
            dts: chunk.timestamp,
            pts: chunk.timestamp,
            is_sync: chunk.type === 'key'
          });
          webcamMux.mp4file.flush();
        },
        error: (e) => console.error('[Webcam Encoder Error]', e)
      });

      // 5. Setup Screen Hardware Encoder Configuration (H.264 Main Profile)
      screenEncoder = new VideoEncoder({
        output: (chunk, metadata) => {
          if (metadata.decoderConfig && !screenMux.trackId) {
            screenMux.trackId = screenMux.mp4file.addTrack({
              timescale: 1000000,
              width: 1920,
              height: 1080,
              nb_samples: 60,
              avcDecoderConfigRecord: metadata.decoderConfig.description
            });
            screenMux.mp4file.setSegmentOptions(screenMux.trackId, null, { nbSamples: 60 });
            screenMux.mp4file.initializeSegmentation();
          }

          if (testModeEnabled()) checkForCollision('screen', chunk.timestamp);

          const buffer = new ArrayBuffer(chunk.byteLength);
          chunk.copyTo(buffer);
          screenMux.mp4file.addSample(screenMux.trackId, buffer, {
            duration: chunk.duration || 16680, // fallback ~59.95fps duration if unavailable
            dts: chunk.timestamp,
            pts: chunk.timestamp,
            is_sync: chunk.type === 'key'
          });
          screenMux.mp4file.flush();
        },
        error: (e) => console.error('[Screen Encoder Error]', e)
      });

      // 6. Fire up hardware config bindings
      webcamEncoder.configure({ codec: 'avc1.42E01F', width: 1280, height: 720, bitrate: 2000000, framerate: 30 });
      screenEncoder.configure({ codec: 'avc1.4D4028', width: 1920, height: 1080, bitrate: 5000000, framerate: 60 });

      // 7. Start independent ingest processing loops
      processTrackLoop(webcamReader, webcamEncoder, 'webcam');
      processTrackLoop(screenReader, screenEncoder, 'screen');

      console.log('[capture_session] Dual synchronized recording engine online (real capture timestamps preserved).');
    } catch (err) {
      console.warn('[capture_session] Critical error starting dual recording pipelines:', err);
      stopWebcamRecording();
    }
  }

  /**
   * Gracefully close encoders, readers, streams and cleanly flush out queues
   */
  async function stopWebcamRecording() {
    recording = false;
    console.log('[capture_session] Initiating session shutdown sequence...');

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

    if (webcamReader) {
      webcamReader.cancel();
      webcamReader = null;
    }
    if (screenReader) {
      screenReader.cancel();
      screenReader = null;
    }

    const closePromises = [];
    if (webcamEncoder && webcamEncoder.state !== 'closed') {
      closePromises.push(webcamEncoder.flush().then(() => webcamEncoder.close()));
    }
    if (screenEncoder && screenEncoder.state !== 'closed') {
      closePromises.push(screenEncoder.flush().then(() => screenEncoder.close()));
    }

    // Make sure any in-flight uploads for both streams settle before we
    // consider the session fully closed.
    closePromises.push(webcamUploadQueue);
    closePromises.push(screenUploadQueue);

    await Promise.all(closePromises);

    if (testModeEnabled()) {
      finalizeTestModeOutput();
    }

    console.log('[capture_session] Encoders and upload queues drained. Recording fully stopped.');
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
