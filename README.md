# Dictate

Press **Super+R**, speak, press **Super+R** again. The speech is transcribed on the CPU with NVIDIA Parakeet TDT 0.6B (sherpa-onnx runtime, no cloud), lightly cleaned up (filler words, stutters, sentence punctuation) and copied to the clipboard.

## Install

```bash
./install.sh
```

Needs `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `python3-venv` and `wl-clipboard` or `xclip` for the clipboard (Ubuntu 24.04+ has all but `wl-clipboard`). The script creates `env/`, downloads the model (~630 MB) to `~/.local/share/dictate/models`, installs `~/.local/bin/dictate`, a desktop entry and the GNOME shortcut.

## Use

- **Super+R** (or Enter in the window) starts and stops recording, **Esc** cancels.
- The text appears in an editable window and is copied to the clipboard automatically. The copy button copies the edited text, the microphone button records again.
- The window stays above other windows. GNOME on Wayland gives apps no way to do that, so Dictate runs through XWayland while **Keep on top** is enabled.
- With **Auto-hide** the window closes as soon as recording stops, the text is copied once it is transcribed. The window comes back only when something fails.
- The gear opens settings: automatic copy, notification, close after copying, auto-hide, keep on top, model (English v2 or multilingual v3), microphone, filler removal, repeated-word collapsing, polish command, shortcut.

## Polish command

Optional shell command that receives the transcript on stdin and prints the edited text, for example with Claude Code:

```
claude -p --model haiku "Fix grammar and remove false starts in this dictated text. Keep the meaning and wording, output only the corrected text."
```

## Files

- `~/.config/dictate/config.json` settings
- `~/.local/share/dictate/models/` models
