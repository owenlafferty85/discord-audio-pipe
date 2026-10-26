"""Route chosen applications' audio into the bot's capture device.

The bot streams one input device into voice. This module decides which
applications feed that device, from a list in ``DAP_apps.json`` sitting
next to ``main.pyw`` / the ``.exe``::

    {
      "apps": ["youtube music", "zen", "strawberry"],
      "hear_locally": true
    }

Each entry is matched case-insensitively as a substring of an app's
identifiers, so ``"zen"`` matches ``zen``, ``zen.exe`` and the flatpak id
``app.zen_browser.zen``. Run ``python app_router.py list`` while the app is
playing to see what it is called on this machine.

Two platforms, one config:

**Linux (PipeWire).** ``setup`` writes a loopback whose input is the
``audio_player`` sink and whose output is the ``audio_player_capture``
source, plus an ALSA device ``audio_player`` reading that source. The router
moves matching playback streams onto ``audio_player`` with ``pactl``. With
``hear_locally`` it also loops ``audio_player.monitor`` to your speakers,
because a stream moved onto the virtual sink is otherwise silent locally.

**Windows.** The virtual device is VB-CABLE: apps play into ``CABLE Input``
and the bot captures ``CABLE Output``. The router sets each matching
running process's output device with NirSoft ``svcl.exe``
(``/SetAppDefault``), and ``hear_locally`` toggles "Listen to this device"
on ``CABLE Output``.

Routing is undone on exit: every stream or process the router moved goes
back to the default output, and the local loopback is removed. WirePlumber
and Windows both remember an app's last output device, so a router that
exits without restoring leaves those apps silent; ``python app_router.py
restore`` fixes that by hand.

The router never touches an input device. It only moves *playback*, so it
cannot put a microphone on air.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("dap.router")

FILENAME = "DAP_apps.json"

DEFAULTS = {
    "apps": [],
    "hear_locally": True,
}

#: How often to look for new app streams. A stream that starts between
#: polls plays to its old output for at most this long.
POLL_SECONDS = 1.0

LINUX_SINK = "audio_player"
LINUX_SOURCE = "audio_player_capture"
LINUX_ALSA_DEVICE = "audio_player"

WINDOWS_CABLE_INPUT = "CABLE Input"
WINDOWS_CABLE_OUTPUT = "CABLE Output"

PIPEWIRE_CONF = """\
# Virtual output for discord-audio-pipe, written by `app_router.py setup`.
# Apps routed to the "audio_player" sink come out of the
# "audio_player_capture" source, which the Discord bot streams into voice.
context.modules = [
  { name = libpipewire-module-loopback
    args = {
      node.description = "Audio Player (Discord bot)"
      capture.props = {
        node.name      = "audio_player"
        media.class    = Audio/Sink
        audio.position = [ FL FR ]
      }
      playback.props = {
        node.name        = "audio_player_capture"
        node.description = "Audio Player capture (Discord bot)"
        media.class      = Audio/Source
        audio.position   = [ FL FR ]
      }
    }
  }
]
"""

ASOUNDRC_BLOCK = """\
# Capture side of the "audio_player" PipeWire sink, for discord-audio-pipe.
# Written by `app_router.py setup`.
pcm.audio_player {
    type pipewire
    capture_node "audio_player_capture"
    hint {
        show on
        description "Audio Player (apps routed for Discord)"
    }
}
"""


def config_path():
    """``DAP_apps.json`` next to the ``.exe`` when frozen, else next to this file."""
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, FILENAME)


def load_config(path=None):
    """Read the app list. Missing or broken file means "route nothing"."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("router: cannot read %s (%s); routing nothing", path, exc)
        return dict(DEFAULTS)

    cfg = dict(DEFAULTS)
    apps = raw.get("apps") if isinstance(raw, dict) else None
    if isinstance(apps, list):
        cfg["apps"] = [a.strip().lower() for a in apps
                       if isinstance(a, str) and a.strip()]
    if isinstance(raw, dict) and isinstance(raw.get("hear_locally"), bool):
        cfg["hear_locally"] = raw["hear_locally"]
    return cfg


def matches(patterns, identifiers):
    """The first pattern found inside any identifier, or None."""
    haystack = [i.lower() for i in identifiers if i]
    for pattern in patterns:
        if any(pattern in item for item in haystack):
            return pattern
    return None


def run(cmd, check=False):
    """Run a command quietly; return stdout, or None if it failed."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("router: %s failed: %s", cmd[0], exc)
        return None
    if done.returncode != 0:
        if check:
            log.warning("router: %s exited %d: %s", " ".join(cmd),
                        done.returncode, done.stderr.strip())
        return None
    return done.stdout


class LinuxBackend:
    """PipeWire via its PulseAudio interface (``pactl``)."""

    name = "linux"

    def __init__(self):
        self.loopback_module = None

    def problems(self):
        if not shutil.which("pactl"):
            return ["pactl not found; install pipewire-pulse (or libpulse)"]
        sinks = run(["pactl", "list", "short", "sinks"]) or ""
        sources = run(["pactl", "list", "short", "sources"]) or ""
        found = []
        if f"\t{LINUX_SINK}\t" not in sinks:
            found.append(f"sink '{LINUX_SINK}' missing; run: python app_router.py setup")
        if f"\t{LINUX_SOURCE}\t" not in sources:
            found.append(f"source '{LINUX_SOURCE}' missing; run: python app_router.py setup")
        return found

    def streams(self):
        """Every playback stream as (id, identifiers, current sink name)."""
        out = run(["pactl", "-f", "json", "list", "sink-inputs"])
        sinks = run(["pactl", "-f", "json", "list", "short", "sinks"])
        if out is None or sinks is None:
            return []
        sink_names = {s["index"]: s["name"] for s in json.loads(sinks)}
        result = []
        for s in json.loads(out):
            p = s.get("properties", {})
            ids = [p.get("application.name"), p.get("application.process.binary"),
                   p.get("application.id"), p.get("pipewire.access.portal.app_id"),
                   p.get("node.name")]
            # Module-made streams (our own hear-locally loopback among them)
            # are plumbing, not apps; routing one could feed the sink into
            # itself.
            if "pulse.module.id" in p:
                continue
            result.append((s["index"], ids, sink_names.get(s["sink"], "")))
        return result

    def route(self, stream_id):
        return run(["pactl", "move-sink-input", str(stream_id), LINUX_SINK], check=True) is not None

    def unroute(self, stream_id):
        return run(["pactl", "move-sink-input", str(stream_id), "@DEFAULT_SINK@"], check=True) is not None

    def set_hear_locally(self, on):
        if on and self.loopback_module is None:
            out = run(["pactl", "load-module", "module-loopback",
                       f"source={LINUX_SINK}.monitor", "latency_msec=30",
                       "source_dont_move=true"], check=True)
            if out:
                self.loopback_module = out.strip()
                log.info("router: hearing routed apps locally (module %s)", self.loopback_module)
        elif not on and self.loopback_module is not None:
            run(["pactl", "unload-module", self.loopback_module], check=True)
            log.info("router: stopped hearing routed apps locally")
            self.loopback_module = None


class WindowsBackend:
    """VB-CABLE plus NirSoft ``svcl.exe`` for per-app output devices."""

    name = "windows"

    def __init__(self):
        self.svcl = self._find_svcl()
        self.listening = False

    @staticmethod
    def _find_svcl():
        here = os.path.dirname(config_path())
        for candidate in (os.path.join(here, "svcl.exe"),
                          os.path.join(here, "svcl", "svcl.exe")):
            if os.path.isfile(candidate):
                return candidate
        return shutil.which("svcl.exe") or shutil.which("svcl")

    def problems(self):
        found = []
        if not self.svcl:
            found.append("svcl.exe not found; download it from "
                         "https://www.nirsoft.net/utils/sound_volume_command_line.html "
                         "and put svcl.exe next to main.pyw / the .exe")
        try:
            import sounddevice as sd
            names = [d["name"] for d in sd.query_devices()]
        except Exception as exc:  # noqa: BLE001 -- PortAudio can fail many ways
            names = []
            found.append(f"cannot list audio devices: {exc}")
        if names and not any(n.startswith(WINDOWS_CABLE_OUTPUT) for n in names):
            found.append("VB-CABLE not installed; get it from https://vb-audio.com/Cable/ "
                         "and reboot")
        return found

    def streams(self):
        """Every running process as (exe name, identifiers, unknown sink)."""
        out = run(["tasklist", "/FO", "CSV", "/NH"])
        if out is None:
            return []
        exes = set()
        for line in out.splitlines():
            exe = line.split('","')[0].strip('"')
            if exe:
                exes.add(exe)
        return [(exe, [exe, exe.rsplit(".", 1)[0]], "") for exe in sorted(exes)]

    def route(self, exe):
        return run([self.svcl, "/SetAppDefault", WINDOWS_CABLE_INPUT, "all", exe], check=True) is not None

    def unroute(self, exe):
        return run([self.svcl, "/SetAppDefault", "DefaultRenderDevice", "all", exe], check=True) is not None

    def set_hear_locally(self, on):
        if on == self.listening or not self.svcl:
            return
        flag = "1" if on else "0"
        if run([self.svcl, "/SetListenToThisDevice", WINDOWS_CABLE_OUTPUT, flag], check=True) is not None:
            if on:
                run([self.svcl, "/SetPlaybackThroughDevice", WINDOWS_CABLE_OUTPUT,
                     "DefaultRenderDevice"], check=True)
            self.listening = on
            log.info("router: hear locally %s", "on" if on else "off")


def make_backend():
    return WindowsBackend() if sys.platform == "win32" else LinuxBackend()


class Router:
    """Polls for app streams and keeps matching ones on the virtual device.

    The config file is re-read whenever its mtime changes, so editing
    ``DAP_apps.json`` while the bot runs takes effect within a second.
    """

    def __init__(self, backend=None, path=None):
        self.backend = backend or make_backend()
        self.path = path or config_path()
        self.cfg = dict(DEFAULTS)
        self.mtime = None
        # stream id -> pattern that matched, for everything we moved
        self.routed = {}
        self._stop = threading.Event()
        self._thread = None

    def reload_if_changed(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return
        self.mtime = mtime
        self.cfg = load_config(self.path) or dict(DEFAULTS)
        log.info("router: config %s apps=%s hear_locally=%s",
                 self.path, self.cfg["apps"], self.cfg["hear_locally"])
        self.backend.set_hear_locally(self.cfg["hear_locally"])

    def tick(self):
        self.reload_if_changed()
        live = set()
        for stream_id, ids, sink in self.backend.streams():
            live.add(stream_id)
            pattern = matches(self.cfg["apps"], ids)
            if pattern and stream_id not in self.routed:
                if self.backend.route(stream_id):
                    self.routed[stream_id] = pattern
                    log.info("router: routed %s (%s) via %r",
                             stream_id, next((i for i in ids if i), "?"), pattern)
            elif not pattern and stream_id in self.routed:
                if self.backend.unroute(stream_id):
                    log.info("router: released %s, no longer in config", stream_id)
                del self.routed[stream_id]
        # Streams that ended are gone; forget them so a reused id is fresh.
        for stream_id in list(self.routed):
            if stream_id not in live:
                del self.routed[stream_id]

    def restore(self):
        """Send everything we moved back to the default output."""
        for stream_id in list(self.routed):
            self.backend.unroute(stream_id)
            log.info("router: restored %s to the default output", stream_id)
        self.routed.clear()
        self.backend.set_hear_locally(False)

    def _loop(self):
        problems = self.backend.problems()
        for p in problems:
            log.warning("router: %s", p)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- a bad poll must not kill routing
                log.exception("router: poll failed")
            self._stop.wait(POLL_SECONDS)
        try:
            self.restore()
        except Exception:  # noqa: BLE001
            log.exception("router: restore failed")

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="dap-router", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def start_if_configured():
    """Start routing when ``DAP_apps.json`` exists; return the Router or None."""
    if not os.path.exists(config_path()):
        log.info("router: no %s, not routing any apps", FILENAME)
        return None
    return Router().start()


def setup_linux():
    """Write the PipeWire loopback and ALSA device, then report what to restart."""
    conf_dir = os.path.expanduser("~/.config/pipewire/pipewire.conf.d")
    os.makedirs(conf_dir, exist_ok=True)
    conf = os.path.join(conf_dir, "audio-player.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(PIPEWIRE_CONF)
    print(f"wrote {conf}")

    asoundrc = os.path.expanduser("~/.asoundrc")
    existing = open(asoundrc, encoding="utf-8").read() if os.path.exists(asoundrc) else ""
    if "pcm.audio_player" in existing:
        print(f"{asoundrc} already defines pcm.audio_player")
    else:
        with open(asoundrc, "a", encoding="utf-8") as f:
            f.write(("\n" if existing else "") + ASOUNDRC_BLOCK)
        print(f"added pcm.audio_player to {asoundrc}")
    print("restart PipeWire to load it: "
          "systemctl --user restart pipewire pipewire-pulse wireplumber")


def cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Route app audio for discord-audio-pipe")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="create the virtual device (Linux) or check prerequisites (Windows)")
    sub.add_parser("check", help="report anything missing")
    sub.add_parser("list", help="show what is playing now and whether it matches")
    sub.add_parser("run", help="route apps until Ctrl+C, without the bot")
    sub.add_parser("restore", help="move every playing app back to the default output")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    backend = make_backend()
    cfg = load_config()
    if cfg is None:
        print(f"note: {config_path()} does not exist; copy DAP_apps.example.json to start")
        cfg = dict(DEFAULTS)

    if args.command == "setup":
        if backend.name == "linux":
            setup_linux()
        problems = backend.problems()
        print("\n".join(problems) if problems else "everything is in place")
    elif args.command == "check":
        problems = backend.problems()
        print("\n".join(problems) if problems else "everything is in place")
    elif args.command == "list":
        for stream_id, ids, sink in backend.streams():
            pattern = matches(cfg["apps"], ids)
            names = ", ".join(sorted({i for i in ids if i}))
            mark = f"ROUTED by {pattern!r}" if pattern else "-"
            where = f" -> {sink}" if sink else ""
            print(f"{stream_id}: {names}{where}  [{mark}]")
    elif args.command == "run":
        router = Router(backend).start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            router.stop()
    elif args.command == "restore":
        for stream_id, ids, sink in backend.streams():
            if backend.name == "windows":
                if matches(cfg["apps"], ids):
                    backend.unroute(stream_id)
                    print(f"restored {stream_id}")
            elif sink == LINUX_SINK:
                backend.unroute(stream_id)
                print(f"restored {stream_id}")
        backend.set_hear_locally(False)


if __name__ == "__main__":
    cli()
