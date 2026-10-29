# v2 Step A handoff — multi-session core refactor

Branch `v2-generic-hfp`, off `main`. **Nothing here has been
hardware-verified**; I had no Bluetooth/PipeWire. Correct by
construction + stub-tested only.

## Files changed

- `hfp_registry.py` (new) — pure-Python routing: `DeviceInfo`, the
  fingerprint rule list `PERSONALITY_RULES` (the quirk seam),
  `DeviceRegistry` (sessions keyed by Bluetooth address),
  `RefusedSlot`, and `ControlRouter` (control-socket verbs).
- `glasses_ag.py` — `Profile.NewConnection` no longer hard-refuses by
  name: reads `DeviceInfo`, asks the registry, glasses → existing
  `Session`, anything else → refusing slot. `Session` gets
  `address`/`registry` and clears its slot on end. `control_server`
  delegates to `ControlRouter`. `AGCore` is untouched (see Deviations).
- `glasses-mic` — reads the full reply (status can now be multi-line);
  usage text mentions the device lines.
- `glasses-waybar` — parses only the first status line, so the new
  device lines can't clobber its key=value scrape.
- `51-glasses-ag.conf` — comment no longer promises an HSP fallback.
- `install.sh` — also installs `hfp_registry.py` next to the daemon
  (it is imported at startup; **without this the daemon won't start**).
- `tests/conftest.py`, `tests/test_routing.py` (new) — 24 stub tests.
- `docs/V2_STEP_A_HANDOFF.md` — this file.

## Stub-test results

`python3 -m pytest -q tests` → **24 passed**. Covers: name-prefix /
alias / case-insensitive / address-pin fingerprint selection (pin is
decisive both ways, as in v1), custom quirk rule ordering, generic
routing + refusal-with-logged-reason, unpaired/unreadable devices not
raising, address keying, attach/detach incl. stale-session safety, a
refused headset not disturbing the glasses slot, control verbs routing
to the glasses controller with unchanged replies, and `status` shape.
Additionally smoke-run (not committed) with faked `dbus`/`gi`:
`NewConnection` for a headset raises `org.bluez.Error.Rejected` with
the logged reason. `glasses_ag.py` compiles; no test touches Bluetooth,
PipeWire, or root.

## What Mike must hardware-test (full glasses regression)

Install fresh with `./install.sh` (it now copies `hfp_registry.py`),
restart the service, then:

1. **Ready:** reconnect glasses → journal shows
   `routing <addr> (Meta Glasses…) -> glasses`, `session start`,
   `SLC established`; `glasses-mic status` first line is
   `ok: state=ready glasses_connected=True call=idle …` exactly as
   before, followed by one `device <addr> … personality=glasses link=up`
   line.
2. **Live:** `glasses-mic toggle` → two blips, `state=live`, mic node
   `glasses-mic` carries audio, same AT exchange in the journal.
3. **Discord mic test, both ways:** `discord_on`, temple tap opens
   mic, speech holds it, silence closes with the chirp (mic → app and
   app → glasses via `glasses-spk`); cap/defer behavior unchanged.
4. **Stop restores media:** `glasses-mic stop` / `media` → A2DP card
   back on `a2dp-sink`.
5. **Reconnect/teardown:** disconnect glasses → `session end: <addr>`
   then `session slot cleared`; reconnect works and status returns
   to `link=up`.
6. **Non-glasses device (Corsair HS55 etc.):** connect → journal shows
   `routing … -> generic` and `refusing HFP service for <addr>:
   generic HFP personality not implemented yet (v2 Step B)`;
   `glasses-mic status` gains a `link=refused … reason="…"` line;
   glasses keep working simultaneously; headset behavior is the same
   as v1 (A2DP only).
7. Waybar widget still shows battery/state as before (verify the
   tooltip with a refused headset present).

## Deviations

- **`AGCore` was not split.** It remains the glasses controller
  (state machine, Discord, A2DP choreography) and is the single
  shared target of existing verbs. "Glasses as tenant #1" is realized
  in routing/slot bookkeeping (registry keyed by address, glasses and
  generic both go through `admit`/`attach`/`detach`), not by moving
  its internals — I judged moving them riskier than the brief's
  "semantically untouched" allows. Step B/C will need a per-device
  controller seam; `DeviceRecord.session` is where it plugs in.
- **Refusal is still wire-level `Rejected`.** The slot is created,
  logged, and shown in status, but the fd is not taken: the headset
  sees the same failure as v1 (non-regression). Accepting then
  closing the RFCOMM would risk reconnect loops.
- **Pairing is logged, not gated** (`paired=` in the routing log),
  per "admission = BlueZ pairing."
- **New module** `hfp_registry.py` means the install now ships two
  files (needed so tests can import without dbus/gi).
- Two glasses addresses (no pin, both match the prefix): last
  connection wins on the shared controller, as in v1; now logged.

## Conflicts with the brief / open issues

- **README.md (lines ~176-178) and `install.sh` final echo** still say
  other headsets "can fall back to HSP". The brief says that fallback
  was measured not to happen. I left both (the brief assigns the
  README rewrite to Step C and Step A to the conf comment only) —
  please confirm, or have them fixed in Step B/C.
- The generic slot refuses for every non-glasses device, so until
  Step B ships, behavior for headsets equals v1.
