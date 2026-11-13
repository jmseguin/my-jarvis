// Jarvis voice player: sentence-packed chunks, gapless prefetch, browser fallback.
//
// THE LAW: chunks are packed from WHOLE sentences (~170 chars). A chunk is never
// cut inside a sentence, and the first chunk is never shortened to start sooner.
// THE MUTE FLAG: ?mute=1 in the page URL silences this tab. The check lives in
// playClip(), the ONE function that plays audio. Nothing else touches speakers.
(() => {
  const CHUNK_TARGET = 170;
  const MAX_REQUEST = 600;                 // matches the server cap
  const PREF_KEY = "jarvis.voice";        // "local" | "browser"

  // ------------------------------------------------------------ chunking
  function sentences(text) {
    const clean = String(text || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/[*_`#>]+/g, "")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/\s+/g, " ").trim();
    if (!clean) return [];
    // sentence = text up to . ! ? … (plus closing quotes/brackets) followed by space/end
    const out = clean.match(/[^.!?…]+(?:[.!?…]+["'”’)\]]*|$)(?=\s|$)|\S[^.!?…]*$/g) || [clean];
    return out.map(s => s.trim()).filter(Boolean);
  }

  // A single sentence longer than the server cap is split at clause breaks only
  // as a last resort (it could not be spoken at all otherwise).
  function capSentence(s) {
    if (s.length <= MAX_REQUEST) return [s];
    const parts = [], words = s.split(/(?<=[,;:—])\s+/);
    let cur = "";
    for (const w of words) {
      if (cur && (cur + " " + w).length > MAX_REQUEST) { parts.push(cur); cur = w; }
      else cur = cur ? cur + " " + w : w;
    }
    if (cur) parts.push(cur);
    return parts.flatMap(p => p.length <= MAX_REQUEST ? [p] : p.match(new RegExp(`.{1,${MAX_REQUEST}}(\\s|$)`, "g")));
  }

  function chunkText(text, target = CHUNK_TARGET) {
    const chunks = [];
    let cur = "";
    for (const s of sentences(text).flatMap(capSentence)) {
      if (!cur) cur = s;
      else if ((cur + " " + s).length <= target) cur += " " + s;
      else { chunks.push(cur); cur = s; }       // whole sentences only
    }
    if (cur) chunks.push(cur);
    return chunks;
  }

  // ------------------------------------------------------------ state
  const state = {
    pref: localStorage.getItem(PREF_KEY) || "local",
    gen: 0,                 // bumps on every new speak(): stops older playback
    audio: null,
    speaking: false,
    log: [],                // per-chunk timings for the latest speak() (verification)
    lastRun: null,
  };
  const now = () => performance.now();
  const setSpeaking = v => {
    if (state.speaking !== v) {
      state.speaking = v;
      window.dispatchEvent(new CustomEvent("jarvis:speaking", { detail: { speaking: v } }));
    }
  };

  // ------------------------------------------------------------ fetching
  async function fetchClip(text) {
    const t = { text, fetchStart: now() };
    if (state.pref === "browser") return { kind: "browser", text, voice: "browser", t, reason: "preference" };
    try {
      const res = await fetch("/tts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }) });
      const voice = res.headers.get("X-Jarvis-Voice") || "browser";
      window.dispatchEvent(new CustomEvent("jarvis:voice-header", { detail: { voice } }));
      const type = res.headers.get("Content-Type") || "";
      if (res.ok && type.startsWith("audio/")) {
        const blob = await res.blob();
        t.fetchDone = now();
        return { kind: "wav", blob, voice, cache: res.headers.get("X-Jarvis-Cache"), text, t };
      }
      const info = await res.json().catch(() => ({}));
      t.fetchDone = now();
      return { kind: "browser", text, voice: "browser", t, reason: info.error || `HTTP ${res.status}` };
    } catch (err) {
      t.fetchDone = now();
      window.dispatchEvent(new CustomEvent("jarvis:voice-header", { detail: { voice: "browser" } }));
      return { kind: "browser", text, voice: "browser", t, reason: String(err) };
    }
  }

  // ------------------------------------------------------------ THE ONE PLAYER
  // Every sound this tab makes goes through here. Returns when the clip ends.
  function playClip(clip, gen) {
    if (new URLSearchParams(location.search).get("mute") === "1") {
      clip.muted = true;
      return Promise.resolve("muted");
    }
    if (gen !== state.gen) return Promise.resolve("cancelled");
    return new Promise(resolve => {
      let done = false;
      const finish = why => { if (!done) { done = true; clip.t.playEnd = now(); resolve(why); } };
      const markStart = () => { if (!clip.t.playStart) { clip.t.playStart = now(); setSpeaking(true); } };
      if (clip.kind === "wav") {
        const url = URL.createObjectURL(clip.blob);
        const a = new Audio(url);
        state.audio = a;
        a.onplaying = markStart;
        a.onended = () => { URL.revokeObjectURL(url); finish("ended"); };
        a.onerror = () => { URL.revokeObjectURL(url); finish("error"); };
        a.onpause = () => { if (gen !== state.gen) finish("cancelled"); };
        a.play().catch(err => {                    // autoplay blocked etc: never silent
          console.warn("[voice] audio play failed, using browser voice:", err);
          URL.revokeObjectURL(url);
          playClip({ ...clip, kind: "browser" }, gen).then(finish);
        });
      } else {
        if (!("speechSynthesis" in window)) { console.warn("[voice] no Web Speech API"); return finish("unsupported"); }
        const u = new SpeechSynthesisUtterance(clip.text);
        u.lang = "en-US"; u.rate = 1.02;
        u.onstart = markStart;
        u.onend = () => finish("ended");
        u.onerror = () => finish("error");
        // Some engines never fire onend: guard with an estimated duration.
        setTimeout(() => finish("timeout"), 4000 + clip.text.length * 90);
        speechSynthesis.speak(u);
      }
    });
  }

  function stop() {
    state.gen++;
    if (state.audio) { try { state.audio.pause(); } catch {} state.audio = null; }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    setSpeaking(false);
  }

  // ------------------------------------------------------------ pipeline
  // Fetch chunk 1 -> play it, and while it plays fetch chunk 2, and so on.
  async function speak(text) {
    stop();
    const gen = state.gen;
    const chunks = chunkText(text);
    const run = { text, chunks, t0: now(), clips: [] };
    state.lastRun = run;
    if (!chunks.length) return run;
    let next = fetchClip(chunks[0]);
    for (let i = 0; i < chunks.length; i++) {
      const clip = await next;
      if (gen !== state.gen) break;
      run.clips.push(clip);
      next = i + 1 < chunks.length ? fetchClip(chunks[i + 1]) : null;   // prefetch NOW
      if (clip.kind === "browser") console.info(`[voice] X-Jarvis-Voice: browser (${clip.reason})`);
      await playClip(clip, gen);
      if (gen !== state.gen) break;
    }
    if (gen === state.gen) setSpeaking(false);
    run.t1 = now();
    run.summary = summarise(run);
    window.dispatchEvent(new CustomEvent("jarvis:spoke", { detail: run.summary }));
    return run;
  }

  function summarise(run) {
    const c = run.clips;
    return {
      chunks: run.chunks.map(s => s.length),
      voices: c.map(x => x.voice),
      muted: c.some(x => x.muted),
      firstAudioSeconds: c[0]?.t.playStart ? +((c[0].t.playStart - run.t0) / 1000).toFixed(2) : null,
      totalSeconds: +((run.t1 - run.t0) / 1000).toFixed(2),
      // was chunk i+1 already fetched when chunk i finished playing?
      prefetchedInTime: c.slice(1).map((x, i) => (c[i].t.playEnd ?? Infinity) >= x.t.fetchDone),
      gapsMs: c.slice(1).map((x, i) => x.t.playStart && c[i].t.playEnd ? Math.round(x.t.playStart - c[i].t.playEnd) : null),
    };
  }

  // ------------------------------------------------------------ voice routes
  // Checked BEFORE a sentence goes to the brain. Later prompts push their own.
  const VOICE_ROUTES = [
    { name: "local-voice",
      pattern: /\b(use|switch to|back to)\s+(your\s+|the\s+)?(local|own|cloned)\s+voice\b/i,
      handler: () => { setPref("local"); return "Local voice on."; } },
    { name: "browser-voice",
      pattern: /\b(use|switch to)\s+(the\s+)?browser(\s+voice)?\b/i,
      handler: () => { setPref("browser"); return "Browser voice on."; } },
  ];
  function setPref(p) { state.pref = p; localStorage.setItem(PREF_KEY, p); }

  // Returns {route, reply} if a route handled the text, else null.
  async function routeVoice(text) {
    for (const r of VOICE_ROUTES) {
      const m = String(text).match(r.pattern);
      if (m) return { route: r.name, reply: await r.handler(m, text) };
    }
    return null;
  }

  window.JarvisVoice = { speak, stop, chunkText, routeVoice, VOICE_ROUTES, state,
    addRoute: (name, pattern, handler) => VOICE_ROUTES.push({ name, pattern, handler }) };
})();
