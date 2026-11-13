// Jarvis ears: local microphone capture -> 16 kHz mono 16-bit WAV -> POST /hear.
// No browser SpeechRecognition anywhere (no audio ever leaves this machine).
const SILENCE_MS = 700;
const FINISH_MS = 900;
const MIN_UTTERANCE_MS = 300;
const TARGET_RATE = 16000;
const PREROLL_MS = 250;
const BASE_THRESHOLD = 0.018;
const SPEAKING_THRESHOLD_INIT = 0.065;

(() => {
  const WAKE_RE = /^(?:hey[\s,]+)?jarvis\b[\s,.:!?-]*/i;
  const INTERRUPT_RE = /^(?:(?:hey[\s,]+)?jarvis[\s,]+)?(stop|wait)\b[\s.!?,]*$/i;

  const state = {
    handsFree: false,
    pttActive: false,
    awaitingCommand: false,
    speakingThreshold: SPEAKING_THRESHOLD_INIT,
    headphoneWarned: false,
    status: "idle",
    lastHeard: "",
    lastSpeechEndMs: null,
    lastTranscriptSeconds: null,
  };

  let onStatus = () => {};
  let onDispatch = () => {};
  let onHeadphoneHint = () => {};

  // Audio capture state
  let stream = null, ctx = null, source = null, proc = null, inputRate = 48000;
  let preroll = [], prerollSamples = 0;
  let recording = false, recChunks = [], recSamples = 0, speechStartMs = 0, lastLoudMs = 0;
  let pttChunks = [], pttSamples = 0;

  // Finish window state
  let finishBuf = "";
  let finishTimer = null;

  function setStatus(kind, detail = "") {
    state.status = kind;
    onStatus(kind, detail, state);
  }

  // ------------------------------------------------------------ 16 kHz mono 16-bit WAV
  function encodeWav(floatChunks, srcRate) {
    let total = 0;
    for (const c of floatChunks) total += c.length;
    const mono = new Float32Array(total);
    let off = 0;
    for (const c of floatChunks) { mono.set(c, off); off += c.length; }

    let pcm16;
    if (srcRate === TARGET_RATE) {
      pcm16 = new Int16Array(mono.length);
      for (let i = 0; i < mono.length; i++) {
        const s = Math.max(-1, Math.min(1, mono[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
    } else {
      const ratio = srcRate / TARGET_RATE;
      const outLen = Math.max(1, Math.floor(mono.length / ratio));
      pcm16 = new Int16Array(outLen);
      for (let i = 0; i < outLen; i++) {
        const pos = i * ratio;
        const idx = Math.floor(pos);
        const frac = pos - idx;
        const a = mono[idx] || 0;
        const b = mono[Math.min(idx + 1, mono.length - 1)] || a;
        const s = Math.max(-1, Math.min(1, a + (b - a) * frac));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
    }

    const buf = new ArrayBuffer(44 + pcm16.length * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF");
    v.setUint32(4, 36 + pcm16.length * 2, true);
    str(8, "WAVE");
    str(12, "fmt ");
    v.setUint32(16, 16, true);           // PCM chunk size
    v.setUint16(20, 1, true);            // PCM format
    v.setUint16(22, 1, true);            // mono
    v.setUint32(24, TARGET_RATE, true);  // 16000 Hz
    v.setUint32(28, TARGET_RATE * 2, true);
    v.setUint16(32, 2, true);            // block align
    v.setUint16(34, 16, true);           // 16-bit
    str(36, "data");
    v.setUint32(40, pcm16.length * 2, true);
    new Int16Array(buf, 44).set(pcm16);
    return new Blob([buf], { type: "audio/wav" });
  }

  // ------------------------------------------------------------ /hear request
  async function transcribe(wavBlob, model = "large") {
    const url = model === "small" ? "/hear?model=small" : "/hear";
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wavBlob,
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out.error || `HTTP ${res.status}`);
    return out; // { text, seconds }
  }

  // ------------------------------------------------------------ finish window
  function holdFinishTimer() {
    if (finishTimer) { clearTimeout(finishTimer); finishTimer = null; }
  }

  function scheduleFinish() {
    holdFinishTimer();
    finishTimer = setTimeout(() => {
      finishTimer = null;
      const text = finishBuf.trim();
      finishBuf = "";
      if (!text) return;
      state.awaitingCommand = false;
      onDispatch(text, {
        secondsToTranscript: state.lastTranscriptSeconds,
      });
    }, FINISH_MS);
  }

  function pushToFinishWindow(piece, speechEndMs) {
    const clean = piece.trim();
    if (!clean) return;
    finishBuf = finishBuf ? `${finishBuf} ${clean}` : clean;
    state.lastHeard = finishBuf;
    if (speechEndMs) {
      state.lastTranscriptSeconds = +((performance.now() - speechEndMs) / 1000).toFixed(2);
    }
    setStatus("heard", finishBuf);
    scheduleFinish();
  }

  // ------------------------------------------------------------ utterance routing
  async function processUtterance(wavBlob, { forceLarge = false, speechEndMs = performance.now() } = {}) {
    try {
      // 1) Interrupt check while Jarvis is speaking
      if (window.JarvisVoice?.state.speaking) {
        const small = await transcribe(wavBlob, "small");
        const txt = (small.text || "").trim();
        if (txt && INTERRUPT_RE.test(txt)) {
          window.JarvisVoice.stop();
          state.lastHeard = txt;
          setStatus("heard", txt);
          setTimeout(() => { if (state.handsFree && !window.JarvisVoice?.state.speaking) setStatus("listening"); }, 800);
          return { action: "interrupted", text: txt };
        }
        // Speaker bleed triggered the raised threshold: raise it further and advise headphones.
        if (txt) {
          state.speakingThreshold = Math.min(0.2, state.speakingThreshold + 0.025);
          if (!state.headphoneWarned) {
            state.headphoneWarned = true;
            onHeadphoneHint("Using speakers? Headphones prevent Jarvis from hearing himself when interrupting.");
          }
        }
        return { action: "ignored-while-speaking" };
      }

      // 2) Push-to-talk, awaiting follow-up after "Yes, sir?", or mid-sentence finish window
      if (forceLarge || state.awaitingCommand || finishBuf) {
        const large = await transcribe(wavBlob, "large");
        const txt = (large.text || "").trim();
        if (!txt) {
          if (state.handsFree && !finishBuf) setStatus("listening");
          return { action: "empty" };
        }
        pushToFinishWindow(txt, speechEndMs);
        return { action: "buffered", text: txt };
      }

      // 3) Hands-free wake-word check via SMALL model first
      const small = await transcribe(wavBlob, "small");
      const smallText = (small.text || "").trim();
      if (!smallText || !WAKE_RE.test(smallText)) {
        // Discarded: never logged, never shown, never sent anywhere.
        if (state.handsFree) setStatus("listening");
        return { action: "discarded" };
      }

      // Wake word matched -> send same WAV to LARGE model
      const large = await transcribe(wavBlob, "large");
      const cmd = (large.text || "").trim().replace(WAKE_RE, "").trim();
      if (!cmd) {
        state.awaitingCommand = true;
        state.lastHeard = "Jarvis";
        setStatus("heard", "Jarvis");
        await window.JarvisVoice?.speak("Yes, sir?");
        if (state.handsFree) setStatus("listening", "awaiting command");
        return { action: "wake-only" };
      }

      pushToFinishWindow(cmd, speechEndMs);
      return { action: "wake-command", text: cmd };
    } catch (err) {
      setStatus("error", err.message);
      return { action: "error", error: err.message };
    }
  }

  // ------------------------------------------------------------ mic & VAD
  async function ensureMic() {
    if (ctx && stream) {
      if (ctx.state === "suspended") await ctx.resume();
      return;
    }
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    inputRate = ctx.sampleRate;
    source = ctx.createMediaStreamSource(stream);
    proc = ctx.createScriptProcessor(2048, 1, 1);

    const maxPrerollSamples = Math.round((PREROLL_MS / 1000) * inputRate);
    proc.onaudioprocess = ev => {
      const input = ev.inputBuffer.getChannelData(0);
      const copy = new Float32Array(input);

      // Push-to-talk captures everything while held
      if (state.pttActive) {
        pttChunks.push(copy);
        pttSamples += copy.length;
        return;
      }
      if (!state.handsFree) return;

      // Maintain pre-roll buffer
      preroll.push(copy);
      prerollSamples += copy.length;
      while (prerollSamples > maxPrerollSamples && preroll.length > 1) {
        prerollSamples -= preroll.shift().length;
      }

      // Measure RMS loudness continuously
      let sum = 0;
      for (let i = 0; i < copy.length; i++) sum += copy[i] * copy[i];
      const rms = Math.sqrt(sum / copy.length);
      const thresh = window.JarvisVoice?.state.speaking ? state.speakingThreshold : BASE_THRESHOLD;
      const tNow = performance.now();

      if (rms >= thresh) {
        if (!recording) {
          recording = true;
          recChunks = [...preroll];
          recSamples = prerollSamples;
          speechStartMs = tNow;
          holdFinishTimer(); // pause finish window while user continues speaking
        } else {
          recChunks.push(copy);
          recSamples += copy.length;
        }
        lastLoudMs = tNow;
      } else if (recording) {
        recChunks.push(copy);
        recSamples += copy.length;
        if (tNow - lastLoudMs >= SILENCE_MS) {
          const durMs = lastLoudMs - speechStartMs;
          const chunks = recChunks;
          recording = false;
          recChunks = [];
          recSamples = 0;
          if (durMs >= MIN_UTTERANCE_MS) {
            const wav = encodeWav(chunks, inputRate);
            processUtterance(wav, { speechEndMs: tNow });
          } else if (finishBuf) {
            scheduleFinish(); // resume finish timer if noise blip was < 300ms
          }
        }
      }
    };

    // ScriptProcessorNode needs to be connected to destination (muted gain) to fire
    const sink = ctx.createGain();
    sink.gain.value = 0;
    source.connect(proc);
    proc.connect(sink);
    sink.connect(ctx.destination);
  }

  async function setHandsFree(on) {
    state.handsFree = !!on;
    if (state.handsFree) {
      await ensureMic();
      setStatus("listening");
    } else {
      recording = false;
      recChunks = [];
      state.awaitingCommand = false;
      holdFinishTimer();
      finishBuf = "";
      setStatus("idle");
    }
  }

  async function startPTT() {
    await ensureMic();
    holdFinishTimer();
    pttChunks = [];
    pttSamples = 0;
    state.pttActive = true;
    setStatus("listening", "push-to-talk");
  }

  async function stopPTT() {
    if (!state.pttActive) return;
    state.pttActive = false;
    const durMs = (pttSamples / inputRate) * 1000;
    const chunks = pttChunks;
    pttChunks = [];
    pttSamples = 0;
    if (durMs < MIN_UTTERANCE_MS) {
      setStatus(state.handsFree ? "listening" : "idle");
      return;
    }
    const wav = encodeWav(chunks, inputRate);
    await processUtterance(wav, { forceLarge: true, speechEndMs: performance.now() });
  }

  window.JarvisHear = {
    SILENCE_MS,
    FINISH_MS,
    MIN_UTTERANCE_MS,
    state,
    encodeWav,
    transcribe,
    processUtterance,
    setHandsFree,
    startPTT,
    stopPTT,
    configure(opts) {
      if (opts.onStatus) onStatus = opts.onStatus;
      if (opts.onDispatch) onDispatch = opts.onDispatch;
      if (opts.onHeadphoneHint) onHeadphoneHint = opts.onHeadphoneHint;
    },
  };
})();
