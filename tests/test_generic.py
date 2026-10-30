"""Scripted SLC exchanges for the generic HFP personality, against a
fake RFCOMM socket. No Bluetooth, PipeWire, or root. Nothing here is
hardware verification."""

import pytest

import hfp_generic as hg
from hfp_registry import (DeviceInfo, DeviceRegistry, FingerprintConfig)

ADDR = "11:22:33:44:55:66"


class FakeSock:
    """recv() hands out scripted chunks then EOF; sendall() records."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


def make(chunks=(), **kw):
    logs = []
    reg = DeviceRegistry(FingerprintConfig("", "Meta Glasses"),
                         log=logs.append)
    reg.admit(DeviceInfo(ADDR, name="Corsair HS55", paired=True))
    sock = FakeSock(chunks)
    sess = hg.GenericSession(sock, "/dev/x", ADDR, name="Corsair HS55",
                             registry=reg, log=logs.append, **kw)
    reg.attach(ADDR, sess)
    return sess, sock, reg, logs


def say(sess, sock, *cmds):
    """Feed commands through handle(); return only the new replies."""
    start = len(sock.sent)
    for c in cmds:
        sess.handle(c)
    return sock.sent[start:].decode()


def replies(text):
    return [p for p in text.split("\r\n") if p]


# HFP 1.8 typical headset: codec negotiation + HF indicators.
HF_FEATURES = hg.HF_CODEC_NEGOTIATION | hg.HF_HF_INDICATORS | 0b1100000


def connect_full(sess, sock):
    out = say(sess, sock,
              f"AT+BRSF={HF_FEATURES}",
              "AT+BAC=1,2",
              "AT+CIND=?",
              "AT+CIND?",
              "AT+CMER=3,0,0,1")
    out += say(sess, sock,
               "AT+BIND=1,2", "AT+BIND=?", "AT+BIND?")
    return out


# -- full connect / indicators / idle ------------------------------------

def test_full_slc_connect():
    sess, sock, _, logs = make()
    out = connect_full(sess, sock)
    r = replies(out)
    assert f"+BRSF: {hg.AG_BRSF}" in r
    assert r.count("OK") == 8 and "ERROR" not in r
    assert sess.slc
    assert sess.indicator_reporting
    assert any("SLC established" in l for l in logs)


def test_slc_waits_for_hf_indicator_exchange_when_negotiated():
    sess, sock, _, _ = make()
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2",
        "AT+CIND=?", "AT+CIND?", "AT+CMER=3,0,0,1")
    assert not sess.slc          # BIND exchange still owed
    say(sess, sock, "AT+BIND=1,2", "AT+BIND=?", "AT+BIND?")
    assert sess.slc


def test_slc_up_at_cmer_when_no_hf_indicators():
    sess, sock, _, _ = make()
    say(sess, sock, "AT+BRSF=0", "AT+CIND=?", "AT+CIND?",
        "AT+CMER=3,0,0,1")
    assert sess.slc


def test_bitmap_is_truthful_about_unclaimed_features():
    assert hg.AG_BRSF == (1 << 6 | 1 << 9 | 1 << 10 | 1 << 11)
    for bit in (0, 1, 2, 3, 4, 5, 7, 8):   # 3WC, NREC, VR, ring, ...
        assert not hg.AG_BRSF & (1 << bit)


def test_cind_has_the_seven_standard_indicators_in_order():
    sess, sock, _, _ = make()
    out = say(sess, sock, "AT+CIND=?")
    for n in ("service", "call", "callsetup", "callheld",
              "signal", "roam", "battchg"):
        assert f'"{n}"' in out
    assert [out.index(f'"{n}"') for n in hg.CIND_NAMES] == sorted(
        out.index(f'"{n}"') for n in hg.CIND_NAMES)


def test_cind_read_is_idle_and_has_seven_values():
    sess, sock, _, _ = make()
    out = say(sess, sock, "AT+CIND?")
    vals = [int(x) for x in out.split("+CIND: ")[1].split("\r\n")[0]
            .split(",")]
    assert len(vals) == 7
    assert vals[1:4] == [0, 0, 0]          # call, callsetup, callheld


def test_cmer_rejects_unsupported_modes():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+CMER=1,0,0,1")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+CMER=3,0,0,2")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+CMER=3,0,0")) == ["ERROR"]
    assert not sess.cmer_done


def test_cmer_off_disables_ciev_and_bia_masks_indicators():
    sess, sock, _, _ = make()
    say(sess, sock, "AT+CMER=3,0,0,0")
    assert say(sess, sock) == ""
    sess.set_indicator("call", 1)
    assert "CIEV" not in sock.sent.decode()
    sess.set_indicator("call", 0)
    say(sess, sock, "AT+CMER=3,0,0,1", "AT+BIA=,0,,,,,")
    start = len(sock.sent)
    sess.set_indicator("call", 1)          # masked by BIA
    assert sock.sent[start:] == b""
    sess.set_indicator("callsetup", 2)     # still enabled
    assert "+CIEV: 3,2" in sock.sent[start:].decode()


def test_bia_bad_input_is_acknowledged_and_leaves_mask():
    sess, sock, _, _ = make()
    # lenient: never ERROR, bad tokens / extra fields ignored
    assert replies(say(sess, sock, "AT+BIA=x")) == ["OK"]
    assert replies(say(sess, sock, "AT+BIA=1,1,1,1,1,1,1,1")) == ["OK"]
    assert replies(say(sess, sock, "AT+BIA=")) == ["OK"]
    assert sess.ind_enabled == [True] * 7


def test_chld_test_does_not_claim_three_way_calling():
    sess, sock, _, _ = make()
    assert "+CHLD: (0)" in say(sess, sock, "AT+CHLD=?")
    assert replies(say(sess, sock, "AT+CHLD=2")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+CHLD=0")) == ["OK"]


# -- +CLCC truthfulness --------------------------------------------------

def test_clcc_is_empty_when_idle():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+CLCC")) == ["OK"]


def test_no_command_conjures_a_call():
    sess, sock, _, _ = make()
    say(sess, sock, "AT+CKPD=200", "AT+BLDN", "AT+BCC", "ATA",
        "ATD5551212;", "AT+CHUP", "AT+BVRA=1")
    assert sess.calls == []
    assert sess.cind[1:4] == [0, 0, 0]
    assert replies(say(sess, sock, "AT+CLCC")) == ["OK"]
    assert "CIEV" not in sock.sent.decode()


def test_clcc_renders_real_entries_only():
    sess, sock, _, _ = make()
    sess.calls = [dict(idx=1, dir=0, stat=0, mode=0, mpty=0,
                       number="123", type=129)]
    assert '+CLCC: 1,0,0,0,0,"123",129' in say(sess, sock, "AT+CLCC")


# -- headset-initiated vocabulary ----------------------------------------

def test_volume_sync_stores_and_validates():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+VGS=7", "AT+VGM=3")) == [
        "OK", "OK"]
    assert (sess.speaker_gain, sess.mic_gain) == (7, 3)
    assert replies(say(sess, sock, "AT+VGS=16")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+VGM=x")) == ["ERROR"]
    assert sess.speaker_gain == 7


def test_biev_battery_and_safety():
    sess, sock, _, logs = make()
    say(sess, sock, "AT+BIND=1,2")
    assert replies(say(sess, sock, "AT+BIEV=2,80")) == ["OK"]
    assert sess.hf_battery == 80
    assert "hf_battery=80" in sess.status_fields()
    assert replies(say(sess, sock, "AT+BIEV=2,101")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+BIEV=1,1")) == ["OK"]
    assert sess.hf_safety == 1
    assert replies(say(sess, sock, "AT+BIEV=9,1")) == ["ERROR"]


def test_biev_for_indicator_headset_never_listed_is_refused():
    sess, sock, _, _ = make()
    say(sess, sock, "AT+BIND=1")
    assert replies(say(sess, sock, "AT+BIEV=2,50")) == ["ERROR"]
    assert sess.hf_battery is None


def test_bind_reports_intersection_only():
    sess, sock, _, _ = make()
    out = say(sess, sock, "AT+BIND=2,7,1", "AT+BIND=?", "AT+BIND?")
    assert "+BIND: (1,2)" in out
    assert "+BIND: 2,1" in out and "+BIND: 1,1" in out
    assert "+BIND: 7" not in out


def test_button_press_acknowledged_other_ckpd_not():
    sess, sock, _, logs = make()
    assert replies(say(sess, sock, "AT+CKPD=200")) == ["OK"]
    assert replies(say(sess, sock, "AT+CKPD=1")) == ["ERROR"]
    assert any("CKPD=200" in l for l in logs)


def test_bldn_bvra_have_no_fake_support():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+BLDN")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+BVRA=1")) == ["ERROR"]


def test_clip_ccwa_cops_cnum_are_honest_noops():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+CLIP=1", "AT+CCWA=1",
                       "AT+COPS=3,0", "AT+CNUM")) == ["OK"] * 4
    assert "+COPS: 0" in say(sess, sock, "AT+COPS?")
    assert replies(say(sess, sock, "AT+CLIP=7")) == ["ERROR"]


# -- codec negotiation -----------------------------------------------------

def test_msbc_offered_and_accepted():
    sess, sock, _, _ = make()
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2")
    assert sess.selected_codec() == hg.CODEC_MSBC
    start = len(sock.sent)
    assert sess.begin_codec_offer() == hg.CODEC_MSBC
    out = sock.sent[start:].decode()
    assert "+BCS: 2" in out
    sess.audio_opener = lambda src: True    # Step C stand-in
    assert replies(say(sess, sock, "AT+BCS=2")) == ["OK"]
    assert sess.codec_confirmed == hg.CODEC_MSBC


def test_mismatched_codec_confirmation_is_error():
    sess, sock, _, _ = make()
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2")
    sess.begin_codec_offer()
    assert replies(say(sess, sock, "AT+BCS=1")) == ["ERROR"]
    assert sess.codec_confirmed is None


def test_cvsd_only_headset_gets_cvsd_floor():
    sess, sock, _, _ = make()
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1")
    assert sess.selected_codec() == hg.CODEC_CVSD
    start = len(sock.sent)
    sess.begin_codec_offer()
    assert "+BCS: 1" in sock.sent[start:].decode()
    assert "codec=cvsd" in sess.status_fields()


def test_headset_without_codec_negotiation_is_cvsd_and_silent():
    sess, sock, _, _ = make()
    say(sess, sock, "AT+BRSF=0")
    assert sess.selected_codec() == hg.CODEC_CVSD
    start = len(sock.sent)
    sess.begin_codec_offer()
    assert sock.sent[start:] == b""
    assert sess.codec_confirmed == hg.CODEC_CVSD


def test_msbc_not_picked_if_ag_cannot_do_it():
    sess, sock, _, _ = make(ag_codecs=(hg.CODEC_CVSD,))
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2")
    assert sess.selected_codec() == hg.CODEC_CVSD


def test_unsolicited_bcs_is_error():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+BCS=2")) == ["ERROR"]


def test_bac_garbage_is_error():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+BAC=")) == ["ERROR"]
    assert replies(say(sess, sock, "AT+BAC=a,b")) == ["ERROR"]


# -- audio-open refusal on a bare session (no audio manager) ------------

def test_bcc_is_refused_cleanly_with_logged_reason():
    sess, sock, reg, logs = make()
    connect_full(sess, sock)
    assert replies(say(sess, sock, "AT+BCC")) == ["ERROR"]
    assert sess.alive and sess.slc               # link untouched
    rec = reg.get(ADDR)
    assert rec.audio_refusals == 1
    assert "no audio manager" in rec.last_audio_reason
    assert any("audio-open refused (AT+BCC)" in l for l in logs)
    assert rec.link() == "up"                    # not a connect refusal
    assert 'audio_refusals=1' in reg.status_sections()[0]


# -- unknown / hostile input ------------------------------------------------

def test_unknown_at_gets_clean_error_and_link_survives():
    sess, sock, _, _ = make()
    connect_full(sess, sock)
    for cmd in ("AT+XYZZY=1",
                "AT+NREC=0", "AT+CMEE=1", "ATZ", "garbage", "AT+"):
        assert replies(say(sess, sock, cmd)) == ["ERROR"], cmd
    assert sess.alive and sess.slc


def test_stray_result_code_lines_are_ignored_not_answered():
    sess, sock, _, _ = make()
    assert say(sess, sock, "+CIEV: 1,1") == ""


def test_run_loop_splits_commands_survives_handler_crash_and_detaches():
    sess, sock, reg, logs = make(
        [b"AT+BRSF=0\rAT+CI", b"ND=?\r\nAT\r\r", b"AT+CKPD=200\r"])

    real = sess.handle

    def boom(cmd):
        if cmd == "AT":
            raise RuntimeError("bug")
        real(cmd)
    sess.handle = boom
    sess.run()
    r = replies(sock.sent.decode())
    assert f"+BRSF: {hg.AG_BRSF}" in r
    assert any(x.startswith("+CIND:") for x in r)
    assert r.count("ERROR") == 1            # the crashed command
    assert r[-1] == "OK"                    # CKPD still served afterwards
    assert not sess.alive and sock.closed
    assert reg.get(ADDR).session is None    # slot cleared on teardown
    assert reg.get(ADDR).link() == "down"
    assert any("handler error" in l for l in logs)


def test_run_survives_oversized_unterminated_input():
    sess, sock, _, logs = make([b"A" * 5000, b"AT\r"])
    sess.run()
    assert replies(sock.sent.decode()) == ["OK"]
    assert any("oversized" in l for l in logs)


def test_socket_error_on_send_does_not_raise():
    class Dead(FakeSock):
        def sendall(self, data):
            raise OSError("gone")
    sess, _, _, _ = make()
    sess.sock = Dead()
    sess.handle("AT")


def test_status_fields_do_not_collide_with_glasses_keys():
    sess, sock, reg, _ = make()
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2",
        "AT+BIND=1,2", "AT+BIEV=2,50")
    line = reg.status_sections()[0]
    assert "codec=msbc" in line and "hf_battery=50" in line
    keys = {t.split("=", 1)[0] for t in line.split() if "=" in t}
    assert not keys & {"state", "call", "battery", "discord",
                       "glasses_connected", "cap_in", "cap"}


# -- Apple IPHONEACCEV battery dialect (Corsair HS55 quirk) ----------------

def test_iphoneaccev_battery_is_accepted_and_stored():
    sess, sock, _, logs = make()
    connect_full(sess, sock)
    assert replies(say(sess, sock, "AT+IPHONEACCEV=1,1,5")) == ["OK"]
    assert sess.hf_battery == 50                 # level 5 of 9 -> x10
    assert "hf_battery=50" in sess.status_fields()
    assert any("IPHONEACCEV dialect accepted" in l for l in logs)
    assert sess.alive and sess.slc


def test_iphoneaccev_multiple_pairs_and_dock_key():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+IPHONEACCEV=2,1,9,2,0")) == ["OK"]
    assert sess.hf_battery == 90
    # dock-only report: accepted, battery untouched
    assert replies(say(sess, sock, "AT+IPHONEACCEV=1,2,0")) == ["OK"]
    assert sess.hf_battery == 90


def test_iphoneaccev_level_is_clamped_and_repeat_not_relogged():
    sess, sock, _, logs = make()
    say(sess, sock, "AT+IPHONEACCEV=1,1,20")
    assert sess.hf_battery == 100
    say(sess, sock, "AT+IPHONEACCEV=1,1,20")
    assert sum("battery" in l for l in logs) == 1


@pytest.mark.parametrize("arg", ["", "x", "1,1", "2,1,5", "-1", "1,1,a"])
def test_iphoneaccev_malformed_is_a_clean_error(arg):
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, f"AT+IPHONEACCEV={arg}")) == ["ERROR"]
    assert sess.hf_battery is None and sess.alive


def test_devices_that_never_send_iphoneaccev_are_unaffected():
    sess, sock, _, _ = make()
    connect_full(sess, sock)
    assert sess.hf_battery is None
    assert "hf_battery" not in sess.status_fields()
    # the spec path still works alongside
    assert replies(say(sess, sock, "AT+BIEV=2,40")) == ["OK"]
    assert sess.hf_battery == 40


def test_bia_records_state_and_tolerates_extra_fields():
    sess, sock, _, logs = make()
    assert replies(say(sess, sock, "AT+BIA=1,0,1,1,1,1,0,1")) == ["OK"]
    assert sess.ind_enabled == [True, False, True, True, True, True,
                                False]
    assert any("past 7" in l for l in logs)
    # unrelated tokens in an otherwise valid list don't undo the rest
    assert replies(say(sess, sock, "AT+BIA=1,z,0")) == ["OK"]
    assert sess.ind_enabled[2] is False and sess.ind_enabled[1] is False


# -- Apple XAPL handshake (strict devices gate battery on it) --------------

def test_xapl_is_accepted_parsed_and_stored():
    sess, sock, _, logs = make()
    connect_full(sess, sock)
    out = replies(say(sess, sock, "AT+XAPL=000D-0001-0101,2"))
    assert out == ["+XAPL=iPhone,2", "OK"]
    assert sess.xapl_id == "000D-0001-0101" and sess.xapl_features == 2
    assert any("XAPL dialect accepted" in l for l in logs)
    assert "xapl=000D-0001-0101,2" in sess.detail_fields()
    assert sess.alive and sess.slc


def test_xapl_advertises_only_what_we_consume():
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, "AT+XAPL=0001-0002-0003,6"))[0] \
        == "+XAPL=iPhone,2"                        # battery, not dock
    assert replies(say(sess, sock, "AT+XAPL=0001-0002-0003,4"))[0] \
        == "+XAPL=iPhone,0"                        # no battery offered
    assert sess.xapl_features == 4


@pytest.mark.parametrize("arg", ["", "abc", "0001-0002-0003",
                                 "0001-0002-0003,x", ",2"])
def test_xapl_malformed_is_a_clean_error(arg):
    sess, sock, _, _ = make()
    assert replies(say(sess, sock, f"AT+XAPL={arg}")) == ["ERROR"]
    assert sess.xapl_id is None and sess.alive


def test_strict_device_xapl_then_iphoneaccev_reports_battery():
    sess, sock, _, _ = make()
    connect_full(sess, sock)
    assert replies(say(sess, sock, "AT+XAPL=000D-0001-0101,2"))[-1] == "OK"
    assert replies(say(sess, sock, "AT+IPHONEACCEV=1,1,7")) == ["OK"]
    assert sess.hf_battery == 70
    assert "hf_battery=70" in sess.status_fields()


def test_devices_that_never_send_xapl_are_unaffected():
    sess, sock, _, _ = make()
    connect_full(sess, sock)
    assert sess.xapl_id is None
    assert "xapl=" not in sess.detail_fields()
