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

# Step A: generic devices are admitted into a slot, but the slot has
# no gateway behavior yet. Step B replaces this refusal.
GENERIC_NOT_IMPLEMENTED = (
    "generic HFP personality not implemented yet (v2 Step B)")


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
        self.refusals = 0
        self.last_reason = ""
        self.last_event = time.time()

    def link(self):
        s = self.session
        if s is not None and getattr(s, "alive", False):
            return "up" if getattr(s, "slc", False) else "connecting"
        if self.refusals and not self.connections:
            return "refused"
        return "down"


class RefusedSlot:
    """The Step A stand-in for the generic personality: a slot that
    cleanly refuses service, with the reason logged."""

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
        if personality == PERSONALITY_GLASSES:
            return Admission(rec, personality)
        return Admission(rec, personality,
                         RefusedSlot(rec, GENERIC_NOT_IMPLEMENTED))

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
        if others:
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

    # -- introspection -----------------------------------------------------
    def get(self, address):
        with self.lock:
            return self._records.get(address.upper())

    def records(self):
        with self.lock:
            return list(self._records.values())

    def status_sections(self):
        """One line per known device, for the lines AFTER the glasses
        status line. Keys are deliberately distinct from the glasses
        line's keys (state/call/battery/discord/glasses_connected/
        cap_in) so a key=value scraper never confuses the two."""
        lines = []
        for rec in sorted(self.records(), key=lambda r: r.address):
            name = (rec.name or "").replace('"', "'")
            line = (f"device {rec.address} name=\"{name}\" "
                    f"personality={rec.personality} link={rec.link()}")
            if rec.refusals:
                line += (f" refusals={rec.refusals} "
                         f"reason=\"{rec.last_reason}\"")
            lines.append(line)
        return lines


# --------------------------------------------------------------------------
# Control-socket verbs. Existing verbs target the glasses controller
# and reply exactly as before; "status" appends per-device sections.

CONTROL_VERBS = ("start", "stop", "toggle", "status", "media",
                 "discord", "discord_on", "discord_off")


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
        return f"err: unknown command {raw!r}"
