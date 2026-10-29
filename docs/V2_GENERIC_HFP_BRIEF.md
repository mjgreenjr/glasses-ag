# glasses-ag v2 — Generic HFP support (work order)

Status: **rulings complete (Oct 29, 2026)** — ready for
implementation. Prepared for a cloud session working cold: this
document is self-contained. Where it conflicts with the README or
code comments, this document wins; flag the conflict in your
handoff.

## What this project is

glasses-ag is a small Linux daemon (repo: mjgreenjr/glasses-ag)
that registers the Bluetooth HFP Audio Gateway profiles with
BlueZ itself, so a pair of Meta Adventurer smart glasses gets a
working microphone on Linux — something WirePlumber's HFP cannot
do for this device. Layout:

- `glasses_ag.py` (~1,560 lines, the whole daemon): D-Bus profile
  registration and SLC handling (`Profile`, `Session`, `AGCore`),
  mSBC codec (`MSBCEncoder`/`MSBCDecoder`, `SCOParser`), PipeWire
  FIFO bridging (`PipeSource`, `SpkSink`), and a Discord mode
  (`MPRISPlayer`): an armed MPRIS tap-to-talk state machine for
  the glasses with an RMS voice-activity governor.
- `glasses-mic`: control CLI; talks to the daemon over a Unix
  socket (verbs: status/start/stop/media/discord/discord_on/
  discord_off).
- `glasses-waybar`: status widget script for the Waybar/Quickshell
  bar.
- `51-glasses-ag.conf`: WirePlumber policy — BlueZ roles limited
  to A2DP + HSP AG so the daemon owns HFP.
  `52-glasses-mic.conf.tmpl` / `53-glasses-spk.conf.tmpl`: static
  PipeWire tunnel nodes for the glasses' mic source / call sink.
- `install.sh`, `glasses-ag.service`: install + systemd user unit.

Today the daemon serves exactly one device — the glasses, at a
fixed Bluetooth address, with a Samsung-phone-replica gateway
personality (verified working on hardware, including Discord
mode, Oct 26–29, 2026).

## Why v2 exists

Registering HFP with BlueZ is exclusive: while glasses-ag is
installed, the daemon — not WirePlumber — is the HFP stack for
the **whole machine**, and today it refuses every non-glasses
device's HFP connection. Measured consequence: an ordinary
headset (a Corsair HS55) becomes A2DP-playback-only with its mic
unavailable; WirePlumber's HSP fallback does not materialize on
the reference build. The repo is headed public, and the bar is
non-regression for strangers: anyone who installs glasses-ag must
find their ordinary HFP headset still working.

## Settled premises (not up for redesign)

- Admission = BlueZ pairing/trust. **No daemon-side MAC
  allowlist.** A fingerprint/quirk table selects which gateway
  personality a device gets; it never gates admission.
- **HSP is out of scope.** WirePlumber keeps the `hsp_ag` role;
  HSP-only headsets remain WirePlumber's, untouched.
- The **glasses personality is glasses-only** and its code path
  is the frozen regression baseline: Samsung-replica SLC, silent
  declared call, injected cue, Discord-mode state machine.
  Generic support is added *around* it, sharing only the proven
  helpers (codec encode/decode, SCO setup, FIFO bridging).
- Headsets get standard phone-like gateway behavior: SLC stays
  up, SCO follows actual audio demand. No tap/VAD/safety-cap
  machinery for headsets.

## Rulings (Oct 29, 2026)

1. **Node naming:** generic devices' PipeWire nodes are named
   after the device (e.g. "Corsair HS55 Mic" / "Corsair HS55
   Call Audio"), matching how WirePlumber presents devices.
2. **Concurrency:** connections are unlimited — any number of
   headsets may hold SLCs. **Audio is one call at a time**: a
   second device requesting SCO while a call is live gets a
   clean refusal (logged; no wedged state). This is a policy
   check at audio-open, not an architectural limit. (Ruled with
   the condition that no functionality is lost — none is: today
   non-glasses devices get nothing at all.)
3. **App-attach detection:** pactl-based watching first (an app
   attaching to a device's tunnel nodes is an activation
   trigger). PipeWire-native graph watching is a later revisit
   only if the polling proves flaky on hardware.
4. **Test spread:** the household inventory (Meta glasses,
   Corsair HS55, a Sennheiser pair) is already three vendors —
   no test-unit purchase is planned; revisit only if the
   inventory collapses to one vendor in practice.

## Steps (each step ends with a handoff and a stop)

Work on branch **`v2-generic-hfp`** off `main`. Each step writes
`docs/V2_STEP_<A|B|C>_HANDOFF.md`: what changed (files, one line
each), what is stub-tested, **what Mike must hardware-test**,
deviations, open issues. You have no hardware and no Bluetooth
stack: **never claim hardware verification.** Code must be
correct by construction and generous with journal logging —
hardware rounds happen locally against your handoffs.

### Step A — Multi-session core refactor (no behavior change)

- Sessions become per-device objects keyed by Bluetooth address;
  the glasses session is tenant #1, not a special case in the
  core. `Profile`'s NewConnection stops hard-refusing unknown
  devices: fingerprint match → glasses personality; any other
  paired device → routed to a session slot that (in this step)
  cleanly refuses service with a logged reason — Step B gives it
  the generic personality.
- The glasses path (including Discord mode) must be semantically
  untouched: same AT exchange, same node names, same control
  replies for existing verbs. `glasses-mic status` keeps its
  current output for the glasses and may add per-device sections
  after it.
- Drive-by fix: the comment in `51-glasses-ag.conf` claims the
  HSP role "is what another headset can fall back to for its
  microphone." That fallback was measured **not** to materialize
  on the reference build — correct the comment to state the role
  assignment without promising the fallback.
- Stub tests: session routing and fingerprint selection logic,
  with the D-Bus/BlueZ boundary faked, in `tests/` (new, small,
  pytest). No test may require Bluetooth, PipeWire, or root.

### Step B — Generic personality (spec-driven AG)

Implement the standard HFP Audio Gateway for generic sessions,
to the HFP 1.8 specification rather than to any vendor:

- SLC: standard AG feature bitmap (codec negotiation, HF
  indicators, eSCO), standard 7-indicator CIND set, +CMER,
  +CHLD, and +CLCC kept truthful.
- Codec negotiation per the headset's +BAC offer: mSBC when
  offered, CVSD fallback. (Ordinary headsets accept ordinary
  CVSD SCO proposals; the glasses' refusal of them is a glasses
  quirk — do not generalize it.)
- Headset-initiated vocabulary: +BCC, +BLDN, +BVRA, +CKPD,
  +VGS/+VGM volume sync, +BIEV and CIND battery reporting,
  +BIND/+BIA for HF indicators. Unknown AT commands get a clean
  ERROR, never a hang or a dropped SLC.
- Reuse the proven codec/SCO/FIFO helpers from the glasses path;
  do not fork them.

### Step C — Dynamic provisioning, activation, control surface

- **Dynamic nodes:** static conf pairs don't scale. Per-device
  FIFOs in the runtime dir; PipeWire tunnel modules loaded at
  session start via `pw-cli load-module` (source/sink named per
  Ruling 1), destroyed at session end. The glasses keep their
  static conf pair. Every module load follows ifexists/nofail
  discipline: a provisioning failure degrades that one device,
  never the audio stack.
- **Per-device A2DP discipline:** a generic device's card is
  parked to its call profile during its call state and restored
  at call end — the same discipline as the glasses path,
  applied per device.
- **Activation triggers:** SCO opens when (a) the headset asks
  (+BCC / button), or (b) an app attaches to the device's tunnel
  nodes (Ruling 3's pactl watch). SCO closes when neither holds.
  No timers, no VAD for headsets. Enforce Ruling 2 at audio-open.
- **Control surface:** `glasses-mic` verbs and the widget grow
  per-device awareness: status per device; start/stop target a
  device (default: the glasses). Existing glasses behavior of
  every verb is unchanged.
- **README:** add the plain statement — while glasses-ag is
  installed it serves HFP headsets; uninstalling restores
  WirePlumber completely. Note the one-call-at-a-time policy.

## Out of scope

HSP anything; changes to the glasses personality or Discord
mode; concurrent audio calls; battery reporting for the glasses
path (already handled there); license choice and the public
flip; new dependencies beyond the Python standard library and
the D-Bus/PipeWire tooling the daemon already uses.

## Acceptance (hardware rounds are local, after the cloud steps)

- Glasses regression pass unchanged: ready → live → Discord mic
  test both ways → stop restores media.
- Each inventoried headset: connects, completes SLC, opens
  audio, mic + call audio usable in Discord, clean teardown,
  A2DP restored.
- A device the daemon cannot serve fails cleanly: logged
  refusal, no wedged state, no effect on other devices or the
  audio stack.

Failures found in hardware rounds become quirk-table entries,
not redesigns — leave the fingerprint/quirk seam obvious in the
code for that purpose.
