"""Flowra - local voice dictation for Windows.

Hold the hotkey and talk, release to type it.  Double-tap to lock hands-free; tap again
to finish.  Esc cancels.  Whisper runs on your GPU; nothing leaves the machine.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
# installed copies keep per-user data in %APPDATA%\Flowra; a dev checkout keeps it next to the code
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Flowra" if FROZEN else Path(__file__).parent
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.jsonl"
SOUND_DIR = APP_DIR / "sounds" if FROZEN else APP_DIR / "assets" / "sounds"
LOG_PATH = APP_DIR / "flowra.log"


def _add_cuda_dlls() -> None:
    import site
    try:
        roots = [Path(p) for p in site.getsitepackages()]
    except Exception:
        roots = []
    for root in roots + [HERE]:
        for bin_dir in (root / "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


_add_cuda_dlls()

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

DEFAULT_CONFIG = {
    # input
    "hotkey": "right ctrl",
    "paste_last_hotkey": "ctrl+alt+v",
    "microphone": "",                    # "" = system default
    # recognition
    "model": "auto",                     # auto = large-v3-turbo on NVIDIA GPUs, small on CPU
    "device": "auto",
    "compute_type": "auto",
    "language": "en",                    # "" = auto-detect
    "vocabulary": "",
    "boost_quiet_speech": True,
    # text
    "cleanup": True,
    "voice_commands": True,
    "replacements": {},
    # output
    "clipboard_fallback": True,          # no text field -> copy instead of paste
    "app_rules": {},                     # {"app.exe": "clipboard" | "paste" | "off"}
    # behaviour
    "auto_stop_silence": 0,              # seconds of silence that end a locked recording (0 = off)
    "double_tap_window": 0.35,
    "tap_max_seconds": 0.3,
    "min_seconds": 0.3,
    "unload_after_idle_minutes": 0,
    # indicator
    "indicator_position": "bottom-center",   # {top,middle,bottom}-{left,center,right}
    "indicator_dx": 0,                   # offset from the anchor, DIP
    "indicator_dy": 0,
    "indicator_monitor": "active",       # active | cursor | primary
    "indicator_scale": 1.0,
    "indicator_opacity": 1.0,
    "indicator_shadow": True,
    "indicator_theme": "graphite",       # graphite | light | midnight | glass | custom
    "indicator_surface": "#121214",      # custom theme colours
    "indicator_ink": "#F5F5F7",
    "indicator_accent": "#FF453A",
    "indicator_idle": "dash",            # dash | dot | hidden
    "indicator_idle_length": 30,
    "indicator_idle_thickness": 6,
    "indicator_idle_opacity": 0.55,
    # system
    "launch_at_startup": True,
    # sound
    "sounds": True,
    "sound_volume": 35,
}


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(f"[flowra] {msg}", flush=True)
    except Exception:
        pass
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".old.log"))
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- config
def read_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))  # tolerate BOM from editors
        if "show_indicator" in data:                       # migrate v1 key
            if not data.pop("show_indicator"):
                data.setdefault("indicator_idle", "hidden")
        if data.get("indicator_position") in ("bottom", "top"):
            data["indicator_position"] += "-center"
        if "indicator_offset_x" in data:                   # migrate v2 offsets
            data.setdefault("indicator_dx", data.pop("indicator_offset_x"))
            data.pop("indicator_offset_y", None)
        cfg.update(data)
    return cfg


def write_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- text
FILLERS = re.compile(r"\b(?:um+|uh+|erm+|ah+|hmm+|mm+)\b[,.]?\s*", re.IGNORECASE)
UNDO = re.compile(r"^\W*(scratch that|delete that|undo that)\W*$", re.IGNORECASE)
CASES = {
    "camel": lambda w: w[0].lower() + "".join(x.capitalize() for x in w[1:]),
    "pascal": lambda w: "".join(x.capitalize() for x in w),
    "snake": lambda w: "_".join(x.lower() for x in w),
    "kebab": lambda w: "-".join(x.lower() for x in w),
    "constant": lambda w: "_".join(x.upper() for x in w),
    "all caps": lambda w: " ".join(x.upper() for x in w),
}
CASE_RE = re.compile(r"\b(?:(camel|pascal|snake|kebab|constant) case|(all caps))\s+([A-Za-z0-9' ]+?)(?=[,.!?;:]|$)",
                     re.IGNORECASE)
SYMBOLS = (("comma", ","), ("full stop", "."), ("question mark", "?"),
           ("exclamation mark", "!"), ("colon", ":"), ("semicolon", ";"))


def tidy(text: str, cfg: dict) -> str:
    if cfg["cleanup"]:
        text = FILLERS.sub("", text)
        # self-correction: "meet Tuesday, no wait, Wednesday" -> "meet Wednesday"
        text = re.sub(r"\b[\w']+[,.]?\s+(?:no wait|no no|actually no)\b[,.]?\s*", "", text, flags=re.IGNORECASE)
    if cfg["voice_commands"]:
        text = re.sub(r"[,.]?\s*\bnew paragraph\b[,.]?\s*", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"[,.]?\s*\bnew line\b[,.]?\s*", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"[,.]?\s*\b(?:bullet point|new bullet)\b[,.]?\s*", "\n- ", text, flags=re.IGNORECASE)
        for word, sym in SYMBOLS:
            text = re.sub(rf"[,.]?\s*\b{word}\b", sym, text, flags=re.IGNORECASE)
        text = CASE_RE.sub(lambda m: "\x00" + CASES[(m.group(1) or m.group(2)).lower()](
            m.group(3).replace("'", "").split()), text)
    for spoken, written in (cfg.get("replacements") or {}).items():
        if spoken.strip():
            text = re.sub(rf"\b{re.escape(spoken)}\b", lambda _m: written, text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+([,.!?:;])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text).strip(" ")
    text = re.sub(r"(^|[.!?]\s+|\n(?:- )?)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text.replace("\x00", "")   # code-cased words are protected from auto-capitalisation


# --------------------------------------------------------------------------- engine
def has_cuda() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve_model(cfg: dict) -> tuple[str, str, str]:
    device = cfg.get("device", "auto")
    if device == "auto":
        device = "cuda" if has_cuda() else "cpu"
    model = cfg.get("model", "auto")
    if model == "auto":
        model = "large-v3-turbo" if device == "cuda" else "small"
    compute = cfg.get("compute_type", "auto")
    if compute == "auto":
        compute = "int8_float16" if device == "cuda" else "int8"
    return model, device, compute


class Transcriber:
    def __init__(self, cfg: dict):
        self.cfg, self.model, self.lock = cfg, None, threading.Lock()
        self.status, self.active = "", ""
        self.last_used = time.time()
        self.ready = threading.Event()

    def ensure_loaded(self):
        with self.lock:
            if self.model is None:
                self.ready.clear()
                from faster_whisper import WhisperModel
                t = time.time()
                model, device, compute = resolve_model(self.cfg)
                self.status = f"Preparing {model} model (first run downloads it)â€¦"
                try:
                    self.model = WhisperModel(model, device=device, compute_type=compute)
                except Exception as e:
                    if device != "cuda":
                        raise
                    log(f"GPU load failed ({e}); falling back to CPU")
                    model, device, compute = "small", "cpu", "int8"
                    self.model = WhisperModel(model, device=device, compute_type=compute)
                list(self.model.transcribe(np.zeros(16000, np.float32), beam_size=1)[0])  # warm-up
                self.active = f"{model} on {'GPU' if device == 'cuda' else 'CPU'}"
                self.status = ""
                log(f"model {self.active} ready in {time.time() - t:.1f}s")
            self.ready.set()
            return self.model

    def unload(self):
        with self.lock:
            self.model = None
            self.ready.clear()
            import gc; gc.collect()

    def unload_if_idle(self):
        mins = self.cfg["unload_after_idle_minutes"]
        if mins and self.model is not None and time.time() - self.last_used > mins * 60:
            self.unload()
            log("model unloaded (idle)")

    def transcribe(self, audio: np.ndarray) -> str:
        model = self.ensure_loaded()
        self.last_used = time.time()
        if self.cfg["boost_quiet_speech"]:
            peak = float(np.abs(audio).max()) if len(audio) else 0
            if 0.003 < peak < 0.35:                 # whispering / far from the mic
                audio = audio * min(12.0, 0.6 / peak)
        segments, _ = model.transcribe(
            audio, language=self.cfg["language"] or None, beam_size=1, vad_filter=True,
            initial_prompt=self.cfg["vocabulary"] or None, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()


def list_microphones() -> list[str]:
    try:
        default_api = sd.query_devices(kind="input")["hostapi"]
        return sorted({d["name"] for d in sd.query_devices()
                       if d["max_input_channels"] > 0 and d["hostapi"] == default_api})
    except Exception:
        return []


class Recorder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chunks: list[np.ndarray] = []
        self.stream = None
        self.level = 0.0
        self.last_voice = 0.0
        self.started = 0.0
        self.rate = 16000

    def _device(self):
        name = self.cfg.get("microphone") or ""
        if name:
            api = sd.query_devices(kind="input")["hostapi"]
            for i, d in enumerate(sd.query_devices()):
                if d["name"] == name and d["max_input_channels"] > 0 and d["hostapi"] == api:
                    return i
        return None

    def _cb(self, data, *_):
        self.chunks.append(data.copy())
        self.level = float(np.sqrt(np.mean(data ** 2)))
        if self.level > 0.012:
            self.last_voice = time.time()

    def start(self):
        self.chunks, self.level = [], 0.0
        self.started = self.last_voice = time.time()
        dev = self._device()
        info = sd.query_devices(dev) if dev is not None else sd.query_devices(kind="input")
        self.rate = int(info["default_samplerate"])
        self.stream = sd.InputStream(device=dev, samplerate=self.rate, channels=1, dtype="float32",
                                     blocksize=self.rate // 50, callback=self._cb)
        self.stream.start()

    def stop(self) -> np.ndarray:
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        self.level = 0.0
        if not self.chunks:
            return np.zeros(0, np.float32)
        audio = np.concatenate(self.chunks)[:, 0]
        if self.rate != 16000:
            n = int(len(audio) * 16000 / self.rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio)
        return audio.astype(np.float32)


# --------------------------------------------------------------------------- output
def paste(text: str):
    import keyboard, pyperclip
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.04)
    keyboard.send("ctrl+v")
    time.sleep(0.25)
    if old is not None:
        pyperclip.copy(old)


def make_sounds(volume: int):
    """Soft, short UI tones, regenerated whenever the volume changes."""
    import wave
    SOUND_DIR.mkdir(parents=True, exist_ok=True)
    stamp = SOUND_DIR / "volume.txt"
    if stamp.exists() and stamp.read_text() == str(volume) and (SOUND_DIR / "copied.wav").exists():
        return
    sr = 44100
    specs = {"start": [(740, 0.0), (988, 0.045)], "stop": [(988, 0.0), (740, 0.045)],
             "lock": [(988, 0.0), (988, 0.07)], "copied": [(880, 0.0), (1175, 0.05)]}
    for name, notes in specs.items():
        out = np.zeros(int(sr * 0.16))
        for freq, at in notes:
            n = int(sr * 0.07)
            t = np.arange(n) / sr
            env = np.minimum(1, t / 0.004) * np.exp(-t * 55)
            i = int(at * sr)
            out[i:i + n] += (np.sin(2 * np.pi * freq * t) * env)[: len(out) - i]
        gain = 0.45 * max(0, min(100, volume)) / 100
        pcm = (out / max(1e-9, np.abs(out).max()) * gain * 32767).astype(np.int16)
        with wave.open(str(SOUND_DIR / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    stamp.write_text(str(volume))


def play(name: str, cfg: dict):
    if not cfg.get("sounds"):
        return
    import winsound
    try:
        winsound.PlaySound(str(SOUND_DIR / f"{name}.wav"),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


# --------------------------------------------------------------------------- history
_hist_lock = threading.Lock()


def add_history(entry: dict):
    with _hist_lock, HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_history() -> list[dict]:
    old = APP_DIR / "history.txt"            # migrate v1 plain-text history once
    if old.exists() and not HISTORY_PATH.exists():
        for line in old.read_text(encoding="utf-8").splitlines():
            ts, _, text = line.partition("\t")
            if text:
                add_history({"ts": ts, "text": text, "words": len(text.split()), "secs": 0, "app": "", "to": "paste"})
        old.rename(old.with_suffix(".txt.bak"))
    out = []
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- system
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}" "{Path(__file__).resolve()}"'


def set_startup(enabled: bool):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, "Flowra", 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(k, "Flowra")
                except FileNotFoundError:
                    pass
    except Exception as e:
        log(f"startup setting failed: {e}")


# --------------------------------------------------------------------------- app
class App:
    def __init__(self):
        try:
            self.cfg = read_config()
        except Exception as e:
            log(f"config unreadable ({e}); using defaults")
            self.cfg = dict(DEFAULT_CONFIG)
        if not CONFIG_PATH.exists() and FROZEN:  # first run after install: honour the installer's choice
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Flowra") as k:
                    self.cfg["launch_at_startup"] = bool(winreg.QueryValueEx(k, "LaunchAtStartup")[0])
            except OSError:
                pass
        write_config(self.cfg)                   # normalise + add any new keys
        self.cfg_mtime = CONFIG_PATH.stat().st_mtime
        self.tx = Transcriber(self.cfg)
        self.rec = Recorder(self.cfg)
        self.state = "idle"        # idle | holding | pending | locked | busy
        self.lock = threading.RLock()
        self.t_down = 0.0
        self.swallow_up = False
        self.last_text = ""
        self.overlay = None
        self.hotkey = self.cfg["hotkey"].lower()
        self.paste_last_handle = None
        self.preview_until = 0.0
        self.copied_until = 0.0
        self.suspended = False     # settings window is capturing a new hotkey

    # ---- what the indicator shows
    @property
    def visual_state(self) -> str:
        now = time.time()
        st = self.state
        if st == "pending" or (st == "holding" and now - self.t_down < 0.18):
            st = "idle"
        if st == "idle":
            if now < self.copied_until:
                return "copied"
            if now < self.preview_until:
                return "preview"
            if not self.tx.ready.is_set():
                return "loading"
        return st

    # ---- config live reload
    def watch_config(self):
        while True:
            time.sleep(0.5)
            try:
                m = CONFIG_PATH.stat().st_mtime
                if m != self.cfg_mtime:
                    self.cfg_mtime = m
                    self.apply_config(read_config())
            except json.JSONDecodeError:
                pass  # half-written / invalid file: keep the current config
            except Exception as e:
                log(f"config reload failed: {e}")

    def apply_config(self, new: dict):
        import keyboard
        old = dict(self.cfg)
        try:
            keyboard.key_to_scan_codes(new["hotkey"])
        except Exception:
            log(f"invalid hotkey {new['hotkey']!r}; keeping {old['hotkey']!r}")
            new["hotkey"] = old["hotkey"]
        self.cfg.clear()
        self.cfg.update(new)                     # same dict object -> Transcriber/Recorder see it
        self.hotkey = self.cfg["hotkey"].lower()
        if old.get("paste_last_hotkey") != new.get("paste_last_hotkey"):
            self._bind_paste_last()
        if any(old.get(k) != new.get(k) for k in ("model", "device", "compute_type")):
            log("model settings changed, reloading")
            self.tx.unload()
            threading.Thread(target=self.tx.ensure_loaded, daemon=True).start()
        if old.get("launch_at_startup") != new.get("launch_at_startup"):
            set_startup(bool(new.get("launch_at_startup")))
        if old.get("sound_volume") != new.get("sound_volume"):
            make_sounds(int(new["sound_volume"]))
        if any(old.get(k) != new.get(k) for k in new if k.startswith("indicator_")):
            if self.overlay:
                self.overlay.refresh()
            moved_by_drag = self.overlay and self.overlay.editing
            if new.get("indicator_idle") == old.get("indicator_idle") and not moved_by_drag \
                    and not any(k.startswith("indicator_idle_") and old.get(k) != new.get(k) for k in new):
                self.preview_until = time.time() + 1.8   # show where it lands
        log("settings applied")

    def save_config(self, changes: dict):
        write_config({**self.cfg, **changes})   # the watcher applies it

    def _bind_paste_last(self):
        import keyboard
        if self.paste_last_handle is not None:
            try:
                keyboard.remove_hotkey(self.paste_last_handle)
            except Exception:
                pass
            self.paste_last_handle = None
        combo = self.cfg.get("paste_last_hotkey")
        if combo:
            try:
                self.paste_last_handle = keyboard.add_hotkey(combo, self.paste_last, suppress=False)
            except Exception as e:
                log(f"invalid paste-last hotkey {combo!r}: {e}")

    def paste_last(self):
        if self.last_text:
            threading.Thread(target=lambda: (time.sleep(0.15), paste(self.last_text)), daemon=True).start()

    # ---- keyboard
    def on_key(self, e):
        if self.suspended:
            return
        name = (e.name or "").lower()
        if self.overlay and self.overlay.editing and e.event_type == "down":
            if name == "enter":
                self.overlay.end_edit(True)
            elif name == "esc":
                self.overlay.end_edit(False)
            return
        if name == self.hotkey:
            (self.on_down if e.event_type == "down" else self.on_up)()
        elif e.event_type == "down":
            if name == "esc" and self.state in ("holding", "pending", "locked"):
                self.cancel()
            elif self.state in ("holding", "pending") and time.time() - self.t_down < 0.6:
                self.cancel(quiet=True)   # it was Ctrl+C / Ctrl+V etc, not dictation

    def _app_off(self) -> bool:
        rules = self.cfg.get("app_rules") or {}
        if "off" not in rules.values():
            return False
        from target import _process_info, u32
        try:
            return rules.get(_process_info(u32.GetForegroundWindow())[0]) == "off"
        except Exception:
            return False

    def on_down(self):
        with self.lock:
            if self.state == "holding":
                return
            if self.state == "locked":
                self.swallow_up = True
                self._finish()
            elif self.state == "pending":
                self.state = "locked"
                self.swallow_up = True
                play("lock", self.cfg)
            elif self.state == "idle":
                if self._app_off():
                    return
                try:
                    self.rec.start()
                except Exception as e:
                    log(f"microphone error: {e}")
                    if self.overlay:
                        self.overlay.flash((255, 69, 58, 220))
                    return
                self.state = "holding"
                self.t_down = time.time()
                threading.Timer(self.cfg["tap_max_seconds"], self._confirm_hold).start()

    def _confirm_hold(self):
        if self.state == "holding":
            play("start", self.cfg)

    def on_up(self):
        with self.lock:
            if self.swallow_up:
                self.swallow_up = False
                return
            if self.state != "holding":
                return
            if time.time() - self.t_down < self.cfg["tap_max_seconds"]:
                self.state = "pending"
                threading.Timer(self.cfg["double_tap_window"], self._pending_timeout).start()
            else:
                self._finish()

    def _pending_timeout(self):
        with self.lock:
            if self.state == "pending":
                self.rec.stop()
                self.state = "idle"

    def cancel(self, quiet=False):
        with self.lock:
            if self.state in ("holding", "pending", "locked"):
                self.rec.stop()
                self.state = "idle"
                self.swallow_up = False
                if not quiet:
                    play("stop", self.cfg)

    def _finish(self):
        audio = self.rec.stop()
        if len(audio) < 16000 * self.cfg["min_seconds"]:
            self.state = "idle"
            return
        play("stop", self.cfg)
        self.state = "busy"
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def silence_watch(self):
        """Locked mode: finish automatically after N seconds of silence."""
        while True:
            time.sleep(0.2)
            n = self.cfg.get("auto_stop_silence") or 0
            if n and self.state == "locked" and time.time() - self.rec.started > 1.5 \
                    and time.time() - self.rec.last_voice > n:
                with self.lock:
                    if self.state == "locked":
                        log(f"auto-stop after {n}s silence")
                        self._finish()

    def _process(self, audio):
        import keyboard, pyperclip
        from target import detect
        try:
            t = time.time()
            raw = self.tx.transcribe(audio)
            if self.cfg["voice_commands"] and UNDO.match(raw) and self.last_text:
                for _ in range(len(self.last_text)):
                    keyboard.send("backspace")
                log(f"undo {len(self.last_text)} chars")
                self.last_text = ""
                return
            text = tidy(raw, self.cfg)
            if not text:
                return
            tgt = detect(self.cfg.get("app_rules"))
            to = tgt.action if self.cfg["clipboard_fallback"] else "paste"
            log(f"{time.time() - t:.2f}s  target={tgt.reason} ({tgt.exe}) -> {to}  {text!r}")
            if to == "clipboard":
                pyperclip.copy(text)
                self.copied_until = time.time() + 1.6
                play("copied", self.cfg)
                self.last_text = text + " "
            else:
                out = text if text.endswith("\n") else text + " "
                paste(out)
                self.last_text = out
            add_history({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text,
                         "words": len(text.split()), "secs": round(len(audio) / 16000, 1),
                         "app": tgt.exe, "to": to})
        except Exception as e:
            log(f"error: {e}")
            if self.overlay:
                self.overlay.flash((255, 69, 58, 200))
        finally:
            self.state = "idle"

    def idle_loop(self):
        while True:
            time.sleep(30)
            if self.state == "idle":
                self.tx.unload_if_idle()

    def run(self):
        import keyboard, pystray
        from PIL import Image
        from overlay import Overlay
        import target, settings_server
        threading.Thread(target=self.tx.ensure_loaded, daemon=True).start()
        threading.Thread(target=target.warm_up, daemon=True).start()
        for fn in (self.idle_loop, self.watch_config, self.silence_watch):
            threading.Thread(target=fn, daemon=True).start()
        make_sounds(int(self.cfg["sound_volume"]))
        set_startup(bool(self.cfg.get("launch_at_startup")))
        overlay = self.overlay = Overlay(self)
        keyboard.hook(self.on_key)
        self._bind_paste_last()
        server = settings_server.start(self)

        def quit_(icon, _item=None):
            icon.stop()
            overlay.stop()

        def set_idle(style):
            return lambda icon, item: self.save_config({"indicator_idle": style})

        icon = pystray.Icon(
            "Flowra", Image.open(HERE / "assets" / "logo.png"),
            f"Flowra - hold {self.hotkey}, double-tap to lock",
            menu=pystray.Menu(
                pystray.MenuItem("Settings", lambda: server.open(), default=True),
                pystray.MenuItem("History", lambda: server.open("history")),
                pystray.MenuItem("Move indicator", lambda: overlay.begin_edit()),
                pystray.MenuItem("Indicator", pystray.Menu(
                    *[pystray.MenuItem(label, set_idle(style), radio=True,
                                       checked=lambda _i, s=style: self.cfg["indicator_idle"] == s)
                      for label, style in (("Line", "dash"), ("Dot", "dot"), ("Only while dictating", "hidden"))])),
                pystray.MenuItem("Sounds", lambda: self.save_config({"sounds": not self.cfg["sounds"]}),
                                 checked=lambda _i: self.cfg["sounds"]),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit Flowra", quit_)))
        icon.run_detached()
        log(f"hold '{self.hotkey}' to talk, double-tap to lock, Esc cancels; settings at {server.url}")
        overlay.run()
        os._exit(0)


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\FlowraDictation")
    return ctypes.windll.kernel32.GetLastError() != 183


if __name__ == "__main__":
    if not single_instance():
        sys.exit(0)
    App().run()
