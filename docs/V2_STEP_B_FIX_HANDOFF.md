# v2 Step B fix handoff — glasses SCO target with a second device present

Branch `v2-generic-hfp`. **Not hardware-verified.** Please read the
"Root cause" section carefully: I could **not** reproduce the reported
wrong target from the code, and I am not claiming the root cause is
found.

## Reported symptom

Corsair HS55 holding an SLC; a glasses Discord tap ran call setup fine,
then the SCO connect went to the Corsair's address (`[Errno 38]`, then
`[Errno 111]` after retries). Disconnecting the Corsair restored
pickup.

## Root cause — what I established and what I did not

**The sorted-order lead is not the mechanism.** The only `sorted()` over
registry records is `DeviceRegistry.status_sections()`, which builds
the extra `glasses-mic status` lines — display only. Nothing in the
audio path reads the registry at all. (The Corsair's address does sort
first; that only affects the order of status lines.)

**What the audio path actually read** was a single controller-wide
scalar, `AGCore.device_address`:

- `Session._open_sco_worker` (`glasses_ag.py`): `address =
  self.core.device_address or GLASSES_MAC` — the SCO `connect()` target.
- `AGCore.CARD` (used by `park_a2dp`, `restore_a2dp`, `reconcile_a2dp`,
  `cmd_media`, `_card_profile`): the same scalar.

That is the wrong *kind* of source — shared state that outlives
sessions, rather than the session's own device — and it is exactly the
class of assumption that only becomes reachable once more than one
device is connected. But **by the code as pushed it is only ever written
by the glasses branch of `Profile.NewConnection`** (the generic branch
returns before it). I traced both connection orders with the Corsair
(sorting first) and the glasses registered: `core.device_address` is the
glasses' address either way, and the new two-record tests pass on the
pre-fix code for the plain case. So I cannot show, from the repository,
a path by which the Corsair's address reaches the SCO `connect()`.

Possibilities I could not rule out from here (all need hardware
evidence):
1. The daemon running on the machine differs from this branch (stale
   install of `glasses_ag.py`, or another writer of `device_address`).
2. The `connect()` did target the glasses, and the Errno 38 / 111 came
   from the kernel/BlueZ SCO layer while a second ACL link (the
   Corsair's) was up. The reported symptom would look identical in the
   journal because the old log lines did not print the target address.

**The next hardware round settles this**, because the SCO log lines now
carry the address (below). If the logged target is the glasses and it
still fails with the Corsair connected, the cause is below the daemon
(then `btmon` during the tap is the next evidence) and Step C should
treat concurrent-ACL SCO as a separate problem.

## What changed (defense in depth, same spirit)

- `Session._open_sco_worker`: target is `self.address` (the session's
  own device); `core.device_address` is only a fallback for a session
  built without an address. New journal line `SCO open: target <addr>`,
  and the refused/failed lines now name the address.
- `AGCore.CARD`: prefers the live glasses session's own address over the
  shared scalar (falls back to the scalar between sessions, so
  `stop`/`media` after a disconnect behave as before).
- Behavior for the glasses alone is unchanged: with one glasses session
  the values are identical. Nothing else in Step B changed.

## Audit of the rest of the audio path

- **Fixed:** SCO connect target, A2DP card selection (above).
- **Safe as is:** `GenericSession` addresses everything by its own
  `self.address`; its audio refusal counts against its own record.
  `ControlRouter` verbs target the glasses controller only. `core.battery`
  is written only by glasses sessions; the generic one keeps its own
  `hf_battery`. `core.session` is replaced only by the glasses branch,
  and `Session.run`'s teardown is guarded by `core.session is self`.
- **Shared and glasses-only by design — flag for Step C (not changed):**
  `core.decoder` / `core.encoder` are stateful libsbc contexts and
  `core.pipe` / `core.spk` are the glasses' static FIFOs. A generic
  device must get its **own** codec contexts and FIFOs (the brief's
  per-device provisioning) or it will corrupt the glasses' stream.
  Also, `GenericSession.audio_opener` must target its own
  `self.address`, never `core.device_address`.

## Tests

`python3 -m pytest -q tests` → **66 passed** (the prior 61 plus 5):
glasses audio-open targets the glasses with two records, both
connection orders (Corsair sorting first); audio-open ignores a
deliberately contaminated `core.device_address`; A2DP card follows the
glasses session; a generic `AT+BCC` never touches glasses state. With
the fix reverted, the contaminated-address and card tests fail
(2 failed) — the plain two-record tests pass either way, consistent
with the finding above.

## Hardware checklist (Mike)

1. Corsair connected (SLC up), then a glasses Discord tap. Journal
   should show `SCO open: target <glasses addr>`. **Paste that line**
   plus the result.
2. If it still fails with the Corsair connected: capture `btmon` during
   the tap and note the Errno; also try with the Corsair connected but
   idle vs. playing A2DP.
3. Regression: glasses alone, tap → live → close, A2DP restored.
