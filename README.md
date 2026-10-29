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

No Meta account, cloud service, or network connection is involved.
Pairing is ordinary Bluetooth pairing between the glasses and the
computer.

## Requirements

- Linux with BlueZ, PipeWire, and WirePlumber
- Python 3
- `dbus-python` and PyGObject
- `libsbc` for mSBC decoding and encoding
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

Hyprland keybind example:

```ini
bind = SUPER SHIFT, M, exec, glasses-mic toggle
```

See [BAR.md](BAR.md) for an optional bar module that displays state
and battery.

## Device selection and configuration

By default, the daemon accepts an HFP connection only from a device
whose advertised Bluetooth name starts with `Meta Glasses`. That
prevents it from taking HFP connections away from unrelated headsets.

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
  so other headsets can still fall back to HSP for their microphones.
  HSP is narrower-band than HFP/mSBC.
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
- **Microphone is live but silent in an app:** select the
  **glasses-mic** source in the app or with `wpctl set-default`; the
  system default may point at another microphone.

## Uninstall

```sh
systemctl --user disable --now glasses-ag.service
rm ~/.local/bin/glasses_ag.py ~/.local/bin/glasses-mic ~/.local/bin/glasses-waybar
rm ~/.config/systemd/user/glasses-ag.service
rm ~/.config/wireplumber/wireplumber.conf.d/51-glasses-ag.conf
rm ~/.config/pipewire/pipewire.conf.d/52-glasses-mic.conf
rm ~/.config/pipewire/pipewire.conf.d/53-glasses-spk.conf
systemctl --user daemon-reload
systemctl --user restart pipewire.service wireplumber.service
```

Removing the WirePlumber configuration returns HFP ownership to
PipeWire/WirePlumber.
