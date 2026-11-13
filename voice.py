"""Voice bay: Jarvis speaks through a local VoiceStudio engine.

Standard library only. server.py soft-imports this module, so Jarvis boots
fine without it (the viewer then falls back to the browser's Web Speech API).

VoiceStudio API used:
  GET  /health                       liveness (must answer within 1 s)
  GET  /profiles                     find the cloned voice's id by name
  POST /engines/select               pin {"family","backend_id","model_id"}
  POST /v1/audio/speech              OpenAI-style TTS -> WAV bytes
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

MAX_CHARS = 600            # hard cap for one TTS request
HEALTH_TIMEOUT = 1.0       # seconds; slower than this = use the browser voice
SPEECH_TIMEOUT = 180
WARM_IDLE_SECONDS = 600    # every 10 idle minutes...
WARM_LINE = "Standing by." # ...render this and throw it away


class VoiceUnavailable(Exception):
    """Local voice cannot be used right now; caller should use the browser voice."""


def cap_text(text, limit=MAX_CHARS):
    """Trim to `limit` chars, preferring a sentence end, then a word break."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = list(re.finditer(r"[.!?…](?=\s|$)", cut))
    if m and m[-1].end() > limit * 0.5:
        return cut[:m[-1].end()]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > limit * 0.5 else cut


class VoiceBay:
    def __init__(self, cfg, cache_dir):
        self.url = cfg.get("url", "http://127.0.0.1:3900").rstrip("/")
        self.engine = cfg["engine"]
        self.model_id = cfg["model_id"]
        self.profile = cfg.get("profile", "Jarvis")
        self.language = cfg.get("language", "en")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._profile_id = None
        self._pinned_run = None      # identity of the engine run we pinned
        self._pin_lock = threading.Lock()
        self._engine_lock = threading.Lock()   # one render at a time
        self._health = (0.0, False, None)      # (checked_at, ok, run_id)
        self.last_activity = time.time()
        self.last_error = ""

    # ------------------------------------------------------------ http
    def _req(self, method, path, body=None, timeout=10.0):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")

    # ------------------------------------------------------------ health
    def healthy(self):
        """True if GET /health answers within 1 s. Cached briefly so a burst
        of chunk requests doesn't pay the probe each time."""
        checked, ok, _ = self._health
        ttl = 5.0 if ok else 3.0
        if time.time() - checked < ttl:
            return ok
        run_id = None
        try:
            raw, _ = self._req("GET", "/health", timeout=HEALTH_TIMEOUT)
            ok = True
            try:
                info = json.loads(raw or b"{}")
                if isinstance(info, dict):
                    for k in ("run_id", "boot_id", "started_at", "start_time", "pid"):
                        if info.get(k) is not None:
                            run_id = f"{k}={info[k]}"
                            break
            except ValueError:
                pass
        except Exception as e:                       # noqa: BLE001 - never crash
            ok = False
            self.last_error = f"health: {e}"
            self._pinned_run = None                   # engine gone: re-pin next run
        self._health = (time.time(), ok, run_id)
        return ok

    # ------------------------------------------------------------ pin engine
    def ensure_pinned(self, force=False):
        """POST /engines/select once per engine run, so VoiceStudio never
        silently swaps our engine for its default."""
        if not self.healthy():
            raise VoiceUnavailable(self.last_error or "VoiceStudio not answering")
        run = self._health[2] or "run"
        with self._pin_lock:
            if not force and self._pinned_run == run:
                return
            try:
                self._req("POST", "/engines/select",
                          {"family": "tts", "backend_id": self.engine,
                           "model_id": self.model_id}, timeout=120)
            except Exception as e:                   # noqa: BLE001
                self.last_error = f"engines/select: {e}"
                raise VoiceUnavailable(self.last_error)
            self._pinned_run = run
            self._profile_id = None                  # re-resolve after a new run
            print(f"  voice: pinned {self.engine} / {self.model_id}")

    def profile_id(self):
        if self._profile_id:
            return self._profile_id
        try:
            raw, _ = self._req("GET", "/profiles", timeout=5)
            data = json.loads(raw)
        except Exception as e:                       # noqa: BLE001
            self.last_error = f"profiles: {e}"
            raise VoiceUnavailable(self.last_error)
        items = data
        if isinstance(data, dict):
            items = data.get("profiles") or data.get("data") or data.get("items") or []
        want = self.profile.strip().lower()
        for p in items if isinstance(items, list) else []:
            if isinstance(p, dict) and str(p.get("name", "")).strip().lower() == want:
                pid = p.get("id") or p.get("profile_id") or p.get("uuid")
                if pid is not None:
                    self._profile_id = str(pid)
                    return self._profile_id
        self.last_error = f'profile "{self.profile}" not found in VoiceStudio'
        raise VoiceUnavailable(self.last_error)

    # ------------------------------------------------------------ speech
    def _render(self, text, pid):
        body = {"model": self.engine, "input": text, "voice": pid,
                "response_format": "wav", "language": self.language}
        with self._engine_lock:
            try:
                audio, ctype = self._req("POST", "/v1/audio/speech", body,
                                         timeout=SPEECH_TIMEOUT)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                self.last_error = f"speech HTTP {e.code}: {detail}"
                self._health = (0.0, False, None)
                self._profile_id = None
                raise VoiceUnavailable(self.last_error)
            except Exception as e:                   # noqa: BLE001
                self.last_error = f"speech: {e}"
                self._health = (0.0, False, None)
                raise VoiceUnavailable(self.last_error)
        if not audio.startswith(b"RIFF"):
            self.last_error = f"speech returned non-WAV ({ctype}, {len(audio)} bytes)"
            raise VoiceUnavailable(self.last_error)
        return audio

    def speak(self, text):
        """Return (wav_bytes, cache_hit). Raises VoiceUnavailable."""
        text = cap_text(text)
        if not text:
            raise VoiceUnavailable("empty text")
        self.last_activity = time.time()
        self.ensure_pinned()
        pid = self.profile_id()
        key = hashlib.sha256(f"{pid}\n{text}".encode("utf-8")).hexdigest()[:32]
        path = self.cache_dir / f"{key}.wav"
        if path.exists() and path.stat().st_size > 44:
            return path.read_bytes(), True
        audio = self._render(text, pid)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(audio)
        tmp.replace(path)
        self.last_activity = time.time()
        return audio, False

    # ------------------------------------------------------------ warmth
    def warm(self):
        """Render the warm line straight to the engine (never cached) and discard."""
        self.ensure_pinned()
        self._render(WARM_LINE, self.profile_id())
        self.last_activity = time.time()

    def start(self):
        """Pin at boot, warm once, then keep warm every 10 idle minutes."""
        def boot():
            try:
                self.ensure_pinned(force=True)
                self.profile_id()
                self.warm()
                print(f'  voice: local "{self.profile}" ready')
            except VoiceUnavailable as e:
                print(f"  voice: local voice unavailable ({e}); browser voice until it answers")

        def keeper():
            while True:
                time.sleep(30)
                if time.time() - self.last_activity < WARM_IDLE_SECONDS:
                    continue
                try:
                    self.warm()
                    print("  voice: kept warm")
                except VoiceUnavailable:
                    self.last_activity = time.time()   # try again in 10 min

        threading.Thread(target=boot, daemon=True, name="voice-boot").start()
        threading.Thread(target=keeper, daemon=True, name="voice-warm").start()
