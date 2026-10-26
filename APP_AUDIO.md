# Streaming chosen apps into voice

The bot streams one input device into a voice channel. `app_router.py` decides
which applications feed that device, so YouTube Music, Zen or an MP3 player can
go on air while system sounds and your microphone stay off it.

The bot joins voice as its own member ("audio player"). Your own Discord
microphone is untouched. Your friends hear the bot, and you keep talking
through your own mic as usual.

## Choosing apps

Copy `DAP_apps.example.json` to `DAP_apps.json`, next to `main.pyw` or the
`.exe`, and list the apps:

```json
{
  "apps": ["youtube music", "zen", "strawberry"],
  "hear_locally": true
}
```

- An entry matches when it appears anywhere in an app's name, case ignored.
  `"zen"` matches `Zen`, `zen-bin`, `zen.exe` and `app.zen_browser.zen`.
- To find an app's name, start it playing and run
  `python app_router.py list`. Every playing app is listed with the names it
  can be matched by, and whether the current config routes it.
- Routing a browser routes every tab in it. To send only one site, play it in
  a browser you don't otherwise use, or use the site's desktop app.
- With `hear_locally` true, you keep hearing routed apps through your
  speakers. Set it to false if you would rather listen to the bot in Discord.
  If you hear everything twice, either set it to false or right-click the bot
  in the voice channel and mute it for yourself.
- The bot reads the file again within a second of you saving it. No restart
  needed.

When the bot starts and `DAP_apps.json` exists, routing starts with it. When
the bot exits, every routed app goes back to your normal output. If the bot
crashed and an app is now silent, run `python app_router.py restore`.

In the bot window, pick the device listed below for your platform, then
choose the providence server and a voice channel and press Connect.

## Linux (PipeWire)

Pick the device **`audio_player`**.

One-time setup, already done on the laptop:

```
python app_router.py setup
systemctl --user restart pipewire pipewire-pulse wireplumber
python app_router.py check
```

`setup` writes `~/.config/pipewire/pipewire.conf.d/audio-player.conf`, a
loopback whose input is the sink "Audio Player (Discord bot)" and whose output
is the source `audio_player_capture`. It also adds an ALSA device
`audio_player` to `~/.asoundrc` that reads that source. The router moves
matching app streams onto the sink with `pactl move-sink-input`.

The microphone guard. A PipeWire ALSA device whose target node is missing
quietly records the default microphone instead. That was measured on the
laptop, and it would put your mic on air. `sound.py` sets
`PIPEWIRE_ALSA='{ node.dont-fallback = true }'` before any device opens, so a
missing `audio_player_capture` makes the device fail to open instead.

## Moving to Windows

Same Discord account, same bot, same server. The bot application and its
invite to providence carry over unchanged. Only the machine-side pieces are
new.

1. Install Python 3.12 and Git, then clone the repo and switch to this
   branch:
   ```
   git clone https://github.com/owenlafferty85/discord-audio-pipe
   cd discord-audio-pipe
   git switch feature/app-audio-routing
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
2. Install VB-CABLE from https://vb-audio.com/Cable/ and reboot. It adds a
   playback device `CABLE Input` and a recording device `CABLE Output`.
3. Download NirSoft SoundVolumeCommandLine (`svcl.exe`) from
   https://www.nirsoft.net/utils/sound_volume_command_line.html and put
   `svcl.exe` next to `main.pyw`. It is gitignored.
4. Bring the token over. Copy `token.txt` from the laptop, or reset the token
   on the bot's page in the Discord developer portal and save the new one as
   `token.txt`. A reset invalidates the laptop's copy, which is fine once the
   laptop is retired.
5. Copy `DAP_apps.json`, then change entries to Windows process names where
   they differ, for example `"youtube music desktop app"` or `"foobar2000"`.
   With the apps playing, `python app_router.py list` shows every running
   `.exe`.
6. Run `python app_router.py check`, then `.venv\Scripts\python main.pyw`.
   Pick **`CABLE Output (VB-Audio Virtual Cable)`** as the device.

On Windows the router sets each matching app's output device to
`CABLE Input` with `svcl.exe /SetAppDefault`, the same setting as Settings >
System > Sound > Volume mixer. `hear_locally` turns on "Listen to this device"
for `CABLE Output`. On exit, routed apps go back to the default output device.
You can also skip the router and set apps to `CABLE Input` by hand in the
Volume mixer. Leave `DAP_apps.json` out and the bot routes nothing.

The Windows side of `app_router.py` has not been run yet. It was written
against svcl's documented commands, with no Windows machine to test on. If a
step misbehaves, the log is `DAP_session.log` (lines start `router:`), and
the code is the `WindowsBackend` class in `app_router.py`.

### Notes for an AI agent doing the move

- Read this file and `app_router.py` first. The rest of the repo is the
  upstream bot and needs no changes for this.
- Never stream a microphone. On Windows the device must be `CABLE Output`.
  Before connecting, confirm with `python main.pyw -D` that the index you
  pass with `-d` is `CABLE Output`.
- The bot token lives only in `token.txt`. Never print it, commit it, or put
  it in `DAP_config.json`.
- Joining voice is audible to the channel. Ask before running
  `main.pyw -c <channel> -d <device>`.
- `python main.pyw -C` lists the servers and voice channels the bot can see
  without joining anything. Use it to confirm providence is there.
