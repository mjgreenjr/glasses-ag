# glasses-ag — Meta glasses microphone on Linux

A small Linux daemon that makes the microphone on Meta smart glasses
usable without a phone. It registers a Hands-Free Profile (HFP) audio
gateway with BlueZ, presents the gateway behavior the glasses accept,
and exposes the resulting wideband audio through PipeWire:

- **glasses-mic** — microphone source, 16 kHz mono mSBC
- **glasses-spk** — call-audio sink, for full-duplex voice apps

The daemon also reports the glasses' battery level when they provide
it through the Apple accessory battery dialect used by these devices.

Developed and tested with Meta Adventurer glasses on Arch Linux /
Omarchy. Other Meta glasses models may work if they advertise a name
beginning with `Meta Glasses` and behave the same way on HFP, but only
the tested configuration should be considered known-good.

## How it works

Meta's glasses do not keep a service-level HFP connection with every
standards-shaped Linux gateway. glasses-ag therefore owns the HFP
profile itself instead of leaving it to WirePlumber. On request it
opens a silent gateway-declared call, negotiates mSBC, and streams the
microphone over SCO. Audio received through the PipeWire sink is sent
back over the same voice link.

A2DP is parked while a call session is live and restored afterward,
matching normal phone-call behavior.

glasses-ag also serves your **other paired HFP headsets** with a
standard gateway — see [Other headsets](#other-headsets-generic-hfp).

No Meta account, cloud service, or network connection is involved.
Pairing is ordinary Bluetooth pairing between the glasses and the
computer.

## Requirements

- Linux with BlueZ, PipeWire, and WirePlumber
- Python 3
- `dbus-python` and PyGObject
- `libsbc` for mSBC decoding and encoding
- `pw-cli` and `pactl` on `PATH` (PipeWire and its PulseAudio
  compatibility layer) — only needed for non-glasses headsets
- The glasses paired with the computer before starting

The included installer is written for Arch-like systems and checks
for the Python and codec dependencies before installing. On Arch, the
packages it asks for are:

```sh
sudo pacman -S python-dbus python-gobject sbc
```

## Install

```sh
./install.sh
```

The installer is user-level and installs:

| File | Destination |
|---|---|
| `glasses_ag.py` | `~/.local/bin/` |
| `hfp_registry.py`, `hfp_generic.py`, `hfp_media.py`, `hfp_audio.py` | `~/.local/bin/` (imported by the daemon) |
| `glasses-mic` | `~/.local/bin/` |
| `glasses-waybar` | `~/.local/bin/` |
| `glasses-ag.service` | `~/.config/systemd/user/` |
| `51-glasses-ag.conf` | `~/.config/wireplumber/wireplumber.conf.d/` |
| `52-glasses-mic.conf` | `~/.config/pipewire/pipewire.conf.d/` (generated) |
| `53-glasses-spk.conf` | `~/.config/pipewire/pipewire.conf.d/` (generated) |

It then restarts PipeWire and WirePlumber and enables the daemon.

## Use

```sh
glasses-mic status    # state=down / ready / starting / live
glasses-mic start     # open the microphone session
glasses-mic stop      # end the session and restore A2DP
glasses-mic toggle
glasses-mic media     # force the glasses back to media/A2DP mode
```

`status` prints the glasses line first, then one `device …` line per
known Bluetooth device. Every verb without a device argument targets
the glasses, exactly as before.

## Discord mode (tap-to-talk, power saving)

Holding an HFP call open is what drains the glasses — a headset-mode
session flattens their battery in a few hours. Discord mode exists so
a call session only exists while you are actually talking:

```sh
glasses-mic discord_on    # arm it (glasses-mic discord toggles)
glasses-mic discord_off   # disarm
```

While armed, the glasses idle in ordinary A2DP media mode and the
daemon advertises itself as a media player (MPRIS, status
*Playing*), which makes the desktop deliver the glasses' temple-tap
play/pause events to the daemon instead of to a paused music app.
Then:

- **Tap once** — the microphone session opens (you hear the usual
  two-blip connect cue). The tap never starts or pauses your music.
- **Speak, then stop** — after about 4 seconds of silence the
  session closes by itself. A descending chirp sounds just before
  it drops, so the close is never a surprise.
- **During a call, taps stay in the glasses.** Temple taps only
  reach the computer in media mode; once a session is up and A2DP
  is parked, the glasses keep their taps to themselves, so no tap
  can extend or end a live session. When a session closes, tap
  again to reopen — reopening takes about a second.
- Sessions close automatically at a **90-second safety cap**, so
  a forgotten open mic can't drain the glasses — with one grace:
  if you're mid-speech when the cap arrives, a warning tone sounds
  and the close waits for your next natural pause. An absolute
  ceiling of **120 seconds** applies no matter what.

Suggested Discord wiring: **Input = Meta Glasses Mic** (it's silent
while the glasses are in media mode). For **Output**, pick *Meta
Glasses Call Audio* if you want to hear people during your own
talking bursts, or the glasses' A2DP device if you mostly listen and
only tap to interject (you won't hear others during your own burst —
A2DP is parked while you talk, as with a phone call).

One interaction to know about: if another app is *actively playing*
to any device when you tap, the desktop hands taps to playing
players in the order they started playing — the first tap may pause
that playback before a later tap reaches the daemon. Paused players
never steal the tap while Discord mode is armed.

Thresholds can be tuned in the service file and the daemon restarted,
e.g. `Environment=GLASSES_AG_DISCORD_CAP=120`:

| Variable | Default | Meaning |
|---|---|---|
| `GLASSES_AG_DISCORD_CAP` | `90` | session cap, seconds (defers to the next pause if you're mid-speech) |
| `GLASSES_AG_DISCORD_CAP_MAX` | `120` | absolute session ceiling, seconds — no deferral past this |
| `GLASSES_AG_DISCORD_HANGOVER` | `4` | seconds of silence before auto-close |
| `GLASSES_AG_DISCORD_VAD_RMS` | `300` | mic level (RMS) that counts as voice |

Hyprland keybind example:

```ini
bind = SUPER SHIFT, M, exec, glasses-mic toggle
```

See [BAR.md](BAR.md) for an optional bar module that displays state
and battery.

## Other headsets (generic HFP)

**While glasses-ag is installed it serves the HFP Audio Gateway role
for your paired headsets.** WirePlumber's native HFP stays off
(`51-glasses-ag.conf`), so the daemon — not WirePlumber — answers
every paired device that connects over HFP. Uninstalling glasses-ag
(and removing that file) restores WirePlumber's behavior completely.
HSP-only headsets are untouched: WirePlumber keeps the HSP role.

Admission is ordinary Bluetooth pairing/trust; there is no allowlist.
Devices whose name starts with `Meta Glasses` (or the pinned address,
below) get the glasses behavior; every other paired device gets a
standard phone-like gateway: the connection stays up, and the call
audio link opens only when it is wanted.

Each such headset gets its own PipeWire nodes, named after the device
as it presents itself, e.g. **Corsair HS55 Mic** (source) and
**Corsair HS55 Call Audio** (sink). They exist while the headset is
connected. Pick them in your voice app like any other device.

The call audio link (and so the headset's microphone) opens when
either:

- the headset asks (its call/mic button, `AT+BCC`), or
- an app attaches a stream to the device's **Mic** source or **Call
  Audio** sink (checked by polling `pactl` about once a second).

It closes when the headset drops it, or — if an app opened it — when
the app detaches. There are no timers and no voice detection for
headsets. While the call link is open, the headset's A2DP (music)
profile is parked and restored when it closes, like a phone call.
Note that an app holding the Mic source open all the time (a voice
app left on that input) keeps the headset in call mode; so do
monitoring streams such as a mixer's level meter. `glasses-mic start
<device>` / `stop <device>` open and close it by hand, and
`glasses-mic status <device>` shows one device in detail; a device is
named by Bluetooth address or by name (`glasses-mic start corsair`).

**One call at a time.** Any number of headsets may stay connected, but
only one call audio link exists across all devices — the glasses
count as one. A second device asking for audio while a call is live is
refused with a logged reason, and shows up as `audio_refusals` on its
`status` line.

### Known limitation: a connected second device can block later glasses calls

Observed on a Realtek adapter (behavior may vary by adapter): with a
second device connected (service-level link up, even idle), the
**first** glasses call succeeds, but **subsequent** calls are refused
by the controller — the synchronous connection fails with HCI status
`0x1F`, which the daemon logs as `Errno 38`, then `111` — until the
second device disconnects; after that, glasses calls work again. It
is state-dependent, not a flat "never works", and was reproduced with
two different vendors' headsets. Nothing in glasses-ag releases the
other device's link for you: **disconnect or power off other headsets
before glasses calls** (this matters most from the second call on).
When a call-audio connection fails while another device is connected,
the journal names this likely cause and the device's `status` line
carries it as `audio_reason` for as long as the other device stays
connected (and the cumulative `audio_refusals` count stays). When the
glasses are not in a call, other headsets get full service.

### Headset reconnect notes

Observed on a Corsair HS55, and on one Sennheiser pairing variant:
after a **host-side reconnect** (reconnecting from the computer's
Bluetooth manager rather than from the headset) the headset may come
back **A2DP-only** — it simply does not open its Hands-Free (HFP)
connection, so no `… Mic` source appears and `glasses-mic status`
shows no device line for it. **Power-cycling the headset restores the
HFP session.** This is the headset being lazy about HFP, not the
daemon: glasses-ag only answers the connection when the headset opens
it, and it is a bystander here. Audio playback (A2DP) is unaffected
either way.

## Device selection and configuration

By default, the daemon gives the glasses behavior to a device whose
advertised Bluetooth name starts with `Meta Glasses`; every other
paired device is served as a generic headset (above).

To pin it to one specific pair, set the Bluetooth address in
`~/.config/systemd/user/glasses-ag.service`:

```ini
Environment=GLASSES_AG_ADDRESS=AA:BB:CC:DD:EE:FF
```

Then reload and restart the service:

```sh
systemctl --user daemon-reload
systemctl --user restart glasses-ag.service
```

If a model uses a different advertised name, `GLASSES_AG_NAME_PREFIX`
can override the default prefix. The bar helper accepts the same two
environment variables.

## Behavior and limitations

- The glasses believe they are in a call while the microphone is
  live. A2DP media remains parked for the duration.
- Audio is 16 kHz mono: appropriate for speech, dictation, calls,
  and voice assistants, not high-quality music recording.
- The WirePlumber configuration leaves the HSP gateway role available
  for HSP-only headsets, which WirePlumber keeps serving. An HSP
  fallback for headsets that also speak HFP was measured *not* to
  materialize on the reference build, so don't rely on it: while
  glasses-ag is installed it is the HFP stack for the machine. Other
  HFP headsets are served by the generic gateway (see above).
- Call audio is one call at a time across all devices, and a second
  connected device can block the glasses' call audio on some adapters
  (see *Known limitation*).
- The daemon logs its local AT-command transcript to the systemd
  journal. Those logs stay on the computer, but review them before
  attaching them to a public bug report; they can contain device
  details.

## Troubleshooting

- **Daemon not reachable:** check
  `systemctl --user status glasses-ag` and
  `journalctl --user -u glasses-ag -e`. The usual cause is
  WirePlumber still owning HFP; confirm that `51-glasses-ag.conf` is
  installed and WirePlumber was restarted afterward.
- **State stays `down`:** the glasses have not opened their HFP
  connection. If they connected before the daemon started, disconnect
  and reconnect them with your Bluetooth manager, or use
  `bluetoothctl disconnect <glasses-address>` followed by
  `bluetoothctl connect <glasses-address>`.
- **No `glasses-mic` source in `wpctl status`:** check that the
  generated `52-glasses-mic.conf` uses the current
  `$XDG_RUNTIME_DIR`, then restart `pipewire.service`.
- **A headset connects but has no `… Mic` source:** the journal line
  `audio provisioning FAILED` gives the reason (usually `pw-cli` not
  on `PATH`, or the PipeWire `pipe-tunnel` module missing). The
  headset's connection and A2DP are unaffected; audio-open is refused
  until the next attempt succeeds (retried at most every 30 s).
- **Microphone is live but silent in an app:** select the
  **glasses-mic** source in the app or with `wpctl set-default`; the
  system default may point at another microphone.

## Uninstall

```sh
systemctl --user disable --now glasses-ag.service
rm ~/.local/bin/glasses_ag.py ~/.local/bin/hfp_{registry,generic,media,audio}.py
rm ~/.local/bin/glasses-mic ~/.local/bin/glasses-waybar
rm ~/.config/systemd/user/glasses-ag.service
rm ~/.config/wireplumber/wireplumber.conf.d/51-glasses-ag.conf
rm ~/.config/pipewire/pipewire.conf.d/52-glasses-mic.conf
rm ~/.config/pipewire/pipewire.conf.d/53-glasses-spk.conf
systemctl --user daemon-reload
systemctl --user restart pipewire.service wireplumber.service
```

Removing the WirePlumber configuration returns HFP ownership to
PipeWire/WirePlumber, and your headsets go back to being handled by it
as before.
