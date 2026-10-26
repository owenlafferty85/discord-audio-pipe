import os
import sys

# On PipeWire, an ALSA device that targets a node (like the "audio_player"
# capture device app_router.py sets up) silently falls back to the default
# microphone when that node is missing. dont-fallback makes the open fail
# instead, so a missing virtual device can never put a mic on air. Set before
# PortAudio opens anything; the PipeWire ALSA plugin reads it per stream.
if sys.platform.startswith("linux"):
    os.environ.setdefault("PIPEWIRE_ALSA", "{ node.dont-fallback = true }")

import discord
import sounddevice as sd
from pprint import pformat

DEFAULT = 0
sd.default.dtype = "int16"
sd.default.latency = "low"
sd.default.channels = discord.opus._OpusStruct.CHANNELS
sd.default.samplerate = discord.opus._OpusStruct.SAMPLING_RATE

class PCMStream(discord.AudioSource):
    def __init__(self):
        discord.AudioSource.__init__(self)
        self.stream = None
        self.frames = discord.opus._OpusStruct.SAMPLES_PER_FRAME

    def read(self):
        if self.stream is None:
            return

        data = self.stream.read(self.frames)[0]

        # convert to pcm format
        return bytes(data)

    def change_device(self, num):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()

        self.stream = sd.RawInputStream(device=num)
        self.stream.start()


class DeviceNotFoundError(Exception):
    def __init__(self):
        self.devices = sd.query_devices()
        self.host_apis = sd.query_hostapis()
        super().__init__("No Devices Found")

    def __str__(self):
        return (
            f"Devices \n"
            f"{self.devices} \n "
            f"Host APIs \n"
            f"{pformat(self.host_apis)}"
        )


def query_devices():
    options = {
        device.get("name"): index
        for index, device in enumerate(sd.query_devices())
        if (device.get("max_input_channels") > 0 and device.get("hostapi") == DEFAULT)
    }

    if not options:
        raise DeviceNotFoundError()

    return options
