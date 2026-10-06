import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "dictate" / "config.json"
MODELS_DIR = Path.home() / ".local" / "share" / "dictate" / "models"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{name}.tar.bz2"
MODELS = {
    "parakeet-tdt-0.6b-v2": "Parakeet TDT 0.6B v2 (English, most accurate)",
    "parakeet-tdt-0.6b-v3": "Parakeet TDT 0.6B v3 (25 European languages)",
}


@dataclass
class Settings:
    auto_copy: bool = True
    notify: bool = True
    close_after_copy: bool = False
    model: str = "parakeet-tdt-0.6b-v2"
    microphone: str = ""
    remove_fillers: bool = True
    collapse_repeats: bool = True
    polish_command: str = ""
    shortcut: str = "<Super>r"

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            data = {}
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @property
    def model_dir(self) -> Path:
        return MODELS_DIR / f"sherpa-onnx-nemo-{self.model}-int8"
