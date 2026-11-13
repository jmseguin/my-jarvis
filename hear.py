"""Hearing bay: local Whisper transcription on Apple Silicon or Windows/Linux.

Standard library + numpy + mlx_whisper (Apple Silicon) or faster_whisper
(Windows/Linux). Soft-imported by server.py so Jarvis boots fine without it.

Both the main ("large-v3-turbo") and wake ("small") models are loaded ONCE at
boot, kept in memory, and warmed with 1 second of silence so the first real
utterance has no cold-start delay.
"""
import io
import platform
import threading
import time
import wave

import numpy as np

SAMPLE_RATE = 16000


def trust_corporate_certs():
    """Make model downloads work behind a TLS-inspecting proxy.

    Python's HTTPS stack ships its own CA list, which lacks the company's inspection
    CA, hence "CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate".
    1. CA_BUNDLE in .env (a PEM file) is used for everything that honours SSL_CERT_FILE.
    2. Otherwise the operating system's trust store is used (pip install truststore).
    """
    import os
    import settings
    if settings.get("HF_TOKEN"):                      # optional: faster, rate-limit-free downloads
        os.environ["HF_TOKEN"] = settings.get("HF_TOKEN")
    bundle = settings.get("CA_BUNDLE")
    if bundle:
        path = str(settings.as_path(bundle))
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ[var] = path
        return f"CA_BUNDLE {path}"
    try:
        import truststore
        truststore.inject_into_ssl()
        return "operating-system certificate store"
    except ImportError:
        return "none (pip install truststore, or set CA_BUNDLE in .env)"

MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def _backend_name():
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mlx_whisper"
    return "faster_whisper"


def _wav_to_float32(wav_bytes):
    """Decode 16-bit PCM WAV bytes to mono 16 kHz float32 numpy array."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {sw * 8}-bit")
    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE and len(pcm) > 0:
        # Linear resample fallback if a non-16kHz WAV is sent
        n_out = int(round(len(pcm) * SAMPLE_RATE / sr))
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n_out), np.arange(len(pcm)), pcm).astype(np.float32)
    return pcm


class HearingBay:
    def __init__(self, cfg):
        self.large_name = cfg.get("model", "large-v3-turbo")
        self.wake_name = cfg.get("wake_model", "small")
        self.backend = _backend_name()
        self.ready = False
        self.last_error = ""
        self._lock = threading.Lock()
        self._models = {}       # "large" | "small" -> loaded model object
        self._repos = {}        # "large" | "small" -> resolved repo/size string

    def _resolve(self, name):
        if self.backend == "mlx_whisper":
            return MLX_REPOS.get(name, name)
        return name

    def load_and_warm(self):
        t0 = time.time()
        print(f"  hearing: trusting {trust_corporate_certs()}")
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)   # 1 second of silence
        if self.backend == "mlx_whisper":
            import mlx.core as mx
            import mlx_whisper
            from mlx_whisper.transcribe import ModelHolder, load_model

            self._mlx_whisper = mlx_whisper
            self._ModelHolder = ModelHolder
            self._mx = mx
            for key, name in (("small", self.wake_name), ("large", self.large_name)):
                repo = self._resolve(name)
                self._repos[key] = repo
                self._models[key] = load_model(repo, dtype=mx.float16)
                with self._lock:
                    ModelHolder.model = self._models[key]
                    ModelHolder.model_path = repo
                    mlx_whisper.transcribe(silence, path_or_hf_repo=repo,
                                           language="en", fp16=True, verbose=False)
        else:
            from faster_whisper import WhisperModel
            device = "cuda" if self._has_cuda() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
            for key, name in (("small", self.wake_name), ("large", self.large_name)):
                repo = self._resolve(name)
                self._repos[key] = repo
                if repo in self._repos.values() and any(
                        self._repos.get(k) == repo and k in self._models for k in self._models):
                    self._models[key] = next(self._models[k] for k in self._models if self._repos.get(k) == repo)
                    continue    # same model for both slots: load it once
                try:    # already downloaded? then never touch the network
                    m = WhisperModel(repo, device=device, compute_type=compute, local_files_only=True)
                except Exception:  # noqa: BLE001 - not cached yet: download once
                    print(f"  hearing: downloading {repo} (first run only)")
                    m = WhisperModel(repo, device=device, compute_type=compute)
                self._models[key] = m
                list(m.transcribe(silence, language="en", vad_filter=False)[0])

        self.ready = True
        print(f"  hearing: {self.backend} ready ({self.wake_name} + {self.large_name}) in {time.time() - t0:.1f}s")

    @staticmethod
    def _has_cuda():
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    def start(self):
        def boot():
            try:
                self.load_and_warm()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                print(f"  hearing: disabled ({e!r})")

        threading.Thread(target=boot, daemon=True, name="hearing-boot").start()

    def transcribe(self, wav_bytes, which="large"):
        """Transcribe WAV bytes with 'large' or 'small' model. Returns (text, seconds)."""
        if not self.ready:
            raise RuntimeError(self.last_error or "hearing models are still loading")
        key = "small" if which in ("small", "wake") else "large"
        audio = _wav_to_float32(wav_bytes)
        t0 = time.time()
        with self._lock:
            if self.backend == "mlx_whisper":
                repo = self._repos[key]
                self._ModelHolder.model = self._models[key]
                self._ModelHolder.model_path = repo
                res = self._mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=repo,
                    language="en",
                    fp16=True,
                    condition_on_previous_text=False,
                    verbose=False,
                )
                text = (res.get("text") or "").strip()
            else:
                segments, _ = self._models[key].transcribe(
                    audio, language="en", condition_on_previous_text=False, vad_filter=False
                )
                text = " ".join(s.text.strip() for s in segments).strip()
        return text, round(time.time() - t0, 3)
