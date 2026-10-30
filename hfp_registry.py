"""hfp_registry — per-device session routing for glasses-ag.

Pure Python: no D-Bus, no BlueZ, no PipeWire, no root. glasses_ag.py
feeds it DeviceInfo records read from BlueZ and asks it two things:

  * which gateway *personality* a device gets (select_personality), and
  * where its session lives (DeviceRegistry, keyed by Bluetooth
    address).

Admission is BlueZ pairing/trust — there is deliberately no daemon-side
allowlist here. The fingerprint only selects a personality; it never
gates admission. The glasses personality is tenant #1 of the registry,
not a special case of it.

THE QUIRK SEAM: PERSONALITY_RULES below is the one place a device is
matched to a personality. Hardware-round findings ("this headset needs
X") become new rules in that list, not new branches elsewhere.
"""

import threading
import time

PERSONALITY_GLASSES = "glasses"
PERSONALITY_GENERIC = "generic"

# A device whose BlueZ identity could not be read has no usable key
# (address "?"), so it cannot hold a per-device session.
UNIDENTIFIED_DEVICE = ("device identity unreadable from BlueZ; cannot "
                       "key a session for it")


def _default_log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Device identity + fingerprinting

class DeviceInfo:
    """What the daemon knows about a device at NewConnection time."""

    def __init__(self, address, name="", alias="", paired=None,
                 object_path=""):
        self.address = (address or "").strip().upper() or "?"
        self.name = name or ""
        self.alias = alias or ""
        self.paired = paired            # True / False / None (unknown)
        self.object_path = str(object_path or "")

    @property
    def display_name(self):
        # Same precedence the v1 gate used: Name, else Alias.
        return self.name or self.alias

    def __repr__(self):
        return (f"DeviceInfo({self.address!r}, "
                f"name={self.display_name!r}, paired={self.paired!r})")


class FingerprintConfig:
    """Owner-supplied knobs (GLASSES_AG_ADDRESS / _NAME_PREFIX)."""

    def __init__(self, pinned_address="", name_prefix=""):
        self.pinned_address = (pinned_address or "").strip().upper()
        self.name_prefix = (name_prefix or "").strip()


# A rule returns a personality string, or None to abstain. The first
# rule that answers wins; no answer means PERSONALITY_GENERIC.

def rule_pinned_address(info, cfg):
    """An exact address pin is decisive in both directions: the pinned
    device is the glasses, and nothing else is (v1 semantics — with a
    pin set, the name prefix is not consulted)."""
    if not cfg.pinned_address:
        return None
    if info.address == cfg.pinned_address:
        return PERSONALITY_GLASSES
    return PERSONALITY_GENERIC


def rule_glasses_name_prefix(info, cfg):
    """Meta glasses advertise a name beginning with 'Meta Glasses'."""
    name = info.display_name
    if name and cfg.name_prefix and \
            name.casefold().startswith(cfg.name_prefix.casefold()):
        return PERSONALITY_GLASSES
    return None


PERSONALITY_RULES = [
    rule_pinned_address,
    rule_glasses_name_prefix,
    # <- per-device quirk entries from hardware rounds go here.
]


def select_personality(info, cfg, rules=None):
    for rule in (PERSONALITY_RULES if rules is None else rules):
        verdict = rule(info, cfg)
        if verdict is not None:
            return verdict
    return PERSONALITY_GENERIC


# --------------------------------------------------------------------------
# Per-device bookkeeping + the registry

class DeviceRecord:
    """Everything the daemon tracks for one Bluetooth address. The
    session is whatever object the personality built (it must expose
    .alive and .slc); None while the device is not connected."""

    def __init__(self, info, personality):
        self.address = info.address
        self.name = info.display_name
        self.personality = personality
        self.session = None
        self.connections = 0
        self.refusals = 0           # connect-time refusals
        self.last_reason = ""
        self.audio_refusals = 0     # audio-open refusals (generic)
        self.last_audio_reason = ""
        # What the reason names, so it can be judged stale later:
        # kind "busy" = a call holder, "sco" = other connected devices.
        self.last_audio_peers = ()
        self.last_audio_kind = ""
        self.last_event = time.time()

    def link(self):
        s = self.session
        if s is not None and getattr(s, "alive", False):
            return "up" if getattr(s, "slc", False) else "connecting"
        if self.refusals and not self.connections:
            return "refused"
        return "down"


class RefusedSlot:
    """A slot that cleanly refuses service at connect time, with the
    reason logged. Used for devices that cannot be served at all
    (e.g. unidentifiable); served generic devices refuse at audio-open
    instead (note_audio_refusal)."""

    def __init__(self, record, reason):
        self.record = record
        self.reason = reason

    def refuse(self, log=_default_log):
        self.record.refusals += 1
        self.record.last_reason = self.reason
        self.record.last_event = time.time()
        log(f"refusing HFP service for {self.record.address} "
            f"({self.record.name or 'unnamed'}): {self.reason} "
            f"[refusal #{self.record.refusals}]")
        return self.reason


class Admission:
    """Routing verdict for one NewConnection."""

    def __init__(self, record, personality, slot=None):
        self.record = record
        self.personality = personality
        self.slot = slot        # RefusedSlot, or None when serving

    @property
    def refused(self):
        return self.slot is not None


class DeviceRegistry:
    def __init__(self, cfg, log=_default_log, rules=None):
        self.cfg = cfg
        self.log = log
        self.rules = rules
        self.lock = threading.Lock()
        self._records = {}      # address -> DeviceRecord
        # Set of addresses currently holding the call (AudioManager
        # wires this to the arbiter); used to judge "busy" reasons.
        self.call_holders = lambda: set()

    # -- routing -----------------------------------------------------------
    def admit(self, info):
        """Decide the personality and find/create the device's record.
        Never raises and never consults an allowlist."""
        personality = select_personality(info, self.cfg, self.rules)
        with self.lock:
            rec = self._records.get(info.address)
            if rec is None:
                rec = DeviceRecord(info, personality)
                self._records[info.address] = rec
            else:
                if rec.personality != personality:
                    self.log(f"device {info.address}: personality "
                             f"changed {rec.personality} -> "
                             f"{personality}")
                rec.personality = personality
                if info.display_name:
                    rec.name = info.display_name
            rec.last_event = time.time()
        shown = info.display_name or "unnamed"
        self.log(f"routing {info.address} ({shown}, "
                 f"paired={info.paired}) -> {personality}")
        if info.address == "?":
            return Admission(rec, personality,
                             RefusedSlot(rec, UNIDENTIFIED_DEVICE))
        # Glasses and generic alike are served; the personality picks
        # the session class (glasses_ag.Profile.NewConnection).
        return Admission(rec, personality)

    # -- session lifecycle -------------------------------------------------
    def attach(self, address, session):
        """Record a freshly built session for its device. A reconnect
        replaces the prior session object (which winds down on its own
        when its socket closes) — the same overwrite v1 did."""
        with self.lock:
            rec = self._records[address]
            prior = rec.session
            rec.session = session
            rec.connections += 1
            rec.last_event = time.time()
            others = [r.address for r in self._records.values()
                      if r is not rec
                      and r.personality == rec.personality
                      and r.session is not None
                      and getattr(r.session, "alive", False)]
        if prior is not None and getattr(prior, "alive", False):
            self.log(f"device {address}: new session replaces a "
                     "still-live one")
        if others and rec.personality == PERSONALITY_GLASSES:
            self.log(f"device {address}: other {rec.personality} "
                     f"session(s) live ({', '.join(others)}); the "
                     "shared controller serves the newest")
        return prior

    def detach(self, address, session):
        """Session ended. Only clears the slot if it is still the
        current session (a replaced one must not clear its successor)."""
        with self.lock:
            rec = self._records.get(address)
            if rec is None or rec.session is not session:
                return False
            rec.session = None
            rec.last_event = time.time()
        self.log(f"device {address}: session slot cleared")
        return True

    def note_audio_refusal(self, address, reason, peers=(), kind=""):
        """A served device asked for audio and was refused (or its
        audio connect failed). `peers` are the addresses the reason
        names; with `kind` they decide whether it is still current."""
        with self.lock:
            rec = self._records.get(address)
            if rec is None:
                return
            rec.audio_refusals += 1
            rec.last_audio_reason = reason
            rec.last_audio_peers = tuple(peers)
            rec.last_audio_kind = kind
            rec.last_event = time.time()

    # -- introspection -----------------------------------------------------
    def get(self, address):
        with self.lock:
            return self._records.get(address.upper())

    def records(self):
        with self.lock:
            return list(self._records.values())

    def note_audio_success(self, address):
        """A call-audio link opened for this device: whatever it was
        last refused for is history. Clears the reason/last_refusal;
        the cumulative audio_refusals counter is deliberately kept."""
        with self.lock:
            rec = self._records.get(address)
            if rec is None:
                return
            rec.last_audio_reason = ""
            rec.last_audio_peers = ()
            rec.last_audio_kind = ""

    def audio_reason_current(self, rec):
        """True while the last refusal's cause still holds: a "busy"
        reason while a device it names still holds the call; an "sco"
        reason while a device it names is still connected. A reason
        that names nothing is always current."""
        peers = rec.last_audio_peers
        if not peers or not rec.last_audio_reason:
            return bool(rec.last_audio_reason)
        if rec.last_audio_kind == "busy":
            holders = self.call_holders()
            return any(p in holders for p in peers)
        for p in peers:
            other = self.get(p)
            if other is not None and other.link() in ("up",
                                                      "connecting"):
                return True
        return False

    def live_peers(self, address):
        """(address, name) of every OTHER device that currently holds
        an RFCOMM/SLC link — the input to the multi-device SCO
        diagnostic."""
        out = []
        for rec in sorted(self.records(), key=lambda r: r.address):
            if rec.address == address:
                continue
            if rec.link() in ("up", "connecting"):
                out.append((rec.address, rec.name))
        return out

    def device_line(self, rec, detail=False):
        name = (rec.name or "").replace('"', "'")
        line = (f"device {rec.address} name=\"{name}\" "
                f"personality={rec.personality} link={rec.link()}")
        if rec.refusals:
            line += (f" refusals={rec.refusals} "
                     f"reason=\"{rec.last_reason}\"")
        sess = rec.session
        fields = getattr(sess, "status_fields", None)
        if fields is not None and getattr(sess, "alive", False):
            line += " " + fields()
            more = getattr(sess, "detail_fields", None)
            if detail and more is not None:
                line += " " + more()
        if rec.audio_refusals:
            line += f" audio_refusals={rec.audio_refusals}"
            # audio_reason only while its cause still holds; afterwards
            # the same text is shown as history, labelled as such, until
            # a successful audio open clears it.
            if rec.last_audio_reason:
                key = ("audio_reason" if self.audio_reason_current(rec)
                       else "last_refusal")
                line += f" {key}=\"{rec.last_audio_reason}\""
        return line

    def status_sections(self):
        """One line per known device, for the lines AFTER the glasses
        status line. Keys are deliberately distinct from the glasses
        line's keys (state/call/battery/discord/glasses_connected/
        cap_in) so a key=value scraper never confuses the two."""
        return [self.device_line(rec)
                for rec in sorted(self.records(),
                                  key=lambda r: r.address)]

    def resolve(self, selector):
        """Find one device by address (any case) or by name: exact
        match first, then a unique case-insensitive substring.
        Returns (record, None) or (None, error text)."""
        sel = (selector or "").strip()
        recs = self.records()
        for r in recs:
            if r.address.upper() == sel.upper():
                return r, None
        low = sel.casefold()
        hits = [r for r in recs if (r.name or "").casefold() == low]
        if not hits:
            hits = [r for r in recs if low and low in
                    (r.name or "").casefold()]
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            known = ", ".join(f"{r.address} ({r.name or 'unnamed'})"
                              for r in recs) or "none"
            return None, f"no such device {selector!r} (known: {known})"
        return None, (f"{selector!r} is ambiguous: " + ", ".join(
            f"{r.address} ({r.name or 'unnamed'})" for r in hits))


# --------------------------------------------------------------------------
# Control-socket verbs. Existing verbs target the glasses controller
# and reply exactly as before; "status" appends per-device sections.

CONTROL_VERBS = ("start", "stop", "toggle", "status", "media",
                 "discord", "discord_on", "discord_off")
# Verbs that also accept one device selector (address or name):
# `status <dev>`, `start <dev>`, `stop <dev>`, `toggle <dev>`. Without
# a selector they target the glasses exactly as before.
DEVICE_VERBS = ("status", "start", "stop", "toggle")


class ControlRouter:
    def __init__(self, registry, glasses_core):
        self.registry = registry
        self.glasses = glasses_core

    def handle(self, raw):
        cmd = raw.lower()
        g = self.glasses
        if cmd == "start":
            return g.cmd_start()
        if cmd == "stop":
            return g.cmd_stop()
        if cmd == "toggle":
            return g.cmd_toggle()
        if cmd == "media":
            return g.cmd_media()
        if cmd == "discord":
            return g.cmd_discord("toggle")
        if cmd == "discord_on":
            return g.cmd_discord("on")
        if cmd == "discord_off":
            return g.cmd_discord("off")
        if cmd == "status":
            lines = ["ok: " + g.status()]
            lines += self.registry.status_sections()
            return "\n".join(lines)
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in DEVICE_VERBS:
            return self._device_verb(parts[0].lower(), parts[1].strip())
        return f"err: unknown command {raw!r}"

    def _device_verb(self, verb, selector):
        reg = self.registry
        g = self.glasses
        rec = None
        if selector.casefold() == "glasses":
            is_glasses = True
            rec = next((r for r in reg.records()
                        if r.personality == PERSONALITY_GLASSES), None)
        else:
            rec, err = reg.resolve(selector)
            if rec is None:
                return f"err: {err}"
            is_glasses = rec.personality == PERSONALITY_GLASSES
        if is_glasses:
            if verb == "status":
                lines = ["ok: " + g.status()]
                if rec is not None:
                    lines.append(reg.device_line(rec, detail=True))
                return "\n".join(lines)
            return {"start": g.cmd_start, "stop": g.cmd_stop,
                    "toggle": g.cmd_toggle}[verb]()
        if verb == "status":
            return "ok: " + reg.device_line(rec, detail=True)
        sess = rec.session
        if sess is None or not getattr(sess, "alive", False):
            return f"err: {rec.address} is not connected"
        audio = getattr(sess, "audio", None)
        if audio is None:
            return f"err: no audio path for {rec.address}"
        if verb == "start":
            return audio.control_start()
        if verb == "stop":
            return audio.control_stop()
        return audio.control_stop() if audio.state != "idle" \
            else audio.control_start()
