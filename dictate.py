#!/usr/bin/env python3
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

import gi

from settings import MODELS, Settings

if Settings.load().keep_on_top:
    os.environ.setdefault("GDK_BACKEND", "x11,wayland")

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkX11", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GdkX11, Gio, GLib, Graphene, Gsk, Gtk

from recorder import CHUNK, RATE, Recorder, list_microphones
from textfix import cleanup, polish
from transcriber import Transcriber, download, model_ready

APP_ID = "org.michalbenes.Dictate"
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEYBINDINGS = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
LEVEL_INTERVAL_US = CHUNK * 1_000_000 // (2 * RATE)
CSS = "@keyframes pulse { 50% { opacity: 0.25; } } .pulse { animation: pulse 1.6s ease-in-out infinite; }"
X11_CLIENT_MESSAGE = 33
X11_SUBSTRUCTURE_MASK = 3 << 19
NET_WM_STATE_ADD = 1


class XClientMessage(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
        ("pad", ctypes.c_long * 12),
    ]


def keep_above(surface: Gdk.Surface) -> None:
    if not isinstance(surface, GdkX11.X11Surface):
        return
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDefaultRootWindow.argtypes = x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
    display = x11.XOpenDisplay(None)
    above = x11.XInternAtom(display, b"_NET_WM_STATE_ABOVE", False)
    event = XClientMessage(type=X11_CLIENT_MESSAGE, window=surface.get_xid(), message_type=x11.XInternAtom(display, b"_NET_WM_STATE", False), format=32)
    event.data[:2] = [NET_WM_STATE_ADD, above]
    x11.XSendEvent(display, x11.XDefaultRootWindow(display), False, X11_SUBSTRUCTURE_MASK, ctypes.byref(event))
    x11.XCloseDisplay(display)


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


class Waveform(Gtk.Widget):
    BAR = 4
    STEP = 7
    HISTORY = 400
    DELAY_US = 200_000

    def __init__(self):
        super().__init__(height_request=72, hexpand=True)
        self._tick_id = 0
        self.clear()

    def clear(self) -> None:
        self.levels = deque([0.0] * self.HISTORY, maxlen=self.HISTORY)
        self.count = 0
        self.start = 0

    def push(self, level: float) -> None:
        if not self.count:
            self.start = GLib.get_monotonic_time()
        self.levels.append(level)
        self.count += 1

    def do_map(self) -> None:
        Gtk.Widget.do_map(self)
        self._tick_id = self.add_tick_callback(lambda *_: self.queue_draw() or True)

    def do_unmap(self) -> None:
        self.remove_tick_callback(self._tick_id)
        Gtk.Widget.do_unmap(self)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        elapsed = self.get_frame_clock().get_frame_time() - self.start if self.count else 0
        position = (elapsed - self.DELAY_US) / LEVEL_INTERVAL_US
        for i, level in enumerate(reversed(self.levels)):
            age = position - (self.count - 1 - i)
            if age < 0:
                continue
            x = width - self.BAR - age * self.STEP
            if x < -self.BAR:
                break
            bar = self.BAR + level * (height - self.BAR)
            rect = Gsk.RoundedRect()
            rect.init_from_rect(Graphene.Rect().init(x, (height - bar) / 2, self.BAR, bar), self.BAR / 2)
            color = Gdk.RGBA(red=accent.red, green=accent.green, blue=accent.blue, alpha=max(0, min(1, age, x / (width * 0.4))))
            snapshot.push_rounded_clip(rect)
            snapshot.append_color(color, rect.bounds)
            snapshot.pop()


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

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        finish_keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        finish_keys.connect("key-pressed", self._on_finish_key)
        self.add_controller(finish_keys)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("close-request", lambda *_: app.on_close())
        if app.settings.keep_on_top:
            self.connect("map", lambda *_: keep_above(self.get_surface()))

    def _recording_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER, margin_start=32, margin_end=32)
        timer_row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.timer = Gtk.Label(label="0:00", css_classes=["title-1", "numeric"])
        timer_row.append(Gtk.Image(icon_name="media-record-symbolic", pixel_size=16, css_classes=["error", "pulse"]))
        timer_row.append(self.timer)
        self.waveform = Waveform()
        hint = Gtk.Label(label=f"{shortcut_label(self.app.settings.shortcut)} or Enter to finish, Esc to cancel", css_classes=["dim-label"])
        self.status = Gtk.Label(css_classes=["dim-label", "caption"])
        for widget in (timer_row, self.waveform, hint, self.status):
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
        self.waveform.clear()
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

    def _on_finish_key(self, _controller, keyval: int, _keycode, _state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.app.state == "recording" and not self.get_visible_dialog():
            self.app.toggle()
            return True
        return False

    def _on_key(self, _controller, keyval: int, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.app.cancel()
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
        if self.state == "working" and self.settings.autohide:
            self.window.set_visible(False)
        else:
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
            self.window.waveform.push(level)
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
        s = self.settings
        self.state = "idle"
        self.window.show_result(text)
        if warning or not text:
            self.window.toast(warning or "No speech recognized")
        if text and (s.auto_copy or s.autohide) and self.copy_result(notify=s.notify) and (s.close_after_copy or (s.autohide and not warning)):
            self.window.close()
        else:
            self.window.set_visible(True)
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

        window = Adw.PreferencesGroup(title="Window")
        window.add(self._switch("Auto-hide", "Close as soon as recording stops, copy the text when it is ready", "autohide"))
        window.add(self._switch("Keep on top", "Stay above other windows (runs through XWayland, applies on next start)", "keep_on_top"))
        page.add(window)

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
