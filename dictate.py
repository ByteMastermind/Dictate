#!/usr/bin/env python3
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from recorder import RATE, Recorder, list_microphones
from settings import MODELS, Settings
from textfix import cleanup, polish
from transcriber import Transcriber, download, model_ready

APP_ID = "org.michalbenes.Dictate"
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEYBINDINGS = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def launcher_command() -> str:
    launcher = Path.home() / ".local" / "bin" / "dictate"
    return str(launcher) if launcher.exists() else f"{sys.executable} {Path(__file__).resolve()}"


def register_shortcut(binding: str, command: str) -> None:
    media = Gio.Settings.new(MEDIA_KEYS)
    paths = list(media.get_strv("custom-keybindings"))

    def entry(path: str) -> Gio.Settings:
        return Gio.Settings.new_with_path(f"{MEDIA_KEYS}.custom-keybinding", path)

    path = next((p for p in paths if entry(p).get_string("name") == "Dictate"), None)
    if path is None:
        n = 0
        while f"{KEYBINDINGS}custom{n}/" in paths:
            n += 1
        path = f"{KEYBINDINGS}custom{n}/"
        media.set_strv("custom-keybindings", [*paths, path])
    keybinding = entry(path)
    keybinding.set_string("name", "Dictate")
    keybinding.set_string("command", command)
    keybinding.set_string("binding", binding)
    Gio.Settings.sync()


def shortcut_label(binding: str) -> str:
    return "+".join((mod or key).capitalize() for mod, key in re.findall(r"<(\w+)>|(\w+)", binding))


def copy_to_clipboard(text: str) -> None:
    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(command[0]):
            subprocess.run(command, input=text.encode(), check=True, timeout=5)
            return
    raise RuntimeError("install wl-clipboard or xclip")


class Window(Adw.ApplicationWindow):
    def __init__(self, app: "App"):
        super().__init__(application=app, default_width=560, default_height=360)
        self.app = app
        self.title = Adw.WindowTitle(title="Dictate")
        header = Adw.HeaderBar(title_widget=self.title)
        self.record_button = Gtk.Button(icon_name="audio-input-microphone-symbolic", tooltip_text=f"Record again ({shortcut_label(app.settings.shortcut)})")
        self.record_button.connect("clicked", lambda *_: app.toggle())
        self.copy_button = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy")
        self.copy_button.connect("clicked", lambda *_: app.copy_result())
        settings_button = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        settings_button.connect("clicked", lambda *_: app.open_settings())
        header.pack_start(self.record_button)
        header.pack_start(self.copy_button)
        header.pack_end(settings_button)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(self._recording_page(), "recording")
        self.stack.add_named(self._busy_page(), "busy")
        self.stack.add_named(self._result_page(), "result")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.stack)
        self.toasts = Adw.ToastOverlay(child=box)
        self.set_content(self.toasts)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("close-request", lambda *_: app.on_close())

    def _recording_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, margin_start=32, margin_end=32)
        self.timer = Gtk.Label(label="0:00", css_classes=["title-1", "numeric"])
        self.level = Gtk.LevelBar(min_value=0, max_value=1)
        hint = Gtk.Label(label=f"{shortcut_label(self.app.settings.shortcut)} or Enter to finish, Esc to cancel", css_classes=["dim-label"])
        self.status = Gtk.Label(css_classes=["dim-label", "caption"])
        for widget in (Gtk.Image(icon_name="media-record-symbolic", pixel_size=32, css_classes=["error"]), self.timer, self.level, hint, self.status):
            box.append(widget)
        return box

    def _busy_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, margin_start=32, margin_end=32)
        self.busy_label = Gtk.Label(css_classes=["title-3"])
        box.append(Adw.Spinner(width_request=48, height_request=48))
        box.append(self.busy_label)
        return box

    def _result_page(self) -> Gtk.Widget:
        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12, left_margin=16, right_margin=16)
        return Gtk.ScrolledWindow(child=self.view, vexpand=True)

    def show_recording(self) -> None:
        self.timer.set_label("0:00")
        self.level.set_value(0)
        self.title.set_subtitle("Recording")
        self.record_button.set_visible(False)
        self.copy_button.set_visible(False)
        self.stack.set_visible_child_name("recording")

    def show_busy(self, text: str) -> None:
        self.busy_label.set_label(text)
        self.title.set_subtitle(text)
        self.stack.set_visible_child_name("busy")

    def show_result(self, text: str | None = None) -> None:
        if text is not None:
            self.view.get_buffer().set_text(text)
        self.title.set_subtitle("")
        self.record_button.set_visible(True)
        self.copy_button.set_visible(True)
        self.stack.set_visible_child_name("result")
        self.view.grab_focus()

    def text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def _on_key(self, _controller, keyval: int, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.app.cancel()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.app.state == "recording":
            self.app.toggle()
            return True
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.settings = Settings.load()
        self.window: Window | None = None
        self.recorder: Recorder | None = None
        self.transcriber: Transcriber | None = None
        self.state = "idle"
        self.work_status = ""
        self._tick_id = 0

    def do_activate(self) -> None:
        self.toggle()

    def toggle(self) -> None:
        if self.state == "recording":
            self.finish()
        elif self.state == "idle":
            self.start()
        self.window.present()

    def start(self) -> None:
        self.recorder = Recorder(self.settings.microphone, lambda level: GLib.idle_add(self._set_level, level))
        if self.transcriber is None or self.transcriber.model_dir != self.settings.model_dir:
            self.transcriber = Transcriber(self.settings.model_dir)
        if self.window is None:
            self.window = Window(self)
        self.state = "recording"
        self.window.show_recording()
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(200, self._tick)

    def _set_level(self, level: float) -> bool:
        if self.state == "recording":
            self.window.level.set_value(level)
        return False

    def _tick(self) -> bool:
        if self.state == "recording":
            seconds = int(self.recorder.seconds)
            self.window.timer.set_label(f"{seconds // 60}:{seconds % 60:02d}")
            self.window.status.set_label(self.transcriber.status)
        elif self.state == "working":
            self.window.show_busy(self.transcriber.status or self.work_status)
        else:
            self._tick_id = 0
            return False
        return True

    def finish(self) -> None:
        samples = self.recorder.stop()
        error = self.recorder.error
        self.recorder = None
        if len(samples) < RATE // 2:
            self.state = "idle"
            self.window.show_result()
            self.window.toast(error or "Nothing recorded")
            return
        self.state = "working"
        self.work_status = "Transcribing…"
        self.window.show_busy(self.transcriber.status or self.work_status)
        threading.Thread(target=self._work, args=(samples,), daemon=True).start()

    def _work(self, samples) -> None:
        try:
            text = cleanup(self.transcriber.transcribe(samples), self.settings)
        except Exception as e:
            GLib.idle_add(self._done, "", f"Transcription failed: {e}")
            return
        warning = ""
        if text and self.settings.polish_command:
            self.work_status = "Polishing…"
            try:
                text = polish(text, self.settings.polish_command)
            except Exception as e:
                warning = f"Polish failed: {e}"
        GLib.idle_add(self._done, text, warning)

    def _done(self, text: str, warning: str) -> bool:
        self.state = "idle"
        self.window.show_result(text)
        if warning or not text:
            self.window.toast(warning or "No speech recognized")
        if text and self.settings.auto_copy and self.copy_result(notify=self.settings.notify) and self.settings.close_after_copy:
            self.window.close()
        return False

    def copy_result(self, notify: bool = False) -> bool:
        text = self.window.text()
        try:
            copy_to_clipboard(text)
        except Exception as e:
            self.window.toast(f"Copy failed: {e}")
            return False
        self.window.toast("Copied to clipboard")
        if notify:
            notification = Gio.Notification.new("Copied to clipboard")
            notification.set_body(text if len(text) <= 150 else text[:149] + "…")
            self.send_notification("copied", notification)
        return True

    def cancel(self) -> None:
        if self.state == "recording":
            self.recorder.stop()
            self.recorder = None
            self.state = "idle"
            if self.window.text():
                self.window.show_result()
                return
        if self.state == "idle":
            self.window.close()

    def on_close(self) -> bool:
        if self.recorder:
            self.recorder.stop()
        return False

    def open_settings(self) -> None:
        s = self.settings
        dialog = Adw.PreferencesDialog(title="Settings")
        page = Adw.PreferencesPage()
        dialog.add(page)

        clipboard = Adw.PreferencesGroup(title="Clipboard")
        clipboard.add(self._switch("Copy automatically", "Put the text into the clipboard when transcription finishes", "auto_copy"))
        clipboard.add(self._switch("Notify", "Show a notification when the text is copied automatically", "notify"))
        clipboard.add(self._switch("Close window after copying", "", "close_after_copy"))
        page.add(clipboard)

        transcription = Adw.PreferencesGroup(title="Transcription")
        model_row = Adw.ComboRow(title="Model", model=Gtk.StringList.new(list(MODELS.values())))
        model_row.set_selected(list(MODELS).index(s.model))
        model_row.connect("notify::selected", lambda row, _: self._set("model", list(MODELS)[row.get_selected()]))
        microphones = ["System default", *list_microphones()]
        microphone_row = Adw.ComboRow(title="Microphone", model=Gtk.StringList.new(microphones))
        microphone_row.set_selected(microphones.index(s.microphone) if s.microphone in microphones else 0)
        microphone_row.connect("notify::selected", lambda row, _: self._set("microphone", "" if row.get_selected() == 0 else microphones[row.get_selected()]))
        polish_row = Adw.EntryRow(title="Polish command (gets text on stdin, prints the result)", text=s.polish_command)
        polish_row.connect("changed", lambda row: self._set("polish_command", row.get_text().strip()))
        for row in (model_row, microphone_row, self._switch("Remove filler words", "uh, um, hmm", "remove_fillers"), self._switch("Collapse repeated words", "the the → the", "collapse_repeats"), polish_row):
            transcription.add(row)
        page.add(transcription)

        shortcut = Adw.PreferencesGroup(title="Shortcut")
        shortcut_row = Adw.EntryRow(title="GNOME keybinding", text=s.shortcut)
        apply_button = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        apply_button.connect("clicked", lambda *_: self._apply_shortcut(shortcut_row.get_text().strip()))
        shortcut_row.add_suffix(apply_button)
        shortcut.add(shortcut_row)
        page.add(shortcut)
        dialog.present(self.window)

    def _switch(self, title: str, subtitle: str, key: str) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=getattr(self.settings, key))
        row.connect("notify::active", lambda r, _: self._set(key, r.get_active()))
        return row

    def _set(self, key: str, value) -> None:
        setattr(self.settings, key, value)
        self.settings.save()

    def _apply_shortcut(self, binding: str) -> None:
        try:
            register_shortcut(binding, launcher_command())
        except Exception as e:
            self.window.toast(f"Shortcut failed: {e}")
            return
        self._set("shortcut", binding)
        self.window.toast(f"{shortcut_label(binding)} registered")


def main(argv: list[str]) -> int:
    settings = Settings.load()
    if argv[1:] == ["download"]:
        if not model_ready(settings.model_dir):
            download(settings.model_dir, lambda f: print(f"\r{f:.0%}", end="", flush=True))
        print(f"\rmodel {settings.model} ready")
        return 0
    if argv[1:] == ["shortcut"]:
        register_shortcut(settings.shortcut, launcher_command())
        print(f"{shortcut_label(settings.shortcut)} registered")
        return 0
    return App().run([argv[0]])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
