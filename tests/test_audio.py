"""Step C: the generic-device audio path at the fake boundary — pactl,
pw-cli, the SCO socket, libsbc, time and threads are all injected. No
Bluetooth, PipeWire, or root. NOTHING HERE IS HARDWARE VERIFICATION."""

import os
import types

import pytest

import hfp_audio as ha
import hfp_generic as hg
import hfp_registry as hr
from test_generic import HF_FEATURES, FakeSock, replies, say
from test_glasses_regression import (CORSAIR_ADDR, GLASSES_ADDR, Core,
                                     _install_fakes, _two_records)

ADDR = "11:22:33:44:55:66"
CARD = "bluez_card.11_22_33_44_55_66"


# -- fakes ----------------------------------------------------------------

class FakePactl:
    def __init__(self):
        self.cards = {CARD: "a2dp-sink"}
        self.sources = []           # (idx, name)
        self.sinks = []
        self.source_outputs = []    # (id, source idx)
        self.sink_inputs = []
        self.calls = []
        self.fail_set = False
        self._next = 100

    def add_node(self, kind, name):
        self._next += 1
        (self.sources if kind == "source" else self.sinks).append(
            (str(self._next), name))

    def remove_node(self, name):
        self.sources = [x for x in self.sources if x[1] != name]
        self.sinks = [x for x in self.sinks if x[1] != name]

    def idx(self, name):
        for i, n in self.sources + self.sinks:
            if n == name:
                return i

    def __call__(self, *args):
        self.calls.append(args)
        if args == ("list", "cards"):
            return "".join(
                f"Card #1\n\tName: {c}\n\tDriver: x\n"
                f"\tActive Profile: {p}\n\tPorts:\n"
                for c, p in self.cards.items())
        if args[0] == "set-card-profile":
            if self.fail_set:
                return None
            self.cards[args[1]] = args[2]
            return ""
        if args[:2] == ("list", "short"):
            kind = args[2]
            if kind == "sources":
                return "".join(f"{i}\t{n}\tm\ts16le 1ch 16000Hz\tIDLE\n"
                               for i, n in self.sources)
            if kind == "sinks":
                return "".join(f"{i}\t{n}\tm\ts16le 1ch 16000Hz\tIDLE\n"
                               for i, n in self.sinks)
            if kind == "source-outputs":
                return "".join(f"{i}\t{s}\t7\tm\ts16le\n"
                               for i, s in self.source_outputs)
            if kind == "sink-inputs":
                return "".join(f"{i}\t{s}\t7\tm\ts16le\n"
                               for i, s in self.sink_inputs)
        return None


class FakeProc:
    def __init__(self, argv, pactl, appear=True, dead=False):
        self.argv = argv
        self.pactl = pactl
        self.rc = 1 if dead else None
        self.terminated = False
        args = argv[-1]
        self.node = args.split('node.name="')[1].split('"')[0]
        mode = args.split("tunnel.mode=")[1].split()[0]
        if appear and not dead:
            pactl.add_node(mode, self.node)

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = 0
        self.pactl.remove_node(self.node)

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.rc


class FakeStream:
    def __init__(self, path):
        self.path = path
        self.started = self.stopped = False
        self.pushed = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def push(self, pcm):
        self.pushed.append(pcm)

    def take(self, n=240):
        return b"\x01\x00" * (n // 2)


class FakeSco:
    def __init__(self):
        self.closed = False
        self.sent = []

    def close(self):
        self.closed = True

    def sendall(self, d):
        self.sent.append(bytes(d))

    def recv(self, n):
        return b""


class FakeTimer:
    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False

    def cancel(self):
        self.cancelled = True


class Env:
    pass


@pytest.fixture(autouse=True)
def _no_bridge_threads(monkeypatch):
    started = []
    monkeypatch.setattr(ha.ScoBridge, "start",
                        lambda self: started.append(self))
    return started


def make_env(tmp_path, name="Corsair HS55", features=HF_FEATURES,
             pw="ok", connect_fail=0):
    e = Env()
    e.logs = []
    e.pactl = FakePactl()
    e.procs = []
    e.timers = []
    e.sco = []                      # (address, codec) per connect attempt
    e.scosocks = []
    e.clock = [1000.0]
    e.fail_left = [connect_fail]

    def spawn_pwcli(argv, tag=""):
        if pw == "missing":
            raise FileNotFoundError("pw-cli")
        p = FakeProc(argv, e.pactl, appear=(pw != "never"),
                     dead=(pw == "dies"))
        e.procs.append(p)
        return p

    def sco_connect(address, codec):
        e.sco.append((address, codec))
        if e.fail_left[0] > 0:
            e.fail_left[0] -= 1
            raise OSError(111, "Connection refused")
        s = FakeSco()
        e.scosocks.append(s)
        return s, 48

    def timer(delay, fn):
        t = FakeTimer(delay, fn)
        e.timers.append(t)
        return t

    e.deps = ha.AudioDeps(
        str(tmp_path), log=e.logs.append, pactl=e.pactl,
        spawn_pwcli=spawn_pwcli, sco_connect=sco_connect,
        make_decoder=lambda: types.SimpleNamespace(
            decode_frame=lambda f: b"\x00" * 240),
        make_encoder=lambda: types.SimpleNamespace(
            encode_frame=lambda p: b"\xAD" + b"\x00" * 56),
        make_pipe=FakeStream, make_spk=FakeStream,
        sleep=lambda s: None, clock=lambda: e.clock[0],
        spawn=lambda fn, *a, name="": fn(*a), timer=timer, watch=False)
    e.reg = hr.DeviceRegistry(hr.FingerprintConfig("", "Meta Glasses"),
                              log=e.logs.append)
    e.reg.admit(hr.DeviceInfo(ADDR, name=name, paired=True))
    e.mgr = ha.AudioManager(e.deps, e.reg)
    e.sock = FakeSock()
    e.sess = hg.GenericSession(e.sock, "/dev/x", ADDR, name=name,
                               registry=e.reg, log=e.logs.append)
    e.reg.attach(ADDR, e.sess)
    e.mgr.attach(e.sess)
    e.audio = e.sess.audio
    return e


def slc(e, features=HF_FEATURES, codecs="1,2"):
    out = say(e.sess, e.sock, f"AT+BRSF={features}")
    if codecs:
        out += say(e.sess, e.sock, f"AT+BAC={codecs}")
    out += say(e.sess, e.sock, "AT+CIND=?", "AT+CIND?",
               "AT+CMER=3,0,0,1")
    if features & hg.HF_HF_INDICATORS:
        out += say(e.sess, e.sock, "AT+BIND=1,2", "AT+BIND?")
    assert e.sess.slc
    return out


def open_call(e):
    """Headset-initiated happy path up to a live call."""
    assert replies(say(e.sess, e.sock, "AT+BCC"))[0] == "OK"
    return say(e.sess, e.sock, "AT+BCS=2")


# -- the happy path: BCC -> offer -> confirm -> SCO to OWN device ---------

def test_bcc_audio_open_happy_path(tmp_path, _no_bridge_threads):
    e = make_env(tmp_path)
    slc(e)
    assert e.audio.prov == "ok"
    # the OK reaches the headset BEFORE our +BCS offer
    assert replies(say(e.sess, e.sock, "AT+BCC")) == ["OK", "+BCS: 2"]
    assert e.audio.state == "offering"
    assert e.mgr.arbiter.generic_holder() == f"Corsair HS55 ({ADDR})"
    assert e.pactl.cards[CARD] == "off"             # parked on open
    assert e.audio.parker.parked
    assert replies(say(e.sess, e.sock, "AT+BCS=2")) == ["OK"]
    # SCO went to THIS session's own address, with the mSBC codec
    assert e.sco == [(ADDR, hg.CODEC_MSBC)]
    assert e.audio.state == "live"
    assert len(_no_bridge_threads) == 1
    assert any("SCO open — Corsair HS55 live" in l for l in e.logs)
    assert "audio=live" in e.sess.status_fields()
    assert "a2dp_parked=true" in e.sess.status_fields()


def test_call_end_restores_what_it_parked_and_releases_the_slot(
        tmp_path, _no_bridge_threads):
    e = make_env(tmp_path)
    slc(e)
    open_call(e)
    bridge = _no_bridge_threads[0]
    bridge._ended()                                 # headset hung up
    assert e.audio.state == "idle"
    assert e.pactl.cards[CARD] == "a2dp-sink"       # restored on close
    assert not e.audio.parker.parked
    assert e.mgr.arbiter.generic_holder() is None
    assert e.scosocks[0].closed
    assert e.sess.alive and e.sess.slc              # SLC untouched


def test_card_not_on_a2dp_is_never_parked_or_restored(tmp_path):
    e = make_env(tmp_path)
    e.pactl.cards[CARD] = "headset-head-unit"
    slc(e)
    open_call(e)
    assert not e.audio.parker.parked
    assert e.pactl.cards[CARD] == "headset-head-unit"
    e.audio.stop()
    assert e.pactl.cards[CARD] == "headset-head-unit"
    assert not any(c[0] == "set-card-profile" for c in e.pactl.calls)


def test_orphaned_park_is_adopted_at_slc(tmp_path):
    e = make_env(tmp_path)
    e.pactl.cards[CARD] = "off"                    # left by a dead daemon
    slc(e)
    assert e.pactl.cards[CARD] == "a2dp-sink"
    assert any("adopted orphaned A2DP park" in l for l in e.logs)


def test_orphan_adoption_leaves_a_park_we_made_alone(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    open_call(e)
    assert e.audio.parker.parked and e.pactl.cards[CARD] == "off"
    assert e.audio.parker.reconcile() is False      # our own park
    assert e.pactl.cards[CARD] == "off"


def test_cvsd_headset_skips_the_offer_and_opens_cvsd(tmp_path):
    e = make_env(tmp_path)
    slc(e, features=0b1100000, codecs="")           # no codec negotiation
    assert e.audio.request("app attach") is True
    assert "+BCS" not in e.sock.sent.decode()
    assert e.sco == [(ADDR, hg.CODEC_CVSD)]
    assert e.audio.state == "live"


def test_codec_offer_timeout_releases_everything(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    say(e.sess, e.sock, "AT+BCC")
    assert e.audio.state == "offering" and e.pactl.cards[CARD] == "off"
    e.timers[-1].fn()                               # watchdog fires
    assert e.audio.state == "idle"
    assert e.pactl.cards[CARD] == "a2dp-sink"
    assert e.mgr.arbiter.generic_holder() is None
    # a late confirmation is not accepted as an unsolicited offer
    assert replies(say(e.sess, e.sock, "AT+BCS=2")) == ["ERROR"]


def test_sco_connect_failure_cleans_up_and_names_the_cause(tmp_path):
    e = make_env(tmp_path, connect_fail=99)
    slc(e)
    open_call(e)
    assert len(e.sco) == ha.SCO_ATTEMPTS
    assert e.audio.state == "idle"
    assert e.pactl.cards[CARD] == "a2dp-sink"
    assert e.mgr.arbiter.generic_holder() is None
    assert e.sess.alive and e.sess.slc
    assert any("SCO connect failed" in l for l in e.logs)
    assert e.reg.get(ADDR).audio_refusals == 1


# -- platform-constraint diagnostic ----------------------------------------

def test_failed_sco_with_another_device_connected_names_the_cause(tmp_path):
    e = make_env(tmp_path, connect_fail=99)
    other = "AA:BB:CC:00:00:02"
    e.reg.admit(hr.DeviceInfo(other, name="Sennheiser HD 450BT",
                              paired=True))
    osess = hg.GenericSession(FakeSock(), "/dev/o", other,
                              name="Sennheiser HD 450BT", registry=e.reg,
                              log=e.logs.append)
    e.reg.attach(other, osess)
    slc(e)
    open_call(e)
    text = "\n".join(e.logs)
    assert "while another device is connected" in text
    assert "Sennheiser HD 450BT" in text
    assert "Realtek" in text and "disconnect or power off" in text
    line = e.reg.status_sections()[0]
    assert ADDR in line
    assert "Sennheiser HD 450BT" in line and "likely an adapter limit" in line


def test_failed_sco_alone_does_not_blame_other_devices(tmp_path):
    e = make_env(tmp_path, connect_fail=99)
    slc(e)
    open_call(e)
    text = "\n".join(e.logs)
    assert "not the multi-device constraint" in text
    assert "Likely cause" not in text


def test_glasses_sco_failure_with_a_second_device_names_the_cause(
        monkeypatch):
    ga = _install_fakes(monkeypatch)
    core, reg, _ = _two_records(ga, monkeypatch, ("/g", "/c"))
    logs = []
    mgr = ha.AudioManager(ha.AudioDeps("/nonexistent", log=logs.append),
                          reg)
    mgr.bind_glasses(core)
    core.restore_a2dp = lambda: None
    sess = core.session
    sess.call_state = "active"
    sess.sock = FakeSock()
    sess.sock.settimeout = lambda t: None

    class RefusingSCO:
        def __init__(self, *a):
            pass

        def setsockopt(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            raise OSError(111, "Connection refused")

        def close(self):
            pass

    monkeypatch.setattr(ga.socket, "socket", RefusingSCO)
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    sess._open_sco_worker()
    text = "\n".join(logs)
    assert f"[{GLASSES_ADDR}] SCO connect failed" in text
    assert "Likely cause" in text and CORSAIR_ADDR in text
    rec = reg.get(GLASSES_ADDR)
    assert rec.audio_refusals == 1
    assert "likely an adapter limit" in rec.last_audio_reason
    assert "audio_refusals=1" in [l for l in reg.status_sections()
                                  if GLASSES_ADDR in l][0]


# -- Ruling 2: one SCO call at a time --------------------------------------

def _glasses_core(ga, e, state="ready"):
    core = ga.AGCore(None, None)
    core.state = state
    e.reg.admit(hr.DeviceInfo(GLASSES_ADDR, name="Meta Glasses 1A",
                              paired=True))
    started = []
    core.session = types.SimpleNamespace(
        alive=True, slc=True, call_state="idle", address=GLASSES_ADDR,
        start_dictation=lambda: started.append(1),
        stop_dictation=lambda: None)
    core.park_a2dp = lambda: None
    core.restore_a2dp = lambda: None
    e.mgr.bind_glasses(core)
    return core, started


def test_generic_refused_while_the_glasses_hold_the_call(
        tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, _ = _glasses_core(ga, e, state="live")
    slc(e)
    assert replies(say(e.sess, e.sock, "AT+BCC")) == ["ERROR"]
    assert e.audio.state == "idle"
    rec = e.reg.get(ADDR)
    assert rec.audio_refusals == 1
    assert "the glasses hold the call" in rec.last_audio_reason
    assert "one call at a time" in rec.last_audio_reason
    assert "audio_refusals=1" in e.reg.status_sections()[0]
    assert e.sess.alive and e.sess.slc
    assert e.sco == [] and e.pactl.cards[CARD] == "a2dp-sink"


def test_glasses_start_refused_while_a_generic_call_is_live(
        tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, started = _glasses_core(ga, e)
    slc(e)
    open_call(e)
    assert e.audio.state == "live"
    reply = core.cmd_start()
    assert reply.startswith("err: call audio busy: Corsair HS55")
    assert core.state == "ready" and started == []   # nothing started
    assert e.reg.get(GLASSES_ADDR).audio_refusals == 1
    # the generic call is undisturbed
    assert e.audio.state == "live"
    # ...and once it ends, the glasses can start
    e.audio.stop()
    assert core.cmd_start() == "ok: starting"
    assert core.state == "starting" and started == [1]


def test_glasses_start_unchanged_with_no_competing_call(
        tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, started = _glasses_core(ga, e)
    assert core.cmd_start() == "ok: starting"
    assert core.cmd_start() == "ok: already starting"
    assert started == [1]


def test_second_generic_device_refused_while_one_call_is_live(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    open_call(e)
    other = "AA:BB:CC:00:00:02"
    e.reg.admit(hr.DeviceInfo(other, name="Sennheiser HD 450BT",
                              paired=True))
    osock = FakeSock()
    osess = hg.GenericSession(osock, "/dev/o", other,
                              name="Sennheiser HD 450BT", registry=e.reg,
                              log=e.logs.append)
    e.reg.attach(other, osess)
    e.mgr.attach(osess)
    say(osess, osock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2",
        "AT+CMER=3,0,0,1", "AT+BIND=1,2", "AT+BIND?")
    assert replies(say(osess, osock, "AT+BCC")) == ["ERROR"]
    assert e.reg.get(other).audio_refusals == 1
    assert "Corsair HS55" in e.reg.get(other).last_audio_reason
    assert e.audio.state == "live"                  # first call untouched
    assert e.mgr.arbiter.generic_holder() == f"Corsair HS55 ({ADDR})"


# -- provisioning ---------------------------------------------------------

def test_nodes_are_named_after_the_device(tmp_path):
    e = make_env(tmp_path)
    ts = e.audio.tunnels
    assert ts.mic_desc == "Corsair HS55 Mic"
    assert ts.spk_desc == "Corsair HS55 Call Audio"
    assert ts.mic_node == "corsair_hs55_mic"
    argvs = [p.argv for p in e.procs]
    assert len(argvs) == 2
    for argv in argvs:
        assert argv[:4] == ["pw-cli", "-m", "load-module",
                            "libpipewire-module-pipe-tunnel"]
    assert 'node.description="Corsair HS55 Mic"' in argvs[0][-1]
    assert "tunnel.mode=source" in argvs[0][-1]
    assert 'node.description="Corsair HS55 Call Audio"' in argvs[1][-1]
    assert "tunnel.mode=sink" in argvs[1][-1]
    assert "audio.rate=16000 audio.channels=1" in argvs[0][-1]
    # per-device FIFOs live under the runtime dir
    assert ts.mic_fifo.startswith(str(tmp_path))
    assert os.path.exists(ts.mic_fifo) and os.path.exists(ts.spk_fifo)
    assert ts.pipe.started and ts.spk.started


def test_hostile_device_name_cannot_break_the_module_args(tmp_path):
    e = make_env(tmp_path, name='Evil" } exec\\ \x07Name')
    arg = e.procs[0].argv[-1]
    assert "\x07" not in arg and "\\" not in arg
    assert arg.count('"') % 2 == 0
    assert e.audio.prov == "ok"


def test_two_same_named_devices_get_distinct_node_names(tmp_path):
    e = make_env(tmp_path)
    other = "AA:BB:CC:00:00:99"
    e.reg.admit(hr.DeviceInfo(other, name="Corsair HS55", paired=True))
    osess = hg.GenericSession(FakeSock(), "/dev/o", other,
                              name="Corsair HS55", registry=e.reg,
                              log=e.logs.append)
    e.reg.attach(other, osess)
    e.mgr.attach(osess)
    assert osess.audio.tunnels.mic_node == "corsair_hs55_0099_mic"
    assert e.audio.tunnels.mic_node == "corsair_hs55_mic"


@pytest.mark.parametrize("mode", ["missing", "dies", "never"])
def test_provisioning_failure_degrades_to_a_clean_refusal(tmp_path, mode):
    e = make_env(tmp_path, pw=mode)
    assert e.audio.prov == "failed"
    assert e.audio.tunnels is None
    assert not any(p.rc is None for p in e.procs)    # nothing left loaded
    slc(e)
    assert e.sess.alive and e.sess.slc               # daemon/SLC alive
    assert replies(say(e.sess, e.sock, "AT+BCC")) == ["ERROR"]
    rec = e.reg.get(ADDR)
    assert rec.audio_refusals == 1
    assert "audio provisioning failed" in rec.last_audio_reason
    assert e.mgr.arbiter.generic_holder() is None    # no slot leaked
    assert e.sco == [] and e.pactl.cards[CARD] == "a2dp-sink"
    assert any("provisioning FAILED" in l for l in e.logs)
    assert "audio_nodes=failed" in e.sess.status_fields()
    # other protocol traffic still fine
    assert replies(say(e.sess, e.sock, "AT+VGS=7")) == ["OK"]


def test_failed_provisioning_is_retried_later(tmp_path):
    e = make_env(tmp_path, pw="missing")
    slc(e)
    assert e.audio.prov == "failed"
    # fix the environment, wait out the retry spacing
    e.deps.spawn_pwcli = lambda argv, tag="": FakeProc(argv, e.pactl)
    e.clock[0] += ha.REPROVISION_AFTER_S + 1
    assert replies(say(e.sess, e.sock, "AT+BCC")) == ["ERROR"]  # this one
    assert e.audio.prov == "ok"                                 # retried
    assert replies(say(e.sess, e.sock, "AT+BCC"))[0] == "OK"   # next works


def test_session_end_tears_down_nodes_fifos_and_parks(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    open_call(e)
    ts = e.audio.tunnels
    fifo_dir = ts.dir
    e.sess.run()                                    # EOF -> teardown
    assert all(p.terminated for p in e.procs)
    assert e.pactl.sources == [] and e.pactl.sinks == []
    assert not os.path.exists(fifo_dir)
    assert ts.pipe is None and ts.spk is None
    assert e.pactl.cards[CARD] == "a2dp-sink"       # restored
    assert e.mgr.arbiter.generic_holder() is None
    assert e.audio.closed
    assert e.reg.get(ADDR).session is None


def test_reconnect_replaces_the_old_audio_first(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    old_procs = list(e.procs)
    sess2 = hg.GenericSession(FakeSock(), "/dev/y", ADDR,
                              name="Corsair HS55", registry=e.reg,
                              log=e.logs.append)
    e.reg.attach(ADDR, sess2)
    e.mgr.attach(sess2)
    assert all(p.terminated for p in old_procs)
    assert e.audio.closed and sess2.audio.prov == "ok"
    assert sess2.audio.tunnels.mic_node == "corsair_hs55_mic"
    # the old session's late end-hook does not touch the new audio
    e.audio.shutdown()
    assert sess2.audio.prov == "ok" and not sess2.audio.closed


# -- activation: app attach (Ruling 3) -------------------------------------

def attach_app(e, kind="source"):
    ts = e.audio.tunnels
    if kind == "source":
        e.pactl.source_outputs.append(("9", e.pactl.idx(ts.mic_node)))
    else:
        e.pactl.sink_inputs.append(("9", e.pactl.idx(ts.spk_node)))


def test_app_attach_opens_and_detach_closes(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    e.mgr.poll_once()
    assert e.audio.state == "idle"                  # nothing attached
    attach_app(e)
    e.mgr.poll_once()
    assert e.audio.state == "offering"              # +BCS went out
    assert "+BCS: 2" in e.sock.sent.decode()
    say(e.sess, e.sock, "AT+BCS=2")
    assert e.audio.state == "live" and e.audio.trigger == "app attach"
    e.pactl.source_outputs.clear()                  # app goes away
    for _ in range(ha.DETACH_POLLS - 1):
        e.mgr.poll_once()
        assert e.audio.state == "live"              # debounced
    e.mgr.poll_once()
    assert e.audio.state == "idle"
    assert e.pactl.cards[CARD] == "a2dp-sink"
    assert e.mgr.arbiter.generic_holder() is None


def test_playback_stream_on_the_call_sink_also_counts(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    attach_app(e, "sink")
    e.mgr.poll_once()
    assert e.audio.state == "offering"


def test_streams_on_other_nodes_do_not_trigger(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    e.pactl.add_node("source", "some_other_mic")
    e.pactl.source_outputs.append(("9", e.pactl.idx("some_other_mic")))
    e.mgr.poll_once()
    assert e.audio.state == "idle"


def test_headset_hold_survives_app_detach(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    open_call(e)                                    # headset asked (BCC)
    attach_app(e)
    e.mgr.poll_once()
    e.pactl.source_outputs.clear()
    for _ in range(ha.DETACH_POLLS + 2):
        e.mgr.poll_once()
    assert e.audio.state == "live"                  # BCC still holds


def test_hangup_with_app_still_attached_does_not_reopen(
        tmp_path, _no_bridge_threads):
    e = make_env(tmp_path)
    slc(e)
    attach_app(e)
    e.mgr.poll_once()
    say(e.sess, e.sock, "AT+BCS=2")
    _no_bridge_threads[0]._ended()                  # headset hung up
    for _ in range(3):
        e.mgr.poll_once()
    assert e.audio.state == "idle"
    assert len(e.sco) == 1                          # no retry storm


def test_refused_attach_retries_when_the_slot_frees(tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, _ = _glasses_core(ga, e, state="live")
    slc(e)
    attach_app(e)
    e.mgr.poll_once()
    assert e.audio.state == "idle"
    assert e.reg.get(ADDR).audio_refusals == 1
    e.mgr.poll_once()                               # inside the backoff
    assert e.reg.get(ADDR).audio_refusals == 1      # no spam
    core.state = "ready"                            # glasses call ended
    e.clock[0] += ha.ATTACH_RETRY_S + 1
    e.mgr.poll_once()
    assert e.audio.state == "offering"


# -- control surface --------------------------------------------------------

class GlassesStub:
    def __init__(self):
        self.calls = []

    def status(self):
        return "state=ready glasses_connected=True call=idle discord=off"

    def cmd_start(self):
        self.calls.append("start")
        return "ok: starting"

    def cmd_stop(self):
        self.calls.append("stop")
        return "ok: stopped"

    def cmd_toggle(self):
        self.calls.append("toggle")
        return "ok: starting"

    def cmd_media(self):
        return "ok: media"

    def cmd_discord(self, a):
        return "ok: discord " + a


def router(e):
    return hr.ControlRouter(e.reg, GlassesStub())


def test_glasses_verbs_are_byte_identical_without_a_device(tmp_path):
    e = make_env(tmp_path)
    r = router(e)
    assert r.handle("start") == "ok: starting"
    assert r.handle("stop") == "ok: stopped"
    assert r.handle("toggle") == "ok: starting"
    assert r.handle("media") == "ok: media"
    assert r.handle("discord_on") == "ok: discord on"
    assert r.handle("bogus") == "err: unknown command 'bogus'"
    assert r.handle("status").splitlines()[0] == \
        "ok: state=ready glasses_connected=True call=idle discord=off"
    assert r.handle("start glasses") == "ok: starting"


def test_status_shows_audio_state_per_device(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    lines = router(e).handle("status").splitlines()
    assert lines[1].startswith(f"device {ADDR} ")
    assert "audio=idle" in lines[1] and "audio_nodes=ok" in lines[1]


def test_status_for_one_device_by_name_or_address(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    for sel in ("corsair", "Corsair HS55", ADDR, ADDR.lower()):
        out = router(e).handle(f"status {sel}")
        assert out.startswith(f"ok: device {ADDR} ")
        assert 'mic_node="Corsair HS55 Mic" (corsair_hs55_mic)' in out
        assert 'call_node="Corsair HS55 Call Audio"' in out
        assert "hf_codecs=1,2" in out


def test_start_stop_target_a_device(tmp_path):
    e = make_env(tmp_path)
    slc(e)
    r = router(e)
    assert r.handle("start corsair") == "ok: starting"
    assert e.audio.state == "offering" and e.audio.manual_hold
    assert r.handle("start corsair") == "ok: already starting"
    say(e.sess, e.sock, "AT+BCS=2")
    assert e.audio.state == "live"
    assert r.handle("start corsair") == "ok: already live"
    # a manual hold survives an app detach; only stop ends it
    attach_app(e)
    e.mgr.poll_once()
    e.pactl.source_outputs.clear()
    for _ in range(5):
        e.mgr.poll_once()
    assert e.audio.state == "live"
    assert r.handle("stop corsair") == "ok: stopped"
    assert e.audio.state == "idle"
    assert r.handle("stop corsair") == "ok: not live"
    assert r.handle("toggle corsair") == "ok: starting"
    assert r.handle("toggle corsair") == "ok: stopped"


def test_device_verb_errors_are_clean(tmp_path):
    e = make_env(tmp_path)
    r = router(e)
    assert r.handle("status nope").startswith("err: no such device 'nope'")
    e.reg.admit(hr.DeviceInfo("AA:BB:CC:00:00:02", name="Corsair HS80"))
    assert "ambiguous" in r.handle("stop corsair")
    # not connected / no SLC
    e2 = make_env(tmp_path / "x")
    assert router(e2).handle("start corsair") == \
        "err: device not connected (no completed SLC)"
    e2.sess.alive = False
    assert router(e2).handle("start corsair").endswith("is not connected")


def test_start_refused_by_one_call_rule_reports_the_reason(
        tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    _glasses_core(ga, e, state="live")
    slc(e)
    out = router(e).handle("start corsair")
    assert out.startswith("err: call audio busy: the glasses hold the call")


# -- bridge + helpers -------------------------------------------------------

def test_msbc_bridge_decodes_uplink_and_frames_downlink_as_h2():
    mic = FakeStream("m")
    spk = FakeStream("s")
    dec = types.SimpleNamespace(decode_frame=lambda f: b"\x05\x00" * 120)
    enc = types.SimpleNamespace(
        encode_frame=lambda p: bytes(range(57)))
    sock = FakeSco()
    b = ha.ScoBridge(sock, hg.CODEC_MSBC, dec, enc, mic, spk,
                     lambda m: None, "[x]")
    pkt = b"\x01\xC8" + b"\xAD\x00\x00" + b"\x11" * 54 + b"\x00"
    assert len(pkt) == 60
    assert b.feed(pkt) == b"\x05\x00" * 120
    assert mic.pushed == [b"\x05\x00" * 120]
    for _ in range(5):
        assert b.downlink_step() is True
    heads = [u[:2] for u in sock.sent]
    assert heads == [bytes((1, h)) for h in (0xC8, 0xF8, 0x08, 0x38, 0xC8)]
    assert all(len(u) == 60 and u[-1] == 0 for u in sock.sent)


def test_cvsd_bridge_resamples_both_ways():
    mic = FakeStream("m")
    spk = FakeStream("s")
    sock = FakeSco()
    b = ha.ScoBridge(sock, hg.CODEC_CVSD, None, None, mic, spk,
                     lambda m: None, "[x]", mtu=48)
    pcm8 = b"".join(int(v).to_bytes(2, "little", signed=True)
                    for v in (100, 200, 300))
    out = b.feed(pcm8)
    assert len(out) == 12                            # 3 -> 6 samples
    assert out[2:4] == (100).to_bytes(2, "little", signed=True)
    b.downlink_step()                                # 120 B per step
    b.downlink_step()
    assert sock.sent and all(len(c) == 48 for c in sock.sent)


def test_bridge_end_reports_once_and_close_is_silent():
    ended = []
    b = ha.ScoBridge(FakeSco(), hg.CODEC_CVSD, None, None,
                     FakeStream("m"), FakeStream("s"), lambda m: None,
                     "[x]", on_closed=ended.append)
    b._ended()
    b._ended()
    assert len(ended) == 1
    b2 = ha.ScoBridge(FakeSco(), hg.CODEC_CVSD, None, None,
                      FakeStream("m"), FakeStream("s"), lambda m: None,
                      "[x]", on_closed=ended.append)
    b2.close()                                      # we closed it ourselves
    b2._ended()
    assert len(ended) == 1


def test_resample_helpers_roundtrip_length():
    pcm16 = b"\x10\x00" * 240
    assert len(ha.downsample_2x(pcm16)) == 240
    up, last = ha.upsample_2x(ha.downsample_2x(pcm16))
    assert len(up) == 480 and last == 0x10


def test_stale_tunnel_processes_from_a_dead_daemon_are_reaped(tmp_path):
    proc = tmp_path / "proc"
    rt = "/run/user/1000/glasses-ag"
    def mk(pid, *argv):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv)
                                    + b"\0")
    mk(11, "pw-cli", "-m", "load-module", "m",
       f'pipe.filename="{rt}/dev/112233445566/mic.fifo"')
    mk(12, "pw-cli", "-m", "load-module", "m",
       'pipe.filename="/run/user/1000/glasses-ag/mic.fifo"')   # static
    mk(13, "python3", f"{rt}/dev/x")                             # other
    (proc / "self").mkdir()
    killed = []
    hit = ha.kill_stale_tunnels(rt, str(proc),
                                kill=lambda p, s: killed.append((p, s)),
                                log=lambda m: None)
    assert hit == [11] and killed == [(11, 15)]


# -- status lifecycle: stale audio_reason ----------------------------------

def _second_headset(e, addr="AA:BB:CC:00:00:02", name="Sennheiser HD 450BT"):
    e.reg.admit(hr.DeviceInfo(addr, name=name, paired=True))
    sock = FakeSock()
    sess = hg.GenericSession(sock, "/dev/o", addr, name=name,
                             registry=e.reg, log=e.logs.append)
    e.reg.attach(addr, sess)
    e.mgr.attach(sess)
    say(sess, sock, f"AT+BRSF={HF_FEATURES}", "AT+BAC=1,2",
        "AT+CMER=3,0,0,1", "AT+BIND=1,2", "AT+BIND?")
    return sess, sock


def _line(e, addr):
    return [l for l in e.reg.status_sections() if f"device {addr} " in l][0]


def test_busy_reason_clears_when_the_call_frees(tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, _ = _glasses_core(ga, e)
    slc(e)
    open_call(e)                                   # Corsair holds the call
    assert core.cmd_start().startswith("err: call audio busy")
    line = _line(e, GLASSES_ADDR)
    assert "audio_refusals=1" in line
    assert 'audio_reason="call audio busy: Corsair HS55' in line
    e.audio.stop()                                 # arbiter frees
    line = _line(e, GLASSES_ADDR)
    assert "audio_refusals=1" in line              # counter stays
    assert "audio_reason" not in line              # no longer current
    assert 'last_refusal="call audio busy: Corsair HS55' in line


def test_busy_reason_clears_when_the_named_device_disconnects(
        tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, _ = _glasses_core(ga, e)
    slc(e)
    open_call(e)
    core.cmd_start()
    assert "audio_reason=" in _line(e, GLASSES_ADDR)
    e.sess.run()                                   # Corsair disconnects
    assert e.reg.get(ADDR).link() == "down"
    line = _line(e, GLASSES_ADDR)
    assert "audio_reason" not in line and "last_refusal=" in line


def test_busy_reason_naming_the_glasses_clears_when_their_call_ends(
        tmp_path, monkeypatch):
    ga = _install_fakes(monkeypatch)
    e = make_env(tmp_path)
    core, _ = _glasses_core(ga, e, state="live")
    slc(e)
    say(e.sess, e.sock, "AT+BCC")
    assert "audio_reason=" in _line(e, ADDR)
    core.state = "ready"
    assert "audio_reason" not in _line(e, ADDR)
    assert "last_refusal=" in _line(e, ADDR)


def test_sco_reason_clears_when_the_other_device_goes_away(tmp_path):
    e = make_env(tmp_path, connect_fail=99)
    osess, _ = _second_headset(e)
    slc(e)
    open_call(e)                                   # fails: peer connected
    assert "audio_reason=" in _line(e, ADDR)
    osess.alive = False                            # Sennheiser link down
    line = _line(e, ADDR)
    assert "audio_reason" not in line and "last_refusal=" in line
    assert "audio_refusals=1" in line


def test_reason_naming_nobody_stays_current(tmp_path):
    e = make_env(tmp_path, pw="missing")
    slc(e)
    say(e.sess, e.sock, "AT+BCC")
    assert "audio_reason=\"audio provisioning failed" in _line(e, ADDR)
