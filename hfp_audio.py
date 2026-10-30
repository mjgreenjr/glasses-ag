"""hfp_audio — the generic-device audio path (v2 Step C).

Pure Python: no D-Bus, no BlueZ, no PipeWire, no root at import time.
Every contact with the outside world (pactl, pw-cli, the SCO socket,
libsbc, time) goes through an injectable AudioDeps bundle, so the whole
path is testable at a fake boundary. NOTHING HERE IS HARDWARE-VERIFIED.

What lives here
  * CallArbiter       — Ruling 2: one SCO call at a time across ALL
                        devices (the glasses count).
  * A2dpParker        — per-device A2DP park/restore, same invariants
                        as the glasses choreography in AGCore.
  * TunnelSet         — per-device FIFOs + PipeWire pipe-tunnel modules
                        (Ruling 1 naming), ifexists/nofail discipline.
  * ScoBridge         — SCO socket <-> FIFOs for one live call (mSBC via
                        the shared hfp_media helpers; CVSD via a plain
                        8k<->16k resampler).
  * GenericAudio      — one generic session's audio lifecycle: offer ->
                        confirm -> SCO -> close, triggers, teardown.
  * AudioManager      — owns the arbiter, the per-address GenericAudio
                        instances and the pactl app-attach watcher
                        (Ruling 3).
  * explain_sco_failure — the platform-constraint diagnostic: a failed
                        SCO connect while another device is connected
                        names the likely cause instead of a bare errno.

The glasses path (AGCore/Session) is the frozen baseline. It touches
this module in exactly two places: AGCore.cmd_start consults the
arbiter, and Session._open_sco_worker calls explain_sco_failure when
its SCO connect fails.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from array import array
from datetime import datetime

import hfp_media
from hfp_generic import CODEC_CVSD, CODEC_MSBC

# -- constants ---------------------------------------------------------------

BT_VOICE_MSBC = 0x0063          # same setting the glasses path proved
BT_VOICE_CVSD = 0x0060          # CVSD_16BIT: linear 16-bit PCM at 8 kHz
SOL_BLUETOOTH = 274
BT_VOICE = 11
SOL_SCO = 17
SCO_OPTIONS = 1

TUNNEL_MODULE = "libpipewire-module-pipe-tunnel"
PCM_RATE = 16000                # tunnel nodes are always 16 kHz mono s16le

ATTACH_POLL_S = 1.0             # pactl watch period (Ruling 3)
DETACH_POLLS = 3                # consecutive "not attached" polls to detach
ATTACH_RETRY_S = 10.0           # re-try spacing for a refused attach
OFFER_TIMEOUT_S = 6.0           # handshake watchdog (not a call timer)
REPROVISION_AFTER_S = 30.0
SCO_ATTEMPTS = 8

PLATFORM_HINT = (
    "on a single Bluetooth adapter a connected second device can make "
    "the controller refuse the call-audio (SCO) link — observed on a "
    "Realtek adapter (HCI status 0x1F, Errno 38/111), may vary by "
    "adapter; disconnect or power off the other device(s) and retry")


def _default_log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    print(line, flush=True)


# -- real-world adapters (only touched at runtime, never in tests) -----------

def real_pactl(*args):
    try:
        r = subprocess.run(["pactl", *args], capture_output=True,
                           text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


class _PwCliProc:
    """A long-lived `pw-cli -m load-module ...` child. The module lives
    exactly as long as the process does: terminate() unloads it. If the
    daemon is killed hard the child survives it; AudioManager.
    reset_runtime() reaps such strays at the next daemon start
    (kill_stale_tunnels)."""

    def __init__(self, argv, log, tag):
        self.argv = argv
        self.p = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._pump, args=(log, tag),
                         daemon=True, name="pw-cli-out").start()

    def _pump(self, log, tag):
        try:
            for line in self.p.stdout:
                line = line.rstrip()
                if line:
                    log(f"{tag} pw-cli: {line}")
        except (OSError, ValueError):
            pass

    def poll(self):
        return self.p.poll()

    def terminate(self):
        self.p.terminate()

    def kill(self):
        self.p.kill()

    def wait(self, timeout=None):
        return self.p.wait(timeout)


def kill_stale_tunnels(runtime_dir, proc_root="/proc", kill=os.kill,
                       log=_default_log):
    """Terminate `pw-cli` processes a previous daemon left behind: any
    pw-cli whose command line names a FIFO under our device directory.
    Returns the pids signalled."""
    marker = os.path.join(runtime_dir, "dev") + os.sep
    hit = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return hit
    for ent in entries:
        if not ent.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, ent, "cmdline"), "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if (argv and os.path.basename(argv[0].decode(errors="replace"))
                == "pw-cli"
                and any(marker.encode() in a for a in argv)):
            try:
                kill(int(ent), 15)
                hit.append(int(ent))
                log(f"reaped stale pw-cli tunnel process {ent} from a "
                    "previous daemon")
            except OSError:
                pass
    return hit


def real_spawn_pwcli(argv, log=_default_log, tag=""):
    return _PwCliProc(argv, log, tag)


def real_sco_connect(address, codec):
    """Open an SCO link to `address`; returns (socket, mtu). Raises
    OSError on refusal (the platform-constraint case: 0x1F)."""
    import socket
    import struct
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                      socket.BTPROTO_SCO)
    try:
        voice = BT_VOICE_MSBC if codec == CODEC_MSBC else BT_VOICE_CVSD
        s.setsockopt(SOL_BLUETOOTH, BT_VOICE, struct.pack("<H", voice))
        s.settimeout(3)
        s.connect(address)
        s.settimeout(None)
        try:
            mtu = struct.unpack(
                "<H", s.getsockopt(SOL_SCO, SCO_OPTIONS, 2))[0] or 48
        except OSError:
            mtu = 48
        return s, mtu
    except OSError:
        s.close()
        raise


class AudioDeps:
    """Everything GenericAudio touches outside pure Python. Tests pass
    fakes; the daemon uses defaults()."""

    def __init__(self, runtime_dir, log=_default_log, pactl=None,
                 spawn_pwcli=None, sco_connect=None, make_decoder=None,
                 make_encoder=None, make_pipe=None, make_spk=None,
                 sleep=time.sleep, clock=time.monotonic, spawn=None,
                 timer=None, watch=True):
        self.runtime_dir = runtime_dir
        self.log = log
        self.pactl = pactl or real_pactl
        self.spawn_pwcli = spawn_pwcli or (
            lambda argv, tag="": real_spawn_pwcli(argv, log, tag))
        self.sco_connect = sco_connect or real_sco_connect
        self.make_decoder = make_decoder or hfp_media.MSBCDecoder
        self.make_encoder = make_encoder or hfp_media.MSBCEncoder
        self.make_pipe = make_pipe or hfp_media.PipeSource
        self.make_spk = make_spk or hfp_media.SpkSink
        self.sleep = sleep
        self.clock = clock
        self.watch = watch              # run the pactl attach watcher
        self.spawn = spawn or self._spawn_thread
        self.timer = timer or self._start_timer

    @staticmethod
    def _spawn_thread(fn, *args, name="audio"):
        threading.Thread(target=fn, args=args, daemon=True,
                         name=name).start()

    @staticmethod
    def _start_timer(delay, fn):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t


# -- naming (Ruling 1) --------------------------------------------------------

def _clean(text):
    """Printable, quote-free text safe to put inside an SPA-JSON string
    and a log line."""
    return re.sub(r'[\x00-\x1f\x7f"\\]', "", text or "").strip()


def display_name(name, address):
    name = _clean(name)
    return name or f"Bluetooth {address[-8:]}"


def node_slug(name, address):
    slug = re.sub(r"[^a-z0-9]+", "_", display_name(name, address).lower())
    return slug.strip("_") or "device"


# -- one-call-at-a-time (Ruling 2) --------------------------------------------

class CallArbiter:
    """Exactly one SCO call across all devices. The glasses count: their
    call is "live" while AGCore.state is starting|live, read through
    `glasses_busy` (so it can never be left set by a missed release).
    Generic calls claim/release explicitly. Lock order is always
    core.lock -> arbiter.lock (glasses) or arbiter.lock alone
    (generic): claim/begin are check-and-set under `lock`."""

    def __init__(self, registry=None, log=_default_log):
        self.lock = threading.RLock()
        self.registry = registry
        self.log = log
        self.glasses_busy = lambda: False
        self.glasses_label = "the glasses"
        self.glasses_address = lambda: None
        self.last_peers = ()        # who the latest busy refusal named
        self._generic = {}          # address -> display name

    def holders(self):
        """Addresses currently holding the call (glasses included)."""
        with self.lock:
            out = set(self._generic)
            if self.glasses_busy() and self.glasses_address():
                out.add(self.glasses_address())
            return out

    def generic_holder(self):
        """'Name (addr)' of a generic device holding the call, or None.
        Caller holds `lock` when it needs the answer to stay true."""
        with self.lock:
            for addr, name in self._generic.items():
                return f"{name} ({addr})"
        return None

    def claim_generic(self, address, name):
        with self.lock:
            if address in self._generic:
                return True, ""
            if self.glasses_busy():
                self.last_peers = (self.glasses_address(),)
                return False, (f"call audio busy: {self.glasses_label} "
                               "hold the call (one call at a time)")
            for addr, other in self._generic.items():
                self.last_peers = (addr,)
                return False, (f"call audio busy: {other} ({addr}) holds "
                               "the call (one call at a time)")
            self._generic[address] = name
            return True, ""

    def release_generic(self, address):
        with self.lock:
            return self._generic.pop(address, None) is not None

    def glasses_refusal(self):
        """Called by AGCore.cmd_start WITH arbiter.lock held, just
        before it sets state=starting. Returns a reason or None."""
        holder = self.generic_holder()
        if holder:
            self.last_peers = tuple(self._generic)
            return (f"call audio busy: {holder} holds the call "
                    "(one call at a time)")
        return None

    def note_success(self, address):
        if self.registry is not None and address:
            self.registry.note_audio_success(address)

    def note_refusal(self, address, reason):
        """Record a "call audio busy" refusal naming the holder(s)."""
        if self.registry is not None and address:
            self.registry.note_audio_refusal(
                address, reason, peers=self.last_peers, kind="busy")


# -- platform-constraint diagnostic -------------------------------------------

def explain_sco_failure(registry, address, exc, log=_default_log):
    """A SCO connect to `address` failed. If another device is
    connected, say so: that is the documented platform constraint
    (README, "Known limitation"). Records the reason on the device's
    status line and returns it."""
    peers = []
    if registry is not None:
        peers = registry.live_peers(address)
    if peers:
        names = ", ".join(f"{n or 'unnamed'} ({a})" for a, n in peers)
        log(f"[{address}] SCO connect failed ({exc}) while another "
            f"device is connected: {names}. Likely cause: {PLATFORM_HINT}")
        reason = (f"SCO connect failed ({exc}); other device(s) "
                  f"connected ({', '.join(n or a for a, n in peers)}) — "
                  "likely an adapter limit: disconnect or power off the "
                  "other device(s)")
    else:
        log(f"[{address}] SCO connect failed ({exc}); no other device "
            "is connected, so this is not the multi-device constraint")
        reason = f"SCO connect failed ({exc})"
    if registry is not None and address:
        registry.note_audio_refusal(address, reason,
                                    peers=[a for a, _ in peers],
                                    kind="sco")
    return reason


# -- A2DP park/restore (per device) ------------------------------------------

class A2dpParker:
    """The glasses choreography's invariants, per device: park only a
    card that is on a2dp-sink; restore only a card WE parked; adopt an
    orphaned park (card on "off" that this process did not park) at
    connect. Best-effort: a missing/erroring pactl never blocks a call."""

    def __init__(self, address, pactl, log=_default_log,
                 sleep=time.sleep):
        self.address = address
        self.card = "bluez_card." + address.replace(":", "_")
        self.pactl = pactl
        self.log = log
        self.sleep = sleep
        self.parked = False
        self.lock = threading.Lock()

    def card_profile(self):
        out = self.pactl("list", "cards")
        if not out:
            return None
        m = re.search(r"Name: " + re.escape(self.card) +
                      r"\n(?:.*\n)*?\tActive Profile: (\S+)", out)
        return m.group(1) if m else None

    def park(self):
        if self.card_profile() != "a2dp-sink":
            return False
        if self.pactl("set-card-profile", self.card, "off") is None:
            return False
        with self.lock:
            self.parked = True
        self.log(f"[{self.address}] parked A2DP for the call")
        self.sleep(0.2)             # let the transport close first
        return True

    def restore(self):
        with self.lock:
            if not self.parked:
                return False
            self.parked = False
        if self.pactl("set-card-profile", self.card,
                      "a2dp-sink") is not None:
            self.log(f"[{self.address}] restored A2DP")
            return True
        self.log(f"[{self.address}] A2DP restore failed (card gone?)")
        return False

    def reconcile(self):
        with self.lock:
            if self.parked:
                return False
        if self.card_profile() != "off":
            return False
        if self.pactl("set-card-profile", self.card,
                      "a2dp-sink") is not None:
            self.log(f"[{self.address}] adopted orphaned A2DP park — "
                     "restored media mode")
            return True
        return False


# -- resampling for the CVSD (8 kHz) fallback ---------------------------------

def upsample_2x(pcm8, prev=0):
    """8 kHz s16le -> 16 kHz by linear interpolation. Returns
    (pcm16, last_sample) so a stream can be fed in arbitrary pieces."""
    n = len(pcm8) // 2
    src = array("h")
    src.frombytes(pcm8[:n * 2])
    out = array("h")
    for x in src:
        out.append((prev + x) // 2)
        out.append(x)
        prev = x
    return out.tobytes(), prev


def downsample_2x(pcm16):
    """16 kHz s16le -> 8 kHz by averaging sample pairs."""
    n = len(pcm16) // 4 * 2
    src = array("h")
    src.frombytes(pcm16[:n * 2])
    return array("h", ((src[i] + src[i + 1]) // 2
                       for i in range(0, len(src) - 1, 2))).tobytes()


# -- the live call's data path -------------------------------------------------

class ScoBridge:
    """SCO socket <-> the device's two FIFOs for one call. mSBC reuses
    the shared decoder/encoder/parser and the H2 framing the glasses
    path measured; CVSD is raw 8 kHz PCM resampled to the nodes' 16 kHz.
    Reader and downlink are separate daemon threads; either ending
    closes the call through on_closed."""

    def __init__(self, sock, codec, decoder, encoder, mic, spk, log,
                 tag, mtu=48, on_closed=None, sleep=time.sleep):
        self.sock = sock
        self.codec = codec
        self.decoder = decoder
        self.encoder = encoder
        self.parser = hfp_media.SCOParser(decoder) if decoder else None
        self.mic = mic
        self.spk = spk
        self.log = log
        self.tag = tag
        self.mtu = max(mtu, 2)
        self.on_closed = on_closed
        self.sleep = sleep
        self.closed = False
        self._idx = 0
        self._prev = 0
        self._cvsd_out = bytearray()
        self.send_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self.reader, daemon=True,
                         name="sco-reader").start()
        threading.Thread(target=self.downlink, daemon=True,
                         name="sco-downlink").start()

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _ended(self):
        was_closed, self.closed = self.closed, True
        try:
            self.sock.close()
        except OSError:
            pass
        if not was_closed and self.on_closed is not None:
            self.on_closed(self)

    # uplink: headset mic -> FIFO
    def feed(self, data):
        if self.codec == CODEC_MSBC:
            pcm = self.parser.feed(data)
        else:
            pcm, self._prev = upsample_2x(data, self._prev)
        if pcm:
            self.mic.push(pcm)
        return pcm

    def reader(self):
        try:
            while not self.closed:
                data = self.sock.recv(1024)
                if not data:
                    break
                self.feed(data)
        except OSError:
            pass
        self.log(f"{self.tag} SCO closed")
        self._ended()

    # downlink: speaker FIFO -> headset
    def downlink_step(self):
        """Send one 7.5 ms worth of call audio. Returns False when the
        link is gone."""
        if self.closed:
            return False
        pcm = self.spk.take()
        try:
            if self.codec == CODEC_MSBC:
                frame = self.encoder.encode_frame(pcm)
                if len(frame) != 57:
                    return True
                unit = (bytes((0x01, hfp_media.H2_HEADER_B[self._idx % 4]))
                        + frame + b"\x00")
                self._idx += 1
                with self.send_lock:
                    self.sock.sendall(unit)
            else:
                self._cvsd_out += downsample_2x(pcm)
                while len(self._cvsd_out) >= self.mtu:
                    chunk = bytes(self._cvsd_out[:self.mtu])
                    del self._cvsd_out[:self.mtu]
                    with self.send_lock:
                        self.sock.sendall(chunk)
        except OSError:
            return False
        return True

    def downlink(self):
        next_t = time.monotonic()
        while self.downlink_step():
            next_t += 0.0075
            delay = next_t - time.monotonic()
            if delay > 0:
                self.sleep(delay)
            elif delay < -0.1:
                next_t = time.monotonic()
        self._ended()


# -- per-device PipeWire provisioning ------------------------------------------

def tunnel_args(mode, fifo, node_name, description):
    """SPA-JSON argument string for one pipe-tunnel module; same
    properties as the static 52/53 conf pair. Strings are quoted and
    cleaned (no quotes/backslashes/control chars survive)."""
    q = lambda s: json.dumps(_clean(s), ensure_ascii=False)
    return (f"tunnel.mode={mode} pipe.filename={q(fifo)} "
            f"node.name={q(node_name)} node.description={q(description)} "
            f'audio.format="S16LE" audio.rate={PCM_RATE} audio.channels=1')


class TunnelSet:
    """FIFOs + two pipe-tunnel modules for one device. Each module is a
    long-lived `pw-cli -m load-module` child process: pw-cli loads
    modules into its OWN context, so the module exists exactly as long
    as the process — terminating the process is the unload. Failure at
    any step is reported, never raised: the caller degrades that one
    device, never the daemon or the audio stack."""

    def __init__(self, address, name, deps, taken_names=()):
        self.address = address
        self.deps = deps
        self.tag = f"[{address}]"
        dn = display_name(name, address)
        slug = node_slug(name, address)
        suffix = ""
        if f"{slug}_mic" in taken_names or f"{slug}_call" in taken_names:
            suffix = "_" + address.replace(":", "")[-4:].lower()
        self.mic_node = f"{slug}{suffix}_mic"
        self.spk_node = f"{slug}{suffix}_call"
        self.mic_desc = f"{dn} Mic"
        self.spk_desc = f"{dn} Call Audio"
        base = os.path.join(deps.runtime_dir, "dev",
                            address.replace(":", ""))
        self.dir = base
        self.mic_fifo = os.path.join(base, "mic.fifo")
        self.spk_fifo = os.path.join(base, "spk.fifo")
        self.pipe = None
        self.spk = None
        self.procs = []

    @property
    def node_names(self):
        return (self.mic_node, self.spk_node)

    def _visible(self):
        def has(kind, name):
            out = self.deps.pactl("list", "short", kind)
            if not out:
                return False
            return any(len(c) > 1 and c[1] == name for c in
                       (ln.split("\t") for ln in out.splitlines()))
        return has("sources", self.mic_node) and has("sinks", self.spk_node)

    def provision(self):
        """Returns (ok, error_text)."""
        d = self.deps
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            for f in (self.mic_fifo, self.spk_fifo):
                try:
                    os.mkfifo(f, 0o600)
                except FileExistsError:
                    pass
        except OSError as e:
            return False, f"FIFO setup failed: {e}"
        try:
            self.pipe = d.make_pipe(self.mic_fifo)
            self.spk = d.make_spk(self.spk_fifo)
            self.pipe.start()
            self.spk.start()
            for mode, fifo, node, desc in (
                    ("source", self.mic_fifo, self.mic_node,
                     self.mic_desc),
                    ("sink", self.spk_fifo, self.spk_node,
                     self.spk_desc)):
                argv = ["pw-cli", "-m", "load-module", TUNNEL_MODULE,
                        tunnel_args(mode, fifo, node, desc)]
                d.log(f"{self.tag} loading tunnel: {desc!r} as {node} "
                      f"({mode}, {fifo})")
                self.procs.append(d.spawn_pwcli(argv, self.tag))
        except OSError as e:
            return False, f"pw-cli unavailable: {e}"
        except Exception as e:      # a provisioning bug must not kill us
            return False, f"provisioning error: {e!r}"
        for _ in range(12):
            if any(p.poll() is not None for p in self.procs):
                return False, ("pw-cli exited before the tunnel nodes "
                               "appeared (module missing or rejected "
                               "its arguments — see the pw-cli lines "
                               "above)")
            if self._visible():
                d.log(f"{self.tag} tunnel nodes up: {self.mic_desc!r}, "
                      f"{self.spk_desc!r}")
                return True, ""
            d.sleep(0.5)
        return False, ("tunnel nodes did not appear in PipeWire within "
                       "6 s")

    def teardown(self):
        for p in self.procs:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(2)
                    except Exception:
                        p.kill()
            except OSError:
                pass
        self.procs = []
        for s in (self.pipe, self.spk):
            if s is not None:
                try:
                    s.stop()
                except Exception:
                    pass
        self.pipe = self.spk = None
        for f in (self.mic_fifo, self.spk_fifo):
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


# -- one generic session's audio lifecycle -------------------------------------

class GenericAudio:
    """States: idle -> offering (claimed, +BCS out / waiting) ->
    opening (SCO connecting) -> live. Anything failing returns to idle
    through _finish(), which always releases the call slot and restores
    A2DP if (and only if) this device parked it."""

    def __init__(self, session, deps, manager):
        self.sess = session
        self.deps = deps
        self.mgr = manager
        self.address = session.address
        self.name = display_name(session.name, session.address)
        self.log = deps.log
        self.tag = f"[{self.address}]"
        self.lock = threading.RLock()
        self.state = "idle"
        self.prov = "pending"           # pending | ok | failed
        self.prov_error = ""
        self.prov_at = None
        self.tunnels = None
        self.parker = A2dpParker(self.address, deps.pactl, deps.log,
                                 deps.sleep)
        self.bridge = None
        self.codec = None
        self.closed = False
        self.trigger = ""
        # holds: SCO stays open while any is set
        self.bcc_hold = False
        self.manual_hold = False
        self.app_attached = False
        self.app_pending = False
        self._detach_polls = 0
        self._next_attach_try = 0.0
        self._offer_timer = None
        self._gen = 0                   # call generation (stale timers)
        self._post_ack = None

    # -- provisioning ----------------------------------------------------
    def provision(self):
        with self.lock:
            if self.closed:
                return False
            self.prov = "pending"
            self.prov_at = self.deps.clock()
            ts = TunnelSet(self.address, self.sess.name, self.deps,
                           self.mgr.taken_node_names(self))
            self.tunnels = ts
        ok, err = ts.provision()
        with self.lock:
            if self.closed:
                ts.teardown()
                return False
            if ok:
                self.prov, self.prov_error = "ok", ""
            else:
                self.prov, self.prov_error = "failed", err
                self.log(f"{self.tag} audio provisioning FAILED: {err}; "
                         "this device keeps its SLC but audio-open will "
                         "be refused")
                ts.teardown()
                self.tunnels = None
        return ok

    def on_slc(self):
        self.deps.spawn(self.parker.reconcile, name="a2dp-reconcile")

    # -- refusal ---------------------------------------------------------
    def _refuse(self, source, reason, busy=False):
        self.log(f"{self.tag} audio-open refused ({source}): {reason}")
        self.mgr.note_refusal(self.address, reason, busy=busy)
        return False

    # -- request (the audio_opener seam) ---------------------------------
    def request(self, source):
        """GenericSession.request_audio(source) lands here. Sources:
        'AT+BCC' (headset asked), 'app attach', 'control start', and
        'codec confirmed' (the BCS answer: proceed to SCO)."""
        if source == "codec confirmed":
            return self._on_codec_confirmed()
        with self.lock:
            if self.closed:
                return self._refuse(source, "session is closing")
            if self.state != "idle":
                self.log(f"{self.tag} audio request ({source}) while "
                         f"{self.state}; already in progress")
                if source == "AT+BCC":
                    self.bcc_hold = True
                return self.state in ("offering", "opening", "live")
            if self.prov != "ok":
                if (self.prov == "failed" and self.prov_at is not None
                        and self.deps.clock() - self.prov_at
                        >= REPROVISION_AFTER_S):
                    self.deps.spawn(self.provision, name="reprovision")
                why = (f"audio provisioning failed: {self.prov_error}"
                       if self.prov == "failed"
                       else "audio provisioning still in progress")
                return self._refuse(source, why)
            ok, why = self.mgr.arbiter.claim_generic(self.address,
                                                     self.name)
            if not ok:
                return self._refuse(source, why, busy=True)
            self._gen += 1
            self.state = "offering"
            self.trigger = source
            if source == "AT+BCC":
                self.bcc_hold = True
            elif source == "control start":
                self.manual_hold = True
            gen = self._gen
        self.log(f"{self.tag} audio-open accepted ({source}); call slot "
                 "claimed")
        if source == "AT+BCC":
            # The OK must reach the headset BEFORE our +BCS: the offer
            # is begun by post_ack(), called after handle() sends OK.
            self._post_ack = lambda: self.deps.spawn(
                self._begin_offer, gen, name="codec-offer")
        else:
            self.deps.spawn(self._begin_offer, gen, name="codec-offer")
        return True

    def post_ack(self):
        """Run by GenericSession right after it sends OK to AT+BCC."""
        act, self._post_ack = self._post_ack, None
        if act is not None:
            act()

    # -- offer / confirm / SCO --------------------------------------------
    def _begin_offer(self, gen):
        with self.lock:
            if gen != self._gen or self.state != "offering":
                return
        self.parker.park()
        with self.lock:
            if gen != self._gen or self.state != "offering":
                return
        try:
            codec = self.sess.begin_codec_offer()
        except Exception as e:
            self.log(f"{self.tag} codec offer failed: {e!r}")
            self._fail("codec offer failed", gen)
            return
        if not self.sess.codec_negotiation:
            self.codec = codec
            self._open_sco(gen)
            return
        self._offer_timer = self.deps.timer(
            OFFER_TIMEOUT_S, lambda: self._offer_timeout(gen))

    def _offer_timeout(self, gen):
        with self.lock:
            if gen != self._gen or self.state != "offering":
                return
        self.log(f"{self.tag} headset never confirmed the codec offer "
                 f"within {OFFER_TIMEOUT_S:.0f}s")
        self.sess.codec_offered = None
        self._fail("codec offer timed out", gen)

    def _on_codec_confirmed(self):
        with self.lock:
            if self.state != "offering":
                self.log(f"{self.tag} codec confirmation while "
                         f"{self.state}; ignored")
                return False
            gen = self._gen
            self.codec = self.sess.codec_confirmed or CODEC_CVSD
            self.state = "opening"
        t, self._offer_timer = self._offer_timer, None
        if t is not None and hasattr(t, "cancel"):
            t.cancel()
        self.deps.spawn(self._open_sco, gen, name="sco-open")
        return True

    def _open_sco(self, gen):
        with self.lock:
            if gen != self._gen or self.closed:
                return
            self.state = "opening"
            codec = self.codec or CODEC_CVSD
        d = self.deps
        # Always THIS session's own device (Step B fix invariant).
        self.log(f"{self.tag} SCO open: target {self.address} "
                 f"(codec {codec}, session {self.sess.device})")
        sock, mtu, last = None, 48, None
        for attempt in range(SCO_ATTEMPTS):
            with self.lock:
                if gen != self._gen or self.closed:
                    return
            try:
                sock, mtu = d.sco_connect(self.address, codec)
                break
            except OSError as e:
                last = e
                if attempt == 0:
                    self.log(f"{self.tag} SCO connect refused ({e}); "
                             "retrying")
                d.sleep(0.25)
        if sock is None:
            reason = explain_sco_failure(self.mgr.registry, self.address,
                                         last, self.log)
            self.mgr.remember_reason(self.address, reason)
            self.app_pending = False
            self._fail("SCO connect failed", gen)
            return
        try:
            dec = d.make_decoder() if codec == CODEC_MSBC else None
            enc = d.make_encoder() if codec == CODEC_MSBC else None
        except Exception as e:
            try:
                sock.close()
            except OSError:
                pass
            self.log(f"{self.tag} codec setup failed: {e!r}")
            self._fail("codec setup failed", gen)
            return
        with self.lock:
            if gen != self._gen or self.closed or self.tunnels is None:
                try:
                    sock.close()
                except OSError:
                    pass
                return
            bridge = ScoBridge(sock, codec, dec, enc, self.tunnels.pipe,
                               self.tunnels.spk, self.log, self.tag,
                               mtu=mtu, on_closed=self._bridge_closed,
                               sleep=d.sleep)
            self.bridge = bridge
            self.state = "live"
        self.log(f"{self.tag} SCO open — {self.name} live "
                 f"({'mSBC' if codec == CODEC_MSBC else 'CVSD'}, "
                 f"trigger: {self.trigger})")
        bridge.start()
        self.mgr.arbiter.note_success(self.address)

    def _bridge_closed(self, bridge):
        with self.lock:
            if self.bridge is not bridge:
                return
            self.app_pending = False
        self._finish("SCO closed (headset or link)")

    # -- ending a call ----------------------------------------------------
    def _fail(self, why, gen):
        with self.lock:
            if gen != self._gen:
                return
        self._finish(why)

    def _finish(self, why):
        """Idempotent. Always releases the slot and restores A2DP that
        THIS device parked."""
        with self.lock:
            bridge, self.bridge = self.bridge, None
            was = self.state
            self.state = "idle"
            self.bcc_hold = self.manual_hold = False
            self.trigger = ""
            self._gen += 1
            t, self._offer_timer = self._offer_timer, None
        if t is not None and hasattr(t, "cancel"):
            t.cancel()
        if bridge is not None:
            bridge.close()
        self.sess.codec_offered = None
        self.mgr.arbiter.release_generic(self.address)
        self.parker.restore()
        if was != "idle":
            self.log(f"{self.tag} call ended ({why}); slot released")

    def stop(self, why="stop"):
        with self.lock:
            if self.state == "idle":
                return False
        self._finish(why)
        return True

    def shutdown(self):
        """Session ended (or superseded). Idempotent."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            ts, self.tunnels = self.tunnels, None
        self._finish("session ended")
        if ts is not None:
            ts.teardown()
            self.log(f"{self.tag} tunnel nodes removed")
        self.mgr.forget(self)

    # -- control verbs ------------------------------------------------------
    def control_start(self):
        with self.lock:
            if self.state == "live":
                return "ok: already live"
            if self.state != "idle":
                return "ok: already starting"
        if not self.sess.slc:
            return "err: device not connected (no completed SLC)"
        if self.request("control start"):
            return "ok: starting"
        return f"err: {self.mgr.last_reason(self.address)}"

    def control_stop(self):
        if self.stop("control stop"):
            return "ok: stopped"
        return "ok: not live"

    # -- app-attach watch (Ruling 3) ---------------------------------------
    def poll_attach(self, snap):
        """Called once per watcher poll with the pactl snapshot."""
        with self.lock:
            ts = self.tunnels
            if self.closed or self.prov != "ok" or ts is None:
                return
        attached = snap.attached(ts.mic_node, ts.spk_node)
        now = self.deps.clock()
        if attached:
            self._detach_polls = 0
            if not self.app_attached:
                self.app_attached = True
                self.app_pending = True
                self._next_attach_try = 0.0
                self.log(f"{self.tag} app attached to {self.name} "
                         "tunnel nodes")
            if (self.app_pending and self.state == "idle"
                    and self.sess.slc and now >= self._next_attach_try):
                if self.request("app attach"):
                    self.app_pending = False
                else:
                    self._next_attach_try = now + ATTACH_RETRY_S
        else:
            if self.app_attached:
                self._detach_polls += 1
                if self._detach_polls >= DETACH_POLLS:
                    self.app_attached = False
                    self.app_pending = False
                    self._detach_polls = 0
                    self.log(f"{self.tag} app detached from {self.name} "
                             "tunnel nodes")
                    if (self.state in ("live", "opening", "offering")
                            and not self.bcc_hold
                            and not self.manual_hold):
                        self.stop("app detached")

    # -- status -------------------------------------------------------------
    def status_fields(self):
        out = f"audio={self.state} audio_nodes={self.prov}"
        if self.state != "idle" and self.trigger:
            out += f" audio_trigger=\"{self.trigger}\""
        if self.parker.parked:
            out += " a2dp_parked=true"
        return out

    def detail_fields(self):
        ts = self.tunnels
        parts = [f"holds=bcc:{int(self.bcc_hold)},"
                 f"app:{int(self.app_attached)},"
                 f"manual:{int(self.manual_hold)}"]
        if ts is not None:
            parts.append(f"mic_node=\"{ts.mic_desc}\" ({ts.mic_node})")
            parts.append(f"call_node=\"{ts.spk_desc}\" ({ts.spk_node})")
        if self.prov == "failed":
            parts.append(f"audio_error=\"{_clean(self.prov_error)}\"")
        if self.codec:
            parts.append("audio_codec=" + (
                "msbc" if self.codec == CODEC_MSBC else "cvsd"))
        return " ".join(parts)


# -- pactl snapshot + the manager -----------------------------------------------

class PactlSnapshot:
    def __init__(self, pactl):
        def rows(kind):
            out = pactl("list", "short", kind)
            if out is None:
                return None
            return [ln.split("\t") for ln in out.splitlines() if ln.strip()]
        self.sources = rows("sources")
        self.sinks = rows("sinks")
        self.source_outputs = rows("source-outputs")
        self.sink_inputs = rows("sink-inputs")

    @property
    def usable(self):
        return None not in (self.sources, self.sinks, self.source_outputs,
                            self.sink_inputs)

    @staticmethod
    def _idx(rows, name):
        for c in rows:
            if len(c) > 1 and c[1] == name:
                return c[0]
        return None

    def attached(self, mic_node, spk_node):
        """True if any stream is attached to the device's mic source
        (a recording stream) or call sink (a playback stream)."""
        if not self.usable:
            return False
        si = self._idx(self.sources, mic_node)
        ki = self._idx(self.sinks, spk_node)
        if si is not None and any(len(c) > 1 and c[1] == si
                                  for c in self.source_outputs):
            return True
        if ki is not None and any(len(c) > 1 and c[1] == ki
                                  for c in self.sink_inputs):
            return True
        return False


class AudioManager:
    """Daemon-level owner: the arbiter, one GenericAudio per address,
    and the polling watcher thread."""

    def __init__(self, deps, registry):
        self.deps = deps
        self.registry = registry
        self.log = deps.log
        self.arbiter = CallArbiter(registry, deps.log)
        if registry is not None:
            registry.call_holders = self.arbiter.holders
        self.lock = threading.RLock()
        self._by_addr = {}
        self._watcher = False
        self._last_reason = {}

    # glasses side wiring (called once from main)
    def bind_glasses(self, core):
        self.arbiter.glasses_busy = lambda: core.state in (
            "starting", "live")
        core.arbiter = self.arbiter
        self.arbiter.glasses_address = lambda: getattr(
            core.session, "address", None)
        core.sco_diagnoser = lambda addr, exc: explain_sco_failure(
            self.registry, addr, exc, self.log)

    def reset_runtime(self):
        """Daemon start: reap tunnel processes and clear device dirs
        left by a previous process."""
        kill_stale_tunnels(self.deps.runtime_dir, log=self.log)
        shutil.rmtree(os.path.join(self.deps.runtime_dir, "dev"),
                      ignore_errors=True)

    def taken_node_names(self, me):
        with self.lock:
            names = set()
            for a in self._by_addr.values():
                if a is not me and a.tunnels is not None:
                    names.update(a.tunnels.node_names)
            return names

    def remember_reason(self, address, reason):
        with self.lock:
            self._last_reason[address] = reason

    def note_refusal(self, address, reason, busy=False):
        self.remember_reason(address, reason)
        if self.registry is not None:
            peers = self.arbiter.last_peers if busy else ()
            self.registry.note_audio_refusal(
                address, reason, peers=peers,
                kind="busy" if busy else "")

    def last_reason(self, address):
        with self.lock:
            return self._last_reason.get(address, "audio-open refused")

    def attach(self, sess):
        """Give a freshly built GenericSession its audio path. A prior
        incarnation for the same address is shut down first, so the new
        one's nodes/FIFOs never collide with the old one's."""
        with self.lock:
            old = self._by_addr.pop(sess.address, None)
        if old is not None:
            old.shutdown()
        ga = GenericAudio(sess, self.deps, self)
        with self.lock:
            self._by_addr[sess.address] = ga
        sess.audio = ga
        sess.audio_opener = ga.request
        sess.audio_post_ack = ga.post_ack
        sess.slc_hooks.append(ga.on_slc)
        sess.end_hooks.append(ga.shutdown)
        self.deps.spawn(ga.provision, name="provision")
        self._ensure_watcher()
        return ga

    def forget(self, ga):
        with self.lock:
            if self._by_addr.get(ga.address) is ga:
                del self._by_addr[ga.address]

    # -- app-attach watcher -------------------------------------------------
    def _ensure_watcher(self):
        with self.lock:
            if self._watcher or not self.deps.watch:
                return
            self._watcher = True
        threading.Thread(target=self._watch_loop, daemon=True,
                         name="attach-watch").start()

    def _watch_loop(self):
        while True:
            self.deps.sleep(ATTACH_POLL_S)
            try:
                self.poll_once()
            except Exception as e:          # the watcher must not die
                self.log(f"attach watcher error: {e!r}")

    def poll_once(self):
        with self.lock:
            audios = [a for a in self._by_addr.values()
                      if a.prov == "ok"]
        if not audios:
            return
        snap = PactlSnapshot(self.deps.pactl)
        if not snap.usable:
            return
        for a in audios:
            a.poll_attach(snap)
