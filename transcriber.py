import os
import shutil
import tarfile
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

from recorder import RATE
from settings import MODEL_URL, MODELS_DIR

FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
MAX_CHUNK = 60 * RATE
SEARCH = 5 * RATE


def model_ready(model_dir: Path) -> bool:
    return all((model_dir / name).exists() for name in FILES)


def download(model_dir: Path, progress: Callable[[float], None] | None = None) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    archive = MODELS_DIR / f"{model_dir.name}.tar.bz2"
    with urllib.request.urlopen(MODEL_URL.format(name=model_dir.name)) as response, archive.open("wb") as out:
        total = int(response.headers.get("Content-Length", 0))
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    with tarfile.open(archive) as tar:
        tar.extractall(MODELS_DIR, filter="data")
    archive.unlink()
    shutil.rmtree(model_dir / "test_wavs", ignore_errors=True)


def _split(samples: np.ndarray) -> list[np.ndarray]:
    pieces = []
    while len(samples) > MAX_CHUNK + SEARCH:
        window = np.abs(samples[MAX_CHUNK - SEARCH : MAX_CHUNK + SEARCH])
        smooth = np.convolve(window, np.ones(1600) / 1600, "same")
        cut = MAX_CHUNK - SEARCH + int(np.argmin(smooth))
        pieces.append(samples[:cut])
        samples = samples[cut:]
    pieces.append(samples)
    return pieces


class Transcriber:
    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self.status = "Loading model…"
        self._recognizer = None
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._load, daemon=True)
        self._thread.start()

    @property
    def ready(self) -> bool:
        return self._recognizer is not None

    def _load(self) -> None:
        try:
            if not model_ready(self.model_dir):
                download(self.model_dir, lambda f: setattr(self, "status", f"Downloading model {f:.0%}"))
                self.status = "Loading model…"
            import sherpa_onnx

            d = self.model_dir
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(d / FILES[0]),
                decoder=str(d / FILES[1]),
                joiner=str(d / FILES[2]),
                tokens=str(d / FILES[3]),
                num_threads=max(1, (os.cpu_count() or 2) // 2),
                model_type="nemo_transducer",
            )
            self.status = ""
        except Exception as e:
            self._error = e
            self.status = f"Model error: {e}"

    def transcribe(self, samples: np.ndarray) -> str:
        self._thread.join()
        if self._error:
            raise self._error
        texts = []
        for piece in _split(samples):
            stream = self._recognizer.create_stream()
            stream.accept_waveform(RATE, piece)
            self._recognizer.decode_stream(stream)
            texts.append(stream.result.text.strip())
        return " ".join(text for text in texts if text)
