# v2 Step B handoff — generic HFP personality (SLC layer)

Branch `v2-generic-hfp`, on top of Step A. **Nothing here is
hardware-verified.** I had no Bluetooth or PipeWire; the code is
stub-tested against a fake RFCOMM socket only. Step C is not started.

## Files changed

- `hfp_generic.py` (new) — `GenericSession`: the spec-driven AG for
  non-glasses devices. Pure Python (no D-Bus), so it is testable.
- `hfp_registry.py` — generic devices are no longer refused at connect
  (`GENERIC_NOT_IMPLEMENTED` removed). Only a device whose BlueZ
  identity is unreadable (address `?`, no usable session key) is still
  refused at connect, with a logged reason. New
  `DeviceRegistry.note_audio_refusal()`, `audio_refusals` /
  `last_audio_reason` on `DeviceRecord`; `status_sections()` appends the
  session's `status_fields()` and the audio-refusal counters to a
  device's line.
- `glasses_ag.py` — `Profile.NewConnection` builds a `GenericSession`
  for generic admissions (closing a superseded live one on reconnect).
  **`AGCore`, `Session` and everything glasses-side are untouched**
  (diff is `Profile`, an import, and docstring/log text).
- `install.sh` — installs `hfp_generic.py` (**without it the daemon
  will not start**); final echo corrected (HSP drive-by, below).
- `README.md` — HSP-fallback claim corrected (drive-by).
- `tests/test_generic.py` (new, 34 tests), `tests/test_glasses_regression.py`
  (new, 3), `tests/test_routing.py` (updated: generic is now served, the
  refusal assertions moved to the unreadable-device case).

## Drive-by fix (promised in Step A review)

README "Behavior and limitations" and the `install.sh` final echo no
longer say other headsets "can fall back to HSP". Both now match
`51-glasses-ag.conf`: HSP-only headsets stay WirePlumber's; the HSP
fallback for HFP headsets was measured not to materialize.

## What the generic personality does

AT exchange per HFP 1.8, written to the spec, not to any vendor:

| Command | Behavior |
|---|---|
| `AT+BRSF=` | `+BRSF: 3648` = enhanced call status (6), codec negotiation (9), HF indicators (10), eSCO S4 (11). Deliberately **not** claimed: 3-way calling, EC/NR, voice recognition, in-band ring, voice tag, reject call, enhanced call control, extended errors. |
| `AT+BAC=` | stores the offer; plan logged. mSBC if headset offered it **and** both sides negotiate codecs, else CVSD. |
| `AT+CIND=?` / `?` | standard 7 indicators in order; idle values `1,0,0,0,5,0,5` (`CIND_IDLE`). |
| `AT+CMER=3,0,0,<0\|1>` | anything else → ERROR. SLC is declared up after CMER, or after `AT+BIND?` when both sides negotiated HF indicators. |
| `AT+CHLD=?` | `+CHLD: (0)` (no 3WC claimed). `=0` OK; other values ERROR. |
| `AT+CLCC` | OK with nothing. The call table is always empty; no command creates a call. |
| `AT+BIA=` | per-indicator mask honored by `set_indicator()` (+CIEV). |
| `AT+BIND=` / `=?` / `?` | AG supports (1 safety, 2 battery); reports the intersection with the headset's list. |
| `AT+BIEV=` | battery (0-100) and safety (0/1) stored; unlisted/invalid → ERROR. Battery shows as `hf_battery=` in status. |
| `AT+VGS=` / `AT+VGM=` | 0-15 stored and logged; else ERROR. |
| `AT+CKPD=200` | OK + log (no call to act on); other args ERROR. |
| `AT+BLDN`, `AT+BVRA=` | ERROR + log (nothing dialed; VR not offered). |
| `AT+BCC` | audio-open request, **refused** (below). |
| `AT+BCS=` | only valid after an AG offer; otherwise ERROR. |
| `AT+CLIP/CCWA`, `AT+COPS=3,0`, `AT+COPS?` (`+COPS: 0`), `AT+CNUM` | benign, truthful (no operator, no numbers). |
| everything else (ATA/ATD/CHUP with no call, NREC, CMEE, XAPL, IPHONEACCEV, junk) | clean `ERROR`; the link stays up. A handler exception also yields ERROR, never a dead thread. |

Stray `+...` lines are ignored; oversized unterminated input is
dropped; socket close runs the normal teardown and clears the slot.

## The audio boundary (Step B)

Every audio-open goes through `GenericSession.request_audio(source)`,
which refuses, logs `audio-open refused (<source>): generic audio path
not implemented yet (v2 Step C); SLC only`, and bumps
`audio_refusals` (visible as `audio_refusals=N audio_reason="…"` on the
device's `glasses-mic status` line). Step C replaces
`GenericSession.audio_opener`. `begin_codec_offer()` (sends `+BCS`) and
the `AT+BCS` confirmation path are built and tested but nothing in
Step B calls them — they are the Step C hook.

## Deviations (with reasons)

- **`AT+BCC` is answered `ERROR`, not `OK`.** An `OK` followed by no
  `+BCS` would leave the headset waiting; `ERROR` is the deterministic
  "not now". If a headset reacts badly (e.g. retry storm in the
  journal), this is the first thing to revisit.
- **No 3-way-calling claim**, so `+CHLD=?` is `(0)`. Truthful for a
  gateway with no calls, but a picky headset might want the full list;
  `AG_BRSF` / the `AT+CHLD` branch are the places to change.
- **Headset's own SCO attempt:** the daemon opens no SCO listener, so
  the kernel — not this code — declines an incoming SCO; nothing is
  logged by the daemon for it. I could not verify what a Corsair does
  on that. `btmon` or the headset's behavior on hardware will tell.
- **`CIND_IDLE` signal=5/battchg=5** are the gateway-link's idle
  values (no cellular radio, mains-style). A judgment call, easy to
  change.
- **Registry still refuses the unreadable-device case at connect** —
  Step A's slot mechanism, reused, since there is no key to hold a
  session under.
- **`AT+NREC`, `AT+CMEE`, Apple `XAPL`/`IPHONEACCEV` answer ERROR.**
  The glasses script accepts the Apple ones (a glasses quirk, kept
  there); not generalized, per the brief. If a headset wants them, add
  a quirk entry, not a rewrite.
- Generic code does not import from `glasses_ag` (that module needs
  `dbus`/`gi`); the CIND/OK/ERROR constants are redefined in
  `hfp_generic.py`. Codec/SCO/FIFO helpers are not touched yet since
  no audio is opened in this step; Step C should reuse them in place.

## Stub-test results

`python3 -m pytest -q tests` → 61 passed (24 Step A updated + 34
generic + 3 glasses regression). Covered: full scripted SLC with
indicators and idle values, SLC gating on the HF-indicator exchange,
CMER/BIA/+CIEV masking, CHLD, truthful empty `+CLCC` and no-fake-call
sweep, BIEV/BIND/VGS/VGM/CKPD/BLDN/BVRA, mSBC offered/accepted, mismatch
and unsolicited `BCS`, CVSD-only and non-negotiating headsets, AG
without mSBC, `AT+BCC` audio refusal (logged, counted, link up),
unknown AT → ERROR, handler crash survival, oversized input, send
errors, teardown clearing the slot. Glasses regression (dbus/gi
faked, real `glasses_ag` imported): the Samsung-replica replies are
byte-identical, a glasses-fingerprint connection still builds the
frozen `Session` on the shared controller, a headset builds
`GenericSession` without touching it, an unreadable device is still
`org.bluez.Error.Rejected`, and a generic reconnect closes the old link.

## Hardware checklist — local Corsair-first round (Mike)

Do `./install.sh`, restart the service, then:

1. **Glasses first (regression):** reconnect glasses → `routing … ->
   glasses`, `SLC established`; `glasses-mic status` first line
   unchanged; toggle/Discord as in the Step A checklist.
2. **SLC connect:** connect the Corsair HS55. Expect `routing <addr>
   (Corsair HS55…) -> generic`, `generic session start`, the `HF <<`
   / `HF >>` exchange, then `SLC established … pick=…`. It must **stay
   connected** (no release within seconds). Check `pick=` — mSBC vs
   CVSD should match what the headset offered in `AT+BAC`.
3. **Status:** `glasses-mic status` gains `device <addr> … personality=
   generic link=up codec=…` (and `hf_battery=` if it reports); glasses
   line first and unchanged; the Waybar widget unaffected.
4. **Indicator behavior:** headset's own UI/LED/voice prompts: does it
   think it has a network/service? Any "no service"/error prompts? Any
   ERRORs in the journal for commands you'd expect it to need? Battery
   report appears in the journal as `headset battery: N%`.
5. **Button / volume:** press the multifunction button (expect
   `AT+CKPD=200` or `AT+BLDN`/`AT+BVRA` in the journal, answered as
   above); change volume if it has controls (`VGS`/`VGM` logged).
6. **Audio-open refusal:** trigger the mic/call button path. Expect
   `audio-open refused (…)` and `audio_refusals=N` in status, SLC
   still up, no loops/retry storm, nothing wedged. Report what the
   headset itself does afterwards (retries? disconnects?).
7. **Clean teardown:** power off / disconnect the headset →
   `generic session end`, `session slot cleared`, status `link=down`;
   reconnect works; glasses unaffected throughout.
8. **A2DP behavior:** while the headset holds its SLC, music still
   plays to it over A2DP, and the card profile is not changed by the
   daemon (Step B never parks A2DP). Note whether the headset's own
   firmware changes anything when SLC is up vs previously (v1 gave it
   no HFP at all).
9. **Second headset (Sennheiser) if handy:** two headsets + glasses
   all holding SLCs at once (Ruling 2 allows unlimited connections).

## Open issues / for Step C

- Audio open: per-device nodes (Ruling 1 naming), `begin_codec_offer`
  → SCO, one-call-at-a-time (Ruling 2), pactl attach detection
  (Ruling 3), per-device A2DP park/restore.
- Hardware findings from item 2-6 above become quirk entries in
  `PERSONALITY_RULES` or branches in `GenericSession.handle`.
