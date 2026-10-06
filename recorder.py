import shutil
import subprocess
import threading
from collections.abc import Callable

import numpy as np

RATE = 16000
CHUNK = RATE // 10 * 2


def list_microphones() -> list[str]:
    try:
        out = subprocess.run(["pactl", "list", "sources", "short"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = [line.split("\t")[1] for line in out.splitlines() if "\t" in line]
    return [name for name in names if not name.endswith(".monitor")]


def _command(device: str) -> list[str]:
    if shutil.which("pw-record"):
        target = ["--target", device] if device else []
        return ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--raw", *target, "-"]
    target = [f"--device={device}"] if device else []
    return ["parecord", "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le", *target]


class Recorder:
    def __init__(self, device: str = "", on_level: Callable[[float], None] | None = None):
        self._proc = subprocess.Popen(_command(device), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._chunks: list[bytes] = []
        self._on_level = on_level
        self.error = ""
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while chunk := self._proc.stdout.read(CHUNK):
            self._chunks.append(chunk)
            if self._on_level:
                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768
                db = 20 * np.log10(np.sqrt(np.mean(samples**2)) + 1e-6)
                self._on_level(max(0.0, min(1.0, 1 + db / 50)))

    @property
    def seconds(self) -> float:
        return sum(map(len, self._chunks)) / (2 * RATE)

    def stop(self) -> np.ndarray:
        self._proc.terminate()
        self._thread.join(timeout=3)
        self._proc.wait(timeout=3)
        if not self._chunks:
            self.error = self._proc.stderr.read().decode(errors="replace").strip() or "Microphone capture failed"
        return np.frombuffer(b"".join(self._chunks), dtype=np.int16).astype(np.float32) / 32768
