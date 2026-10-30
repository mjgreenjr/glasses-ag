# v2 Step C handoff — dynamic audio provisioning for generic devices

Branch `v2-generic-hfp`, on top of the Step B fix. **Nothing here is
hardware-verified.** I had no Bluetooth, PipeWire, `pw-cli` or `pactl`;
everything is stub-tested at a fake boundary (pactl, pw-cli, SCO socket,
libsbc, clock and threads injected). `python3 -m pytest -q tests` →
**117 passed**. Several design points rest on PipeWire/BlueZ behavior I
could only recall, not check — they are listed under "Assumptions to
verify first" and are the first things the local round must settle.

The Step B fix (glasses SCO target = the initiating session's own
address, `SCO open: target <addr>` in the journal) is present on this
branch (`45a0ddc`) and Step C builds on it; its regression tests still
pass. Step C does not modify it.

## Files

- `hfp_audio.py` (new) — the whole generic audio path: `CallArbiter`
  (Ruling 2), `A2dpParker`, `TunnelSet` (FIFOs + pipe-tunnel modules),
  `ScoBridge` (SCO ↔ FIFOs), `GenericAudio` (per-session lifecycle and
  triggers), `AudioManager` (daemon-level owner + pactl attach watcher),
  `explain_sco_failure` (platform-constraint diagnostic),
  `kill_stale_tunnels`, `AudioDeps` (injection seam).
- `hfp_media.py` (new) — `MSBCDecoder`, `MSBCEncoder`, `SCOParser`,
  `PipeSource`, `SpkSink` **moved verbatim** out of `glasses_ag.py` so
  the generic path reuses them without importing D-Bus (see Deviations).
  Only additions: `stop()` on `PipeSource`/`SpkSink`, a `running` flag in
  `SpkSink`'s loop, and the `H2_HEADER_B` table.
- `hfp_generic.py` — `AT+IPHONEACCEV` accepted; audio hooks
  (`audio`, `audio_post_ack`, `slc_hooks`, `end_hooks`);
  `status_fields()` appends audio state; new `detail_fields()`; OK is
  sent to `AT+BCC` before the codec offer; bare-session refusal text
  reworded (no longer says "Step C").
- `hfp_registry.py` — `live_peers()`, `device_line(rec, detail)`,
  `resolve(selector)`; `ControlRouter` gains `status|start|stop|toggle
  <device>`.
- `glasses_ag.py` — imports the moved helpers; `AGCore` gets `arbiter`
  and `sco_diagnoser` slots and `cmd_start` consults the arbiter;
  `Session._open_sco_worker` calls the diagnoser on a failed connect;
  `Profile` takes an optional `AudioManager` and attaches it to generic
  sessions (failure there is logged and never blocks the SLC); `main()`
  builds the manager. Nothing else on the glasses path changed.
- `glasses-mic` — optional second argument for `status|start|stop|toggle`.
- `install.sh` — installs `hfp_media.py` and `hfp_audio.py` (**without
  them the daemon will not start**); final echo updated.
- `README.md` — "Other headsets" section (plain statement that the
  daemon is the HFP AG while installed and uninstall restores
  WirePlumber; triggers; one-call-at-a-time), "Known limitation"
  (platform constraint), troubleshooting bullet, uninstall list,
  requirements (`pw-cli`, `pactl`).
- `tests/test_audio.py` (new, 41 tests), `tests/test_generic.py`
  (+10 IPHONEACCEV tests; two Step B assertions updated).

## What was built

**Provisioning (Ruling 1).** On session start (in a worker thread, so
the SLC is never delayed), per device: FIFOs `…/glasses-ag/dev/<ADDR>/
{mic,spk}.fifo`, `PipeSource`/`SpkSink` on them, and two
`pw-cli -m load-module libpipewire-module-pipe-tunnel '<args>'`
children — a source described **"<Name> Mic"** and a sink **"<Name> Call
Audio"** (same tunnel properties as the static 52/53 conf pair; names
sanitized before they reach the SPA-JSON args). `node.name` is a slug
(`corsair_hs55_mic` / `_call`), with the last 4 address digits appended
only if another live device already has that slug. Success is judged by
the nodes **appearing in `pactl list short sources/sinks`** (up to 6 s),
not by parsing pw-cli output. Teardown (session end, supersede by a
reconnect, or failed provisioning) terminates the children, stops the
FIFO threads, unlinks FIFOs. A superseding session shuts the old
instance down **first** so names/FIFOs never collide. At daemon start
`reset_runtime()` reaps stray `pw-cli` tunnel processes from a killed
daemon and clears `dev/`. The glasses keep their static conf pair.

**Degradation (ifexists/nofail).** Every failure is returned, never
raised: `pw-cli` missing, exiting early, nodes not appearing, FIFO
errors, any unexpected exception. The device keeps its SLC and A2DP;
audio-open is refused with `audio provisioning failed: <why>` (journal +
`audio_refusals`/`audio_reason` on its status line; `audio_nodes=failed`).
Re-provisioning is retried on a later request, at most every 30 s.

**Audio-open (the Step B codec path).** All triggers funnel into
`GenericAudio.request(source)`: arbiter claim → park this device's A2DP
→ `begin_codec_offer()` (+BCS) → wait for `AT+BCS` (6 s handshake
watchdog, which is not a call timer) → SCO connect **to
`self.address`** with retries (8 × 0.25 s) → `ScoBridge`. For `AT+BCC` the
OK is sent first and the offer follows (`audio_post_ack`). A headset
without codec negotiation skips the offer and gets CVSD. Any failure
returns to idle through one `_finish()` that always releases the call
slot and restores A2DP **only if this device parked it**. Orphan
adoption (card on `off` that this process did not park) runs at SLC.

**Triggers.** (a) headset `AT+BCC`; (b) app attach — an `AudioManager`
thread polls `pactl` once a second and treats a source-output on the
device's Mic source or a sink-input on its Call Audio sink as
"attached" (Ruling 3); (c) `glasses-mic start <device>`. Close: SCO
dropped by the headset; or app detached (3 consecutive polls) when
neither the headset (BCC) nor a manual start also holds it. No timers,
no VAD. A hang-up while an app is still attached does **not** re-open
(edge-triggered); a *refused* attach retries every 10 s so it opens once
the slot frees.

**Ruling 2.** `CallArbiter`: the glasses count as holding the call
while `AGCore.state ∈ {starting, live}` (read, never stored — a missed
release cannot wedge it); generic devices claim/release explicitly.
Check-and-set is under the arbiter's lock on both sides
(`cmd_start`: core.lock → arbiter.lock; generic: arbiter.lock alone).
Refusals are logged, counted (`audio_refusals`), and visible on the
refused device's status line — for the glasses too.

**Control surface.** Old verbs and replies are byte-identical (tested).
New: `status <dev>` (detail line: hf_features, codecs, gains, node
names, holds, codec, `audio_error`), `start|stop|toggle <dev>`. `<dev>`
= Bluetooth address (any case), exact name, or a unique
case-insensitive name substring; `glasses` selects the glasses. Errors
are `err:` lines (unknown/ambiguous device, not connected, busy).

**IPHONEACCEV.** `AT+IPHONEACCEV=<n>,<key>,<val>,…` → OK; key 1 stored as
`hf_battery` (×10, like the glasses side), key 2 ignored; malformed →
ERROR. Logged as "IPHONEACCEV dialect accepted", not gated by any quirk
table. `XAPL` and other vendor commands still get ERROR.

**Platform constraint (accept and document).** README "Known
limitation" states it plainly. In code: when a SCO connect fails (glasses
path via `Session._open_sco_worker`, generic via `_open_sco`) and any
other device holds a link, the journal line reads `SCO connect failed
(…) while another device is connected: <names>. Likely cause: on a
single Bluetooth adapter a connected second device can make the
controller refuse the call-audio (SCO) link — observed on a Realtek
adapter (HCI status 0x1F, Errno 38/111), may vary by adapter; disconnect
or power off the other device(s) and retry`, and the same cause is
stored as `audio_reason` on the failing device's `status` line. With no
other device connected the log says it is *not* the multi-device
constraint. No SLC-release machinery was added.

## Assumptions to verify first (highest risk)

1. **`pw-cli -m load-module` keeps the module alive while the process
   runs.** I believe `pw-cli` loads modules into its *own* context (so a
   plain `pw-cli load-module …` unloads on exit) and that `-m` keeps it
   running. If wrong, provisioning reports "pw-cli exited before the
   tunnel nodes appeared" and every generic device degrades (daemon
   fine). Quick manual check before anything else:
   ```bash
   mkfifo /tmp/t.fifo
   pw-cli -m load-module libpipewire-module-pipe-tunnel \
     'tunnel.mode=source pipe.filename="/tmp/t.fifo" node.name="t" node.description="T" audio.format="S16LE" audio.rate=16000 audio.channels=1'
   ```
   In another terminal `pactl list short sources | grep -w t` must show
   it; Ctrl-C in the first must remove it. Report the output if not.
2. **Argument quoting:** the args go in as one argv string of
   `key=value` pairs with JSON-quoted values (as above). Names with
   spaces rely on it.
3. **pactl names:** the attach watcher assumes the tunnel nodes appear in
   `pactl list short sources|sinks` under `node.name`, and that stream
   rows are `id<TAB>source-or-sink-index<TAB>…`.
4. **mSBC on generic headsets** reuses the glasses' proven socket
   setting (`BT_VOICE` 0x0063) and H2 framing (`01 C8/F8/08/38`).
   Whether an ordinary headset wants exactly that is unverified.
5. **CVSD is the least-tested path** (8 kHz resample ↔ 16 kHz nodes,
   downlink chunked at the SCO MTU read via `SCO_OPTIONS`). Only
   exercised if a headset does not offer mSBC.
6. **Realtek constraint applies to generic SCO too (my expectation).**
   If any second device blocks the *glasses'* SCO, expect a generic
   headset's SCO to fail the same way while the glasses (or another
   headset) are connected. The checklist is ordered around that.

## Deviations (with reasons)

- **Shared helpers moved to `hfp_media.py`** rather than imported from
  `glasses_ag`: `glasses_ag` needs `dbus`/`gi` (untestable here) and runs
  as `__main__`, so importing it from another module would load it twice.
  Classes are unchanged byte-for-byte except the additions above; the
  glasses code is a pure `from hfp_media import …`. The glasses'
  uplink/downlink *loops* (entangled with cue_hold/Discord/core) are left
  untouched; `ScoBridge` is a sibling that shares the codec classes,
  parser and framing table. Same for A2DP: `A2dpParker` re-implements
  AGCore's invariants per device rather than refactoring AGCore.
- **`pw-cli` instead of config flags:** `ifexists`/`nofail` are
  `context.modules` config-file flags with no `pw-cli` equivalent; the
  same discipline is implemented as "check, never raise, tear down, keep
  the device's SLC/A2DP".
- **`cmd_start` can now say `err: call audio busy: … (one call at a
  time)`** — a new reply, but only when a generic call is live (Ruling 2
  requires refusing it). With nothing competing, glasses behavior is
  unchanged. A Discord tap in that situation logs the refusal.
- **Glasses status line unchanged.** Glasses-side audio failures appear
  on the glasses *device* line (`audio_refusals`/`audio_reason`), not the
  first status line, to keep the first line byte-identical.
- **BCC-opened calls stay open until the headset drops them** (or `stop
  <dev>`): "no timers" leaves nothing else to end them. A headset that
  never releases keeps its mic open.
- **Widget (`glasses-waybar`) not extended.** The work order listed the
  control surface only; the widget still reads just the first status
  line, so it is unaffected.
- **`AT+BLDN`/`AT+BVRA`/`AT+CKPD` unchanged** from Step B (ERROR/OK as
  before); a button press is not turned into an audio-open — only `AT+BCC`
  is. If a headset opens audio via CKPD or BLDN, that is a quirk entry.

## Hardware checklist (local round, Mike)

Install with `./install.sh`, restart the service. Keep a terminal on
`journalctl --user -u glasses-ag -f`. Do the manual `pw-cli -m` check
above **first**; stop if it fails. Because of the Realtek constraint,
the order below keeps a second connected device out of the way whenever
a SCO connect is expected to succeed.

**A. Glasses regression with a generic SLC up** (the Step B fix repro,
kept as a regression item)

1. Glasses alone: connect, `glasses-mic status`, `toggle` → live → `toggle`
   → A2DP restored; `discord_on`, tap, speak, silence closes with the
   chirp (as in the Step A checklist). First status line unchanged.
2. Connect the Corsair (SLC up). Journal: `routing … -> generic`, `SLC
   established`, `loading tunnel: 'Corsair HS55 Mic'…`, `tunnel nodes up`.
   `pactl list short sources | grep corsair` and `sinks`; `pgrep -a pw-cli`
   shows two; `glasses-mic status` gains the Corsair line with
   `audio=idle audio_nodes=ok`. Battery appears as `hf_battery=` with an
   `IPHONEACCEV dialect accepted` log line, no ERROR to it.
3. **Discord tap with the Corsair connected.** Expected on the Realtek
   adapter: journal `SCO open: target <glasses addr>` (the Step B fix —
   paste it), then `SCO connect … refused` / `failed after retries`
   (Errno 38 then 111), **then the new `while another device is
   connected: Corsair HS55 … Likely cause: …` line**, and the glasses
   device line in `glasses-mic status` shows `audio_refusals=1
   audio_reason="SCO connect failed …likely an adapter limit…"`. State
   returns to `ready`, A2DP restored, glasses link and Corsair untouched.
4. Power the Corsair off → tap again → glasses work (cure confirmed).
   Pw-cli processes and nodes for the Corsair are gone (`pgrep -a pw-cli`,
   `pactl list short sources`), `ls $XDG_RUNTIME_DIR/glasses-ag/dev` empty.
5. If on this adapter the glasses call **does** succeed with the Corsair
   connected, say so — the constraint is adapter-specific and that is
   useful.

**B. Generic audio, both ways** (glasses **off or disconnected**, other
headsets off — one device connected)

6. Connect the Corsair. In Discord choose input **Corsair HS55 Mic** (and
   output **Corsair HS55 Call Audio** to test downlink). Expected: `app
   attached to Corsair HS55 tunnel nodes`, `audio-open accepted (app
   attach)`, `parked A2DP`, `offering codec 2`, `codec 2 confirmed`, `SCO
   open: target <corsair addr>`, `SCO open — Corsair HS55 live (mSBC …)`.
   Speak: mic level moves in Discord. Have someone speak in the call
   (or play audio into the Call Audio sink): it is heard in the headset.
   Report anything robotic/clipped/delayed and the actual codec used.
7. **Park/restore:** while live, `pactl list cards` shows the card
   `Active Profile: off`; `glasses-mic status corsair` shows
   `audio=live a2dp_parked=true`. Close the input in Discord → within ~3 s
   `app detached`, `call ended`, `restored A2DP`; profile back to
   `a2dp-sink`, music resumes.
8. **Headset-initiated:** press the Corsair's call/mic button. Expect
   `AT+BCC` → OK → `+BCS` → live. Record whether the HS55 sends `AT+BCC`
   at all; if it opens audio some other way (CKPD/BLDN/its own SCO), copy
   the exact journal lines — that becomes a quirk entry.
9. `glasses-mic start corsair` / `stop corsair` / `toggle corsair` work;
   `glasses-mic status corsair` prints the detail line.
10. Orphan adoption: with the call live, `systemctl --user kill -s KILL
    glasses-ag` (card stays `off`; stray pw-cli children remain), then let
    systemd restart it and reconnect the Corsair: expect `reaped stale
    pw-cli tunnel process` and `adopted orphaned A2DP park — restored media
    mode`.

**C. One call at a time and second-device refusal**

11. Corsair connected alone with a call **live** (step 6). Now connect the
    Sennheiser (connecting while a SCO is already up may be fine on this
    adapter — report if it drops the call). Open audio on it (`glasses-mic
    start sennheiser`, or its button / an app): expect `audio-open refused …
    call audio busy: Corsair HS55 (…) holds the call (one call at a time)`,
    `audio_refusals=1` on its status line, Corsair call undisturbed,
    Sennheiser SLC still up.
12. With the Corsair call live, connect the glasses and `glasses-mic start`:
    expect `err: call audio busy: Corsair HS55 (…)`, state `ready`.
13. Reverse: end the Corsair call; start a glasses call (glasses alone); a
    generic headset connecting afterwards and asking for audio → refused
    with `the glasses hold the call`.

**D. Failure and teardown**

14. Provisioning failure: `chmod -x $(which pw-cli)` (or rename it),
    reconnect the Corsair → `audio provisioning FAILED: pw-cli
    unavailable…`, `audio_nodes=failed`; SLC stays up; A2DP music still
    works; asking for audio is refused (counter increments); daemon and
    audio stack fine. Restore `pw-cli`, wait 30 s, ask again → provisions and
    works.
15. Disconnect the Corsair (not just power-off): `generic session end`,
    `tunnel nodes removed`, nodes/children/FIFO dir gone, A2DP restored if
    a call was live, slot released (`glasses-mic start` on the glasses is
    no longer refused). Reconnect → fresh nodes, same names, no duplicates
    in `pactl list short sources`.
16. Fast reconnect while live (power-cycle the headset): old audio shut
    down before the new session provisions; no duplicate nodes.

## Open issues

- Attach detection counts **any** stream on the nodes — a mixer's level
  meter or an always-on voice app will hold the headset in call mode.
  Whether that bites in practice is a hardware-round observation; the
  seam is `PactlSnapshot.attached()`.
- If the pactl polling proves flaky, Ruling 3 says revisit with
  PipeWire-native graph watching later.
- Hardware findings become `PERSONALITY_RULES` entries or branches in
  `GenericSession.handle`, per the brief — not redesigns.

---

# Addendum — fixes from the local hardware round (Oct 30)

Step C was verified end to end on hardware; four follow-ups, all on
`v2-generic-hfp`. `python3 -m pytest -q tests` → **123 passed**. The new
code paths are stub-tested only; re-check the items below on the next
local pass.

1. **Stale `audio_reason` on the status line** (`hfp_registry.py`,
   `hfp_audio.py`). Each recorded refusal now remembers which devices its
   text names and why (`last_audio_peers`, `last_audio_kind`). The reason
   is judged *at render time*, so no release/disconnect path can forget to
   clear it: a "call audio busy" reason is current only while a named
   device still holds the call (the registry asks the arbiter via
   `call_holders`; the glasses count while `state ∈ starting|live`); a
   failed-SCO reason is current only while a named device still has a link
   up. While current it prints as `audio_reason="…"`; afterwards the same
   text prints as **`last_refusal="…"`** (labelled history) and the
   cumulative `audio_refusals=N` is untouched. A reason naming nobody
   (e.g. provisioning failed) stays `audio_reason` until a later outcome.
   The glasses' first status line is unchanged. *Re-check:* repeat the
   HD 450BT scenario — after it disconnects, the glasses device line shows
   `last_refusal=` and no `audio_reason`.
2. **README — SCO known limitation** rewritten in its characterized form:
   with a guest SLC up the *first* glasses call succeeded and subsequent
   ones were refused (Errno 38/111) until the guest disconnected;
   state-dependent, "observed on a Realtek adapter". The journal hint text
   in code (`PLATFORM_HINT`) was left as is ("can make the controller
   refuse") — still true, not narrowed to the new wording. Say if you want
   it brought in line.
3. **`AT+BIA` accepted** (`hfp_generic.py`). Step B already handled
   well-formed BIA, but ERRORed on forms it did not anticipate (more than 7
   fields, non-0/1 tokens). I could not tell from the report which form
   the headset sent, so it is now deliberately lenient: the activation
   state for the first seven indicators is recorded, extra fields and
   non-0/1 tokens are logged and ignored, and the reply is always OK.
   Tests that asserted ERROR for those inputs were changed to assert OK.
   *Re-check:* if the journal shows the exact BIA line the headset sent
   (the `past 7` / `not 0/1` log lines will appear), paste it.
4. **README — lazy HFP on reconnect** documented under "Headset reconnect
   notes": Corsair HS55 (and a Sennheiser pairing variant) may return
   A2DP-only after a host-side reconnect; a power-cycle restores the
   session; "observed on" wording; the daemon is a bystander.

Nothing else was touched.
