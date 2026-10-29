"""hfp_generic — the generic HFP Audio Gateway personality (SLC layer).

Pure Python: no D-Bus, no BlueZ, no PipeWire, no root, so the whole AT
exchange is testable against a fake socket. glasses_ag.Profile builds a
GenericSession for any paired device the fingerprint rules
(hfp_registry.PERSONALITY_RULES) do not call the glasses.

This is a standard phone-like gateway written to the HFP 1.8
specification, not to any vendor: truthful feature bitmap, the standard
7-indicator CIND set, +CMER/+CHLD/+CLCC kept honest (no fake calls —
the call table is empty and stays empty), +BAC codec negotiation
(mSBC when the device offers it, CVSD floor), and the headset-initiated
vocabulary. It shares nothing with the glasses' Samsung-replica script
(AGCore/Session); the glasses personality is untouched.

STEP B BOUNDARY: SLC only. The audio path (per-device PipeWire
provisioning, SCO) is Step C. Every audio-open request — AT+BCC, or the
device's own attempt — goes through GenericSession.request_audio(),
which in this step refuses cleanly with a logged reason. Step C
replaces `audio_opener`; nothing here fakes a call to get audio.

Nothing in this module is hardware-verified.
"""

import threading

OK = "\r\nOK\r\n"
ERROR = "\r\nERROR\r\n"

CODEC_CVSD = 1
CODEC_MSBC = 2

# -- AG feature bitmap (HFP 1.8 §4.34.2, +BRSF) -----------------------------
# Only what this AG genuinely does. Bits NOT claimed: 3-way calling (0),
# EC/NR (1), voice recognition (2), in-band ring (3), voice tag (4),
# reject call (5), enhanced call control (7), extended error codes (8).
AG_ENHANCED_CALL_STATUS = 1 << 6    # +CLCC
AG_CODEC_NEGOTIATION = 1 << 9       # +BAC/+BCS/+BCC
AG_HF_INDICATORS = 1 << 10          # +BIND/+BIEV
AG_ESCO_S4 = 1 << 11                # eSCO S4 (and T2) settings
AG_BRSF = (AG_ENHANCED_CALL_STATUS | AG_CODEC_NEGOTIATION
           | AG_HF_INDICATORS | AG_ESCO_S4)

# HF feature bits we look at (HFP 1.8 §4.34.1).
HF_CODEC_NEGOTIATION = 1 << 7
HF_HF_INDICATORS = 1 << 8

# -- standard 7-indicator CIND set (order is the protocol) ------------------
CIND_NAMES = ("service", "call", "callsetup", "callheld",
              "signal", "roam", "battchg")
CIND_TEST = ('+CIND: ("service",(0,1)),("call",(0,1)),'
             '("callsetup",(0-3)),("callheld",(0-2)),("signal",(0-5)),'
             '("roam",(0,1)),("battchg",(0-5))')
# Idle gateway, truthfully: service available (the gateway link itself),
# no call, no setup, nothing held, no cellular radio (signal is the
# link's, shown full), not roaming, mains-powered-style full battery.
# If a headset ever dislikes a value, this is the one place to change.
CIND_IDLE = (1, 0, 0, 0, 5, 0, 5)

# -- HF indicators (assigned numbers, HFP 1.8 §4.36) ------------------------
HFI_SAFETY = 1          # enhanced safety (0/1)
HFI_BATTERY = 2         # battery level (0-100)
HFI_SUPPORTED = (HFI_SAFETY, HFI_BATTERY)

AUDIO_NOT_IMPLEMENTED = ("generic audio path not implemented yet "
                         "(v2 Step C); SLC only")

MAX_LINE_BUFFER = 4096


def _default_log(msg):
    print(msg, flush=True)


def _ints(text):
    """'1, 2,3' -> [1, 2, 3]; ValueError if any field is not an int."""
    return [int(x) for x in text.split(",") if x.strip() != ""]


class GenericSession:
    """One headset's SLC. Exposes .alive and .slc for the registry."""

    def __init__(self, sock, device_path, address, name="",
                 registry=None, log=_default_log, ag_codecs=None):
        self.sock = sock
        self.device = str(device_path)
        self.address = address
        self.name = name or ""
        self.registry = registry
        self.log = log
        self.ag_codecs = tuple(ag_codecs if ag_codecs is not None
                               else (CODEC_MSBC, CODEC_CVSD))
        self.lock = threading.Lock()
        self.alive = True
        self.slc = False

        # negotiated / reported state
        self.hf_features = 0
        self.hf_codecs = []             # from AT+BAC
        self.hf_indicators = []         # from AT+BIND=<list>
        self.hfi_enabled = {}           # id -> 0/1 (AG-side state)
        self.hfi_queried = False
        self.cmer_done = False
        self.indicator_reporting = False
        self.ind_enabled = [True] * len(CIND_NAMES)   # AT+BIA
        self.cind = list(CIND_IDLE)
        self.calls = []                 # always empty: no fake calls
        self.hf_battery = None
        self.hf_safety = None
        self.speaker_gain = None
        self.mic_gain = None
        self.clip = False
        self.ccwa = False
        # codec connection state
        self.codec_offered = None
        self.codec_confirmed = None
        # Step C replaces this; Step B refuses at audio-open.
        self.audio_opener = self._refuse_audio

    # -- plumbing ----------------------------------------------------------
    def send(self, text):
        shown = " / ".join(p for p in text.split("\r\n") if p)
        if shown:
            self.log(f"[{self.address}] HF >> {shown}")
        with self.lock:
            try:
                self.sock.sendall(text.encode())
            except OSError:
                pass

    def close(self):
        """Drop the RFCOMM link (used when a reconnect supersedes us)."""
        try:
            self.sock.close()
        except OSError:
            pass

    # -- feature queries ---------------------------------------------------
    @property
    def codec_negotiation(self):
        return bool(self.hf_features & HF_CODEC_NEGOTIATION
                    and AG_BRSF & AG_CODEC_NEGOTIATION)

    @property
    def hf_indicators_negotiated(self):
        return bool(self.hf_features & HF_HF_INDICATORS
                    and AG_BRSF & AG_HF_INDICATORS)

    def selected_codec(self):
        """mSBC when the headset offered it (and we can do it), else the
        CVSD floor. A headset without codec negotiation never sends
        +BAC and gets CVSD implicitly."""
        if (self.codec_negotiation and CODEC_MSBC in self.hf_codecs
                and CODEC_MSBC in self.ag_codecs):
            return CODEC_MSBC
        return CODEC_CVSD

    # -- indicators (+CIEV honors +CMER and +BIA) --------------------------
    def set_indicator(self, name, value):
        i = CIND_NAMES.index(name)
        if self.cind[i] == value:
            return
        self.cind[i] = value
        if self.indicator_reporting and self.ind_enabled[i]:
            self.send(f"\r\n+CIEV: {i + 1},{value}\r\n")

    # -- audio-open gate (Step B refuses; Step C replaces) -----------------
    def request_audio(self, source):
        return self.audio_opener(source)

    def _refuse_audio(self, source):
        self.log(f"[{self.address}] audio-open refused ({source}): "
                 f"{AUDIO_NOT_IMPLEMENTED}")
        if self.registry is not None:
            self.registry.note_audio_refusal(
                self.address, AUDIO_NOT_IMPLEMENTED)
        return False

    # -- codec connection (offer side; driven by Step C's audio open) ------
    def begin_codec_offer(self):
        """Pick the codec. A negotiating headset gets +BCS:<id> and
        must confirm with AT+BCS; a non-negotiating one is CVSD by
        definition. Returns the codec id. (Step C's audio open calls
        this; nothing in Step B does.)"""
        codec = self.selected_codec()
        self.codec_confirmed = None
        if self.codec_negotiation:
            self.codec_offered = codec
            self.log(f"[{self.address}] offering codec {codec} "
                     f"(headset offered {self.hf_codecs})")
            self.send(f"\r\n+BCS: {codec}\r\n")
        else:
            self.codec_offered = None
            self.codec_confirmed = CODEC_CVSD
            self.log(f"[{self.address}] no codec negotiation; CVSD")
        return codec

    # -- status ------------------------------------------------------------
    def status_fields(self):
        codec = {CODEC_MSBC: "msbc", CODEC_CVSD: "cvsd"}[
            self.selected_codec()]
        out = f"codec={codec}"
        if self.hf_battery is not None:
            out += f" hf_battery={self.hf_battery}"
        return out

    # -- SLC steps ---------------------------------------------------------
    def _maybe_slc(self, force=False):
        """SLC is up once the mandatory steps for what BOTH sides
        support are done: +CMER, and the HF-indicator exchange (ending
        at AT+BIND?) when negotiated. `force`: the headset is already
        issuing post-SLC commands, so it considers the link up."""
        if self.slc or not self.cmer_done:
            return
        if (self.hf_indicators_negotiated and not self.hfi_queried
                and not force):
            return
        self.slc = True
        self.log(f"[{self.address}] SLC established "
                 f"({self.name or 'unnamed'}; hf_features={self.hf_features}"
                 f", codecs={self.hf_codecs or 'none (CVSD)'}, "
                 f"pick={self.selected_codec()})")

    def _on_brsf(self, arg):
        try:
            self.hf_features = int(arg)
        except ValueError:
            self.send(ERROR)
            return
        self.send(f"\r\n+BRSF: {AG_BRSF}\r\n" + OK)

    def _on_bac(self, arg):
        try:
            codecs = _ints(arg)
        except ValueError:
            self.send(ERROR)
            return
        if not codecs or any(c < 1 or c > 255 for c in codecs):
            self.send(ERROR)
            return
        self.hf_codecs = codecs
        self.log(f"[{self.address}] headset codecs {codecs} -> "
                 f"pick {self.selected_codec()}")
        self.send(OK)

    def _on_cmer(self, arg):
        try:
            fields = _ints(arg)
        except ValueError:
            self.send(ERROR)
            return
        # HFP 1.8 §4.34.2: only mode 3, keyp 0, disp 0, ind 0|1.
        if len(fields) != 4 or fields[:3] != [3, 0, 0] \
                or fields[3] not in (0, 1):
            self.send(ERROR)
            return
        ind = fields[3]
        self.indicator_reporting = bool(ind)
        self.cmer_done = True
        self.send(OK)
        self._maybe_slc()

    def _on_bia(self, arg):
        # '1,0,,1': per-CIND-index activation; empty field = unchanged.
        parts = arg.split(",")
        if len(parts) > len(CIND_NAMES):
            self.send(ERROR)
            return
        new = list(self.ind_enabled)
        for i, p in enumerate(parts):
            p = p.strip()
            if p == "":
                continue
            if p not in ("0", "1"):
                self.send(ERROR)
                return
            new[i] = p == "1"
        self.ind_enabled = new
        self.send(OK)

    def _on_bind(self, u):
        if u == "AT+BIND=?":
            self.send("\r\n+BIND: (" + ",".join(
                str(i) for i in HFI_SUPPORTED) + ")\r\n" + OK)
        elif u == "AT+BIND?":
            lines = "".join(f"\r\n+BIND: {i},{int(on)}"
                            for i, on in sorted(self.hfi_enabled.items()))
            self.send(lines + "\r\n" + OK)
            self.hfi_queried = True
            self._maybe_slc()
        else:
            try:
                ids = _ints(u.split("=", 1)[1])
            except ValueError:
                self.send(ERROR)
                return
            if not ids:
                self.send(ERROR)
                return
            self.hf_indicators = ids
            # Active = what both sides support; on by default.
            self.hfi_enabled = {i: True for i in ids
                                if i in HFI_SUPPORTED}
            self.send(OK)

    def _on_biev(self, arg):
        try:
            ind, val = _ints(arg)
        except ValueError:
            self.send(ERROR)
            return
        if not self.hfi_enabled.get(ind):
            self.log(f"[{self.address}] BIEV for inactive/unknown "
                     f"indicator {ind}; ignored")
            self.send(ERROR)
            return
        if ind == HFI_BATTERY:
            if not 0 <= val <= 100:
                self.send(ERROR)
                return
            if self.hf_battery != val:
                self.log(f"[{self.address}] headset battery: {val}%")
            self.hf_battery = val
        elif ind == HFI_SAFETY:
            if val not in (0, 1):
                self.send(ERROR)
                return
            self.hf_safety = val
        self.send(OK)

    def _on_gain(self, kind, arg):
        try:
            val = int(arg)
        except ValueError:
            self.send(ERROR)
            return
        if not 0 <= val <= 15:
            self.send(ERROR)
            return
        if kind == "VGS":
            self.speaker_gain = val
        else:
            self.mic_gain = val
        self.log(f"[{self.address}] headset {kind} = {val}")
        self.send(OK)

    def _clcc(self):
        # Truthful: only calls that really exist. None, so just OK.
        out = ""
        for c in self.calls:
            out += (f"\r\n+CLCC: {c['idx']},{c['dir']},{c['stat']},"
                    f"{c['mode']},{c['mpty']}")
            if c.get("number"):
                out += f",\"{c['number']}\",{c.get('type', 129)}"
        self.send(out + OK if out else OK)

    # -- AT dispatch -------------------------------------------------------
    def handle(self, cmd):
        u = cmd.strip().upper()
        if not u:
            return
        # Lines that are not commands (stray result codes) are ignored.
        if u.startswith("+"):
            self.log(f"[{self.address}] ignoring unsolicited {cmd!r}")
            return

        if u.startswith("AT+BRSF="):
            self._on_brsf(u[8:])
        elif u.startswith("AT+BAC="):
            self._on_bac(u[7:])
        elif u == "AT+CIND=?":
            self.send("\r\n" + CIND_TEST + "\r\n" + OK)
        elif u == "AT+CIND?":
            self.send("\r\n+CIND: " + ",".join(
                str(v) for v in self.cind) + "\r\n" + OK)
        elif u.startswith("AT+CMER="):
            self._on_cmer(u[8:])
        elif u == "AT+CHLD=?":
            # No 3-way calling claimed; only "release held" exists, and
            # there is never anything held.
            self.send("\r\n+CHLD: (0)\r\n" + OK)
        elif u == "AT+CHLD=0":
            self.send(OK)
        elif u.startswith("AT+CHLD"):
            self.send(ERROR)
        elif u == "AT+CLCC":
            self._clcc()
        elif u.startswith("AT+BIA="):
            self._on_bia(u[7:])
        elif u in ("AT+BIND=?", "AT+BIND?") or u.startswith("AT+BIND="):
            self._on_bind(u)
        elif u.startswith("AT+BIEV="):
            self._on_biev(u[8:])
        elif u.startswith("AT+VGS="):
            self._on_gain("VGS", u[7:])
        elif u.startswith("AT+VGM="):
            self._on_gain("VGM", u[7:])
        elif u == "AT+BCC":
            self._force_slc_if_ready()
            # Refuse with ERROR rather than OK-then-silence: an OK
            # would leave the headset waiting for a +BCS that never
            # comes.
            self.log(f"[{self.address}] headset requested a codec "
                     "connection (AT+BCC)")
            if self.request_audio("AT+BCC"):
                self.send(OK)
            else:
                self.send(ERROR)
        elif u.startswith("AT+BCS="):
            self._on_bcs(u[7:])
        elif u == "AT+BLDN":
            # Last-number redial: this gateway has dialed nothing.
            self._force_slc_if_ready()
            self.log(f"[{self.address}] AT+BLDN: no last dialed number")
            self.send(ERROR)
        elif u.startswith("AT+BVRA="):
            # No voice-recognition feature is advertised (BRSF bit 2).
            self._force_slc_if_ready()
            self.log(f"[{self.address}] AT+BVRA from headset "
                     "(voice recognition not offered)")
            self.send(ERROR)
        elif u.startswith("AT+CKPD="):
            self._force_slc_if_ready()
            if u == "AT+CKPD=200":
                # Headset button press. With no call to act on and the
                # audio path deferred to Step C, acknowledge and note.
                self.log(f"[{self.address}] headset button (CKPD=200): "
                         "no call to act on")
                self.send(OK)
            else:
                self.send(ERROR)
        elif u.startswith(("AT+CLIP=", "AT+CCWA=")):
            flag = u.endswith("=1")
            if not (u.endswith("=0") or flag):
                self.send(ERROR)
                return
            if u.startswith("AT+CLIP"):
                self.clip = flag
            else:
                self.ccwa = flag
            self.send(OK)
        elif u == "AT+COPS=3,0":
            self.send(OK)
        elif u == "AT+COPS?":
            # Not registered to any operator (mode only, no name).
            self.send("\r\n+COPS: 0\r\n" + OK)
        elif u == "AT+CNUM":
            self.send(OK)           # no subscriber numbers
        elif u == "AT":
            self.send(OK)
        else:
            # Everything else — ATA/ATD/AT+CHUP with no call, NREC,
            # CMEE, BTRH, BSIR, vendor extensions (XAPL, IPHONEACCEV,
            # …) — is unsupported here: clean ERROR, link stays up.
            self.log(f"[{self.address}] unsupported command {cmd!r}")
            self.send(ERROR)

    def _force_slc_if_ready(self):
        self._maybe_slc(force=True)

    def _on_bcs(self, arg):
        try:
            codec = int(arg)
        except ValueError:
            self.send(ERROR)
            return
        if self.codec_offered is None:
            self.log(f"[{self.address}] unsolicited AT+BCS={codec}")
            self.send(ERROR)
        elif codec != self.codec_offered:
            self.log(f"[{self.address}] headset answered codec {codec} "
                     f"to offer {self.codec_offered}")
            self.codec_offered = None
            self.send(ERROR)
        else:
            self.codec_confirmed = codec
            self.codec_offered = None
            self.log(f"[{self.address}] codec {codec} confirmed")
            self.send(OK)
            # Step C: the SCO open follows confirmation.
            self.request_audio("codec confirmed")

    # -- main loop ---------------------------------------------------------
    def run(self):
        self.log(f"[{self.address}] generic session start: {self.device}"
                 f" ({self.name or 'unnamed'})")
        buf = b""
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    cmd = line.decode(errors="replace").strip("\n").strip()
                    if not cmd:
                        continue
                    self.log(f"[{self.address}] HF << {cmd}")
                    try:
                        self.handle(cmd)
                    except Exception as e:      # never kill the SLC
                        self.log(f"[{self.address}] handler error on "
                                 f"{cmd!r}: {e!r}")
                        self.send(ERROR)
                if len(buf) > MAX_LINE_BUFFER:
                    self.log(f"[{self.address}] discarding oversized "
                             "unterminated input")
                    buf = b""
        except OSError:
            pass
        finally:
            self.alive = False
            self.slc = False
            try:
                self.sock.close()
            except OSError:
                pass
            if self.registry is not None and self.address:
                self.registry.detach(self.address, self)
            self.log(f"[{self.address}] generic session end")
