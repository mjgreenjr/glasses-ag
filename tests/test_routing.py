"""Stub tests for fingerprint selection and per-device session
routing. The D-Bus/BlueZ boundary is faked: DeviceInfo records are
built by hand and sessions are plain objects. Needs no Bluetooth,
PipeWire, or root."""

import hfp_registry as hr
from hfp_registry import (DeviceInfo, DeviceRegistry, FingerprintConfig,
                          PERSONALITY_GENERIC, PERSONALITY_GLASSES)

GLASSES = "AA:BB:CC:DD:EE:01"
HEADSET = "11:22:33:44:55:66"


def glasses(addr=GLASSES, name="Meta Glasses 1A2B"):
    return DeviceInfo(addr, name=name, paired=True)


def headset(addr=HEADSET, name="Corsair HS55"):
    return DeviceInfo(addr, name=name, paired=True)


def cfg(pin="", prefix="Meta Glasses"):
    return FingerprintConfig(pin, prefix)


class FakeSession:
    def __init__(self, alive=True, slc=True):
        self.alive = alive
        self.slc = slc


class FakeGlassesCore:
    def __init__(self):
        self.calls = []

    def status(self):
        return ("state=ready glasses_connected=True call=idle "
                "discord=off")

    def __getattr__(self, name):
        if name.startswith("cmd_"):
            def verb(*a):
                self.calls.append((name, a))
                return f"ok: {name}"
            return verb
        raise AttributeError(name)


def registry(**kw):
    logs = []
    return DeviceRegistry(cfg(**kw), log=logs.append), logs


# -- fingerprint selection ---------------------------------------------

def test_name_prefix_selects_glasses():
    assert hr.select_personality(glasses(), cfg()) == PERSONALITY_GLASSES


def test_name_prefix_is_case_insensitive():
    info = glasses(name="META GLASSES xyz")
    assert hr.select_personality(info, cfg()) == PERSONALITY_GLASSES


def test_alias_used_when_name_empty():
    info = DeviceInfo(GLASSES, name="", alias="Meta Glasses 9")
    assert hr.select_personality(info, cfg()) == PERSONALITY_GLASSES


def test_other_device_is_generic():
    assert hr.select_personality(headset(), cfg()) == PERSONALITY_GENERIC


def test_unnamed_device_is_generic():
    info = DeviceInfo(HEADSET)
    assert hr.select_personality(info, cfg()) == PERSONALITY_GENERIC


def test_pin_matches_regardless_of_name():
    info = DeviceInfo(GLASSES, name="Something Else")
    assert hr.select_personality(info, cfg(pin=GLASSES.lower())) \
        == PERSONALITY_GLASSES


def test_pin_is_decisive_against_matching_name():
    # v1 semantics: with a pin set, the name prefix is not consulted.
    other = glasses(addr="AA:BB:CC:DD:EE:99")
    assert hr.select_personality(other, cfg(pin=GLASSES)) \
        == PERSONALITY_GENERIC


def test_empty_prefix_and_no_pin_means_nothing_matches():
    assert hr.select_personality(glasses(), cfg(prefix="")) \
        == PERSONALITY_GENERIC


def test_quirk_seam_custom_rule_wins_in_order():
    def rule(info, c):
        return PERSONALITY_GLASSES if info.address == HEADSET else None
    assert hr.select_personality(headset(), cfg(), rules=[rule]) \
        == PERSONALITY_GLASSES


# -- admission / routing -------------------------------------------------

def test_glasses_admitted_and_not_refused():
    reg, _ = registry()
    adm = reg.admit(glasses())
    assert adm.personality == PERSONALITY_GLASSES
    assert not adm.refused


def test_unknown_paired_device_is_served_generic_not_refused():
    reg, _ = registry()
    adm = reg.admit(headset())
    assert adm.personality == PERSONALITY_GENERIC
    assert not adm.refused
    assert adm.record.refusals == 0


def test_unpaired_flag_does_not_gate_admission():
    # Admission is BlueZ's job; the daemon has no allowlist/gate.
    reg, _ = registry()
    info = DeviceInfo(HEADSET, name="X", paired=False)
    assert reg.admit(info).personality == PERSONALITY_GENERIC


def test_unreadable_device_is_refused_not_raised():
    reg, logs = registry()
    adm = reg.admit(DeviceInfo("?"))
    assert adm.refused
    reason = adm.slot.refuse(logs.append)
    assert "unreadable" in reason
    assert any(reason in l for l in logs)
    assert adm.record.link() == "refused"


def test_records_keyed_by_address_case_insensitively():
    reg, _ = registry()
    a = reg.admit(DeviceInfo(HEADSET.lower(), name="H"))
    b = reg.admit(DeviceInfo(HEADSET.upper(), name="H"))
    assert a.record is b.record
    assert len(reg.records()) == 1


# -- session slots -------------------------------------------------------

def test_attach_and_detach_glasses_session():
    reg, _ = registry()
    adm = reg.admit(glasses())
    s = FakeSession()
    assert reg.attach(GLASSES, s) is None
    assert adm.record.session is s
    assert adm.record.link() == "up"
    assert reg.detach(GLASSES, s)
    assert adm.record.session is None
    assert adm.record.link() == "down"


def test_session_before_slc_is_connecting():
    reg, _ = registry()
    adm = reg.admit(glasses())
    reg.attach(GLASSES, FakeSession(slc=False))
    assert adm.record.link() == "connecting"


def test_stale_session_cannot_clear_successor():
    reg, _ = registry()
    adm = reg.admit(glasses())
    old, new = FakeSession(), FakeSession()
    reg.attach(GLASSES, old)
    assert reg.attach(GLASSES, new) is old
    assert not reg.detach(GLASSES, old)
    assert adm.record.session is new


def test_refused_device_does_not_disturb_glasses_session():
    reg, _ = registry()
    g = reg.admit(glasses())
    s = FakeSession()
    reg.attach(GLASSES, s)
    h = reg.admit(DeviceInfo("?"))
    h.slot.refuse()
    h.slot.refuse()
    assert g.record.session is s and g.record.link() == "up"
    assert h.record.refusals == 2 and h.record.session is None


def test_second_glasses_address_is_logged():
    reg, logs = registry()
    other = "AA:BB:CC:DD:EE:02"
    reg.admit(glasses())
    reg.attach(GLASSES, FakeSession())
    reg.admit(glasses(addr=other))
    reg.attach(other, FakeSession())
    assert any("other glasses session" in l for l in logs)


# -- control routing -----------------------------------------------------

def test_existing_verbs_target_glasses_unchanged():
    reg, _ = registry()
    core = FakeGlassesCore()
    r = hr.ControlRouter(reg, core)
    assert r.handle("start") == "ok: cmd_start"
    assert r.handle("stop") == "ok: cmd_stop"
    assert r.handle("toggle") == "ok: cmd_toggle"
    assert r.handle("media") == "ok: cmd_media"
    assert r.handle("discord") == "ok: cmd_discord"
    assert r.handle("discord_on") == "ok: cmd_discord"
    assert r.handle("discord_off") == "ok: cmd_discord"
    assert [c for c in core.calls if c[0] == "cmd_discord"] == [
        ("cmd_discord", ("toggle",)), ("cmd_discord", ("on",)),
        ("cmd_discord", ("off",))]


def test_unknown_verb_reply_unchanged():
    r = hr.ControlRouter(registry()[0], FakeGlassesCore())
    assert r.handle("Bogus") == "err: unknown command 'Bogus'"


def test_status_first_line_is_glasses_status_then_device_sections():
    reg, _ = registry()
    core = FakeGlassesCore()
    reg.admit(glasses())
    reg.attach(GLASSES, FakeSession())
    reg.admit(headset())
    out = hr.ControlRouter(reg, core).handle("status")
    lines = out.split("\n")
    assert lines[0] == "ok: " + core.status()
    assert len(lines) == 3
    by_addr = {l.split()[1]: l for l in lines[1:]}
    assert "link=up" in by_addr[GLASSES]
    assert "personality=generic link=down" in by_addr[HEADSET]


def test_status_with_no_devices_is_exactly_the_old_reply():
    core = FakeGlassesCore()
    out = hr.ControlRouter(registry()[0], core).handle("status")
    assert out == "ok: " + core.status()


def test_device_lines_do_not_reuse_glasses_status_keys():
    reg, _ = registry()
    reg.admit(glasses())
    reg.attach(GLASSES, FakeSession())
    reg.admit(headset())
    reg.note_audio_refusal(HEADSET, "r")
    taken = {"state", "call", "battery", "discord",
             "glasses_connected", "cap_in", "cap"}
    for line in reg.status_sections():
        keys = {t.split("=", 1)[0] for t in line.split() if "=" in t}
        assert not keys & taken
