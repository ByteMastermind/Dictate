import re
import subprocess

from settings import Settings

_FILLER = re.compile(r"(,?)\s*\b(?:uh+|um+|uhm+|erm?|hmm+|mm+|mhm+)\b,?(?=[\s.,;:!?]|$)", re.I)
_REPEAT = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.I)
_KEEP_COMMA_BEFORE = {"but", "and", "so", "or", "because", "which", "then", "however", "although", "while", "if", "when", "where", "who", "that"}


def _drop_filler(match: re.Match) -> str:
    following = re.match(r"\s*([A-Za-z']+)", match.string[match.end() :])
    if match.group(1) and following and following.group(1).lower() in _KEEP_COMMA_BEFORE:
        return ","
    return ""


def _sentences(text: str) -> str:
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"[,;:]+(?=[.,;:!?])", "", text)
    text = re.sub(r"([.!?])[.!?]+", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    if text and text[-1].isalnum():
        text += "."
    return text


def cleanup(text: str, settings: Settings) -> str:
    if settings.remove_fillers:
        text = _FILLER.sub(_drop_filler, text)
    if settings.collapse_repeats:
        text = _REPEAT.sub(r"\1", text)
    return _sentences(text)


def polish(text: str, command: str) -> str:
    result = subprocess.run(command, shell=True, input=text, capture_output=True, text=True, timeout=120)
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        raise RuntimeError(result.stderr.strip() or "no output")
    return output
