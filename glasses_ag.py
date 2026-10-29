#!/usr/bin/env python3
"""
glasses_ag — Meta glasses microphone daemon for Linux.

Owns the HFP (Handsfree) profile for Meta smart glasses and
impersonates a Samsung phone's audio gateway — the only kind of gateway
the glasses will keep a service-level connection with (proven
2026-10-26: PipeWire's native gateway answers BRSF 3584 with all-zero
CIND values and the glasses release the connection ~20 ms after SLC;
the Samsung replica — BRSF 4079, live CIND values, deferred codec
negotiation — holds indefinitely).

What it does:
  * Registers org.bluez.Profile1 endpoints for HFP (111e out, 111f in)
    and holds the service-level connection with the glasses. Any other
    device's HFP attempt is refused, so other headsets can fall back
    to HSP via WirePlumber (see 51-glasses-ag.conf).
  * Dictation (via the `glasses-mic` control command): runs a silent,
    gateway-declared outgoing call (dialing -> alerting -> connected,
    no ring, no gesture), negotiates mSBC, opens SCO, plays a two-blip
    cue into the glasses, and streams the microphone — decoded from
    mSBC by libsbc — into a FIFO that PipeWire exposes as the
    "glasses-mic" source (see 52-glasses-mic.conf), usable by any app
    (dictation tools, voice assistants, recorders).
  * CVSD is never used: the glasses refuse legacy synchronous-link
    proposals from an initiator (HCI status 0x1f); the parameterized
    eSCO/mSBC setup is accepted instantly.
  * Discord mode (`glasses-mic discord`): the power-saving mode.
    The glasses idle in A2DP media mode while the daemon advertises
    a Playing MPRIS player, so the temple tap's play/pause event is
    delivered to the daemon instead of starting a paused music app.
    A tap opens the mic as above; the mic closes itself after ~4 s
    of silence (RMS meter on the decoded stream) or at a 90 s cap,
    whichever comes first, with a descending chirp before the close.
    If the cap arrives mid-speech it defers instead: a warning tone,
    then the close lands at the next natural pause — never past a
    120 s absolute ceiling. Taps during a call never reach the
    computer (AVRCP rides the A2DP link, which is parked for the
    call's duration), so no tap can extend a live session —
    reopening after a close is one tap.
    A held HFP call is what drains the glasses, so calls now exist
    only while the user is actually talking.

Audio path details (all proven on the real hardware, 2026-10-26):
  * SCO packets are 60 bytes: [2-byte header][57-byte mSBC frame]
    [1 trailer byte]. The decoder sync-scans for the frame's
    AD 00 00 header inside each packet, so dropped packets cost one
    7.5 ms frame, not the stream.
  * The connect cue is sent in the glasses' own packet framing — the
    only downlink construction their decoder accepted.
"""

import base64
import ctypes
import math
import os
import re
import socket
import subprocess
import struct
import sys
import threading
import time
from array import array
from collections import deque
from datetime import datetime

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    import dbus.exceptions
    from gi.repository import GLib
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\n"
             "On Arch/Omarchy:  sudo pacman -S python-dbus python-gobject")

# --------------------------------------------------------------------------
# Constants — every value below was verified against the real glasses.

# Optional safety pin. By default the daemon identifies the glasses by
# their advertised name ("Meta Glasses ...") and remembers the address
# of the accepted HFP connection. Set GLASSES_AG_ADDRESS to a Bluetooth
# address if you want it to serve one specific pair only. Set
# GLASSES_AG_NAME_PREFIX if a regional/model variant uses another name.
GLASSES_MAC = os.environ.get("GLASSES_AG_ADDRESS", "").strip().upper()
GLASSES_NAME_PREFIX = os.environ.get(
    "GLASSES_AG_NAME_PREFIX", "Meta Glasses").strip()

# Discord mode (tap-to-talk with power saving; design settled and
# hardware-verified in pieces 2026-10-28): while armed, the daemon
# advertises itself as a *Playing* MPRIS player, which makes the
# desktop's AVRCP bridge route the glasses' temple-tap play/pause
# events to the daemon instead of to a paused music app. A tap opens
# the mic; the mic closes itself after DISCORD_HANGOVER seconds of
# silence, or at the DISCORD_CAP hard limit. (Taps DURING a call
# never reach the PC — AVRCP rides the parked A2DP link and the
# taps surface on no other channel; verified 2026-10-29. A closed
# session is reopened with one tap.) Thresholds are env-tunable.
DISCORD_CAP = float(os.environ.get("GLASSES_AG_DISCORD_CAP", "90"))
DISCORD_HANGOVER = float(os.environ.get(
    "GLASSES_AG_DISCORD_HANGOVER", "4"))
DISCORD_VAD_RMS = float(os.environ.get(
    "GLASSES_AG_DISCORD_VAD_RMS", "300"))
# When the cap arrives mid-speech it defers — warn once, close at
# the next pause — but never past this absolute session ceiling.
DISCORD_CAP_MAX = float(os.environ.get(
    "GLASSES_AG_DISCORD_CAP_MAX", "120"))
MPRIS_NAME = "org.mpris.MediaPlayer2.glassesag"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"
AG_UUID = "0000111f-0000-1000-8000-00805f9b34fb"
BLUEZ = "org.bluez"
PROFILE_IFACE = "org.bluez.Profile1"
MANAGER_PATH = "/org/bluez"
AG_PATH_HF = "/org/glassesag/hf"
AG_PATH_AG = "/org/glassesag/ag"

AG_BRSF = 4079                      # Samsung's answer (every AG feature
                                    # except voice-tag)
CIND_TEST = ('+CIND: ("service",(0-1)),("call",(0-1)),("callsetup",(0-3)),'
             '("callheld",(0-2)),("signal",(0-5)),("roam",(0-1)),'
             '("battchg",(0-5))')
CIND_PHONE = "1,0,0,0,4,0,5"        # service, signal 4, battery 5
OK = "\r\nOK\r\n"
ERROR = "\r\nERROR\r\n"

CODEC_MSBC = 2
BT_VOICE_MSBC = 0x0063
SOL_BLUETOOTH = 274
BT_VOICE = 11

RUNTIME_DIR = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "glasses-ag")
CTL_PATH = os.path.join(RUNTIME_DIR, "ctl")
FIFO_PATH = os.path.join(RUNTIME_DIR, "mic.fifo")
SPK_FIFO_PATH = os.path.join(RUNTIME_DIR, "spk.fifo")

# Two-blip connect cue (740 Hz, amp ~20000, 0.375 s), mSBC-encoded and
# pre-wrapped in the glasses' packet framing: [01 C8][frame][00] x50.
CUE_B64 = "AcitAADaySEAAH/v7t/7+7gBAO4AQBuBzuugnGaWqrryil47bQcw2WVTPH0m7MiSO5lfTS9JbDAu0wwAAcitAABa60ISAZwoO17FLtZWa7axiO1dFDpf648aeXu1GW7tpm68YgOOlJaDtxSa8VhMSVsdVww9U9QAAcitAAAr6xEQADaMeCgWcZnOXQTfT8XFtKaaIWUFo5anK7UmPuoUI3apmtxzFMzExLs25p7pRQSpfNQAAcitAAAr6xEQAM1QBZLZ/QenrGbvpCUzTnArPjGo8FME+VbFyrKmlsmVBnekpxitJjhL1Cb2gaoWYQQAAcitAAAr6xEQADYzMTDdUabUVUFX5DXMDemQal0Ikaxqxp41MrCRLf3ZqAhRRRzdNc0QqZPB3QdbrKQAAcitAAAr6xEQAL7JjU0DdAqNkmpPFcU79TFyVNll+GFBi+gpyAvpTorNCXWkapaZwTTykTA9cacgU0QAAcitAAAr6xEQAFIYqXMrqmRKj0IQ6xq1SA1Mo/VLR35qFZRRQ/bRcyRapS5xQcNqqcGKKU1LTQpLmGgAAcitAAAr6xEQAJhlkE5tJEx5QGmQF1BeecpyaypjxqlCSmoaqebNTPR8S+NmadsUUE44TXNL0mR+h0AAAcitAAAr6xEQAH9axm4qI1Ms8xLDYZqKJRBQRZNcvSCpXBrQbBppcPqqU2jHUoFnGpzl0E30/FxbSmgAAcitAAAr6xEQAGiFlBaOWpyu1Jj7qFCN2qZrccxTMxMS7Nuae6UUEqXzXNUAWS2f0Hp6xm76QlM05wAAAcitAAAr6xEQAKz4xqPBTBPlWxcqyppbJlQZ3pKcYrSY4S9Qm9oGqFmEE2MzEw3VGm1FVBV+Q1zA3pgAAcitAAAr6xEQAEGpdCJGsasaeNTKwkS392agIUUUc3TXNEKmTwd0HW6ym+yY1NA3QKjZJqTxXFO/UxQAAcitAAAr6xEQAMlTZZfhhQYvoKcgL6U6KzQl1pGqWmcE08pEwPXGnIFNFSGKlzK6pkSo9CEOsatUgNQAAcitAAAr6xEQADKP1S0d+ahWUUUP20XMkWqUucUHDaqnBiilNS00KS5hqYZZBObSRMeUBpkBdQXnnKQAAcitAAAr6xEQAMmsqY8apQkpqGqnmzUz0fEvjZmnbFFBOOE1zS9JkfodB/WsZuKiNTLPMSw2GaiiUQQAAcitAAAr6xEQAEEWTXL0gqVwa0GwaaXD6qlNox1KBZxqc5dBN9PxcW0ppohZQWjlqcrtSY+6hQjdqmQAAcitAAAr6xEQAK3HMUzMTEuzbmnulFBKl81zVAFktn9B6esZu+kJTNOcCs+MajwUwT5VsXKsqaWyZUAAAcitAAAr6xEQAGd6SnGK0mOEvUJvaBqhZhBNjMxMN1RptRVQVfkNcwN6ZBqXQiRrGrGnjUysJEt/dmgAAcitAACT60IRAIBJulHLrtzOE8k8Owh1tTxvtM7TQbuio8jSk4q6TwqPDIpZmYQ0pWTzs2/VbORZq6AAAcitAAB12gEAAKS/2u5YAVFMtZtcKtpu4XlSY4WNQFxlEZZWg3z6K1TmdFKXmozmuIOkodhGu2oKbOAAAcitAACZqCEAAAdVoJdep730ObEm1UOHEVefuS0YEe4N/n97f9/e3/f3t/397f9/e3/f3t/397f9/ewAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADFAAAAAHdttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd2223dttt3bbbd222wAAcitAADaySEAAH/v7t/7+7gBAO4AQBuBzuugnGaWqrryil47bQcw2WVTPH0m7MiSO5lfTS9JbDAu0wwAAcitAABa60ISAZwoO17FLtZWa7axiO1dFDpf648aeXu1GW7tpm68YgOOlJaDtxSa8VhMSVsdVww9U9QAAcitAAAr6xEQADaMeCgWcZnOXQTfT8XFtKaaIWUFo5anK7UmPuoUI3apmtxzFMzExLs25p7pRQSpfNQAAcitAAAr6xEQAM1QBZLZ/QenrGbvpCUzTnArPjGo8FME+VbFyrKmlsmVBnekpxitJjhL1Cb2gaoWYQQAAcitAAAr6xEQADYzMTDdUabUVUFX5DXMDemQal0Ikaxqxp41MrCRLf3ZqAhRRRzdNc0QqZPB3QdbrKQAAcitAAAr6xEQAL7JjU0DdAqNkmpPFcU79TFyVNll+GFBi+gpyAvpTorNCXWkapaZwTTykTA9cacgU0QAAcitAAAr6xEQAFIYqXMrqmRKj0IQ6xq1SA1Mo/VLR35qFZRRQ/bRcyRapS5xQcNqqcGKKU1LTQpLmGgAAcitAAAr6xEQAJhlkE5tJEx5QGmQF1BeecpyaypjxqlCSmoaqebNTPR8S+NmadsUUE44TXNL0mR+h0AAAcitAAAr6xEQAH9axm4qI1Ms8xLDYZqKJRBQRZNcvSCpXBrQbBppcPqqU2jHUoFnGpzl0E30/FxbSmgAAcitAAAr6xEQAGiFlBaOWpyu1Jj7qFCN2qZrccxTMxMS7Nuae6UUEqXzXNUAWS2f0Hp6xm76QlM05wAAAcitAAAr6xEQAKz4xqPBTBPlWxcqyppbJlQZ3pKcYrSY4S9Qm9oGqFmEE2MzEw3VGm1FVBV+Q1zA3pgAAcitAAAr6xEQAEGpdCJGsasaeNTKwkS392agIUUUc3TXNEKmTwd0HW6ym+yY1NA3QKjZJqTxXFO/UxQAAcitAAAr6xEQAMlTZZfhhQYvoKcgL6U6KzQl1pGqWmcE08pEwPXGnIFNFSGKlzK6pkSo9CEOsatUgNQAAcitAAAr6xEQADKP1S0d+ahWUUUP20XMkWqUucUHDaqnBiilNS00KS5hqYZZBObSRMeUBpkBdQXnnKQAAcitAAAr6xEQAMmsqY8apQkpqGqnmzUz0fEvjZmnbFFBOOE1zS9JkfodB/WsZuKiNTLPMSw2GaiiUQQAAcitAAAr6xEQAEEWTXL0gqVwa0GwaaXD6qlNox1KBZxqc5dBN9PxcW0ppohZQWjlqcrtSY+6hQjdqmQAAcitAAAr6xEQAK3HMUzMTEuzbmnulFBKl81zVAFktn9B6esZu+kJTNOcCs+MajwUwT5VsXKsqaWyZUAAAcitAAAr6xEQAGd6SnGK0mOEvUJvaBqhZhBNjMxMN1RptRVQVfkNcwN6ZBqXQiRrGrGnjUysJEt/dmgAAcitAACT60IRAIBJulHLrtzOE8k8Owh1tTxvtM7TQbuio8jSk4q6TwqPDIpZmYQ0pWTzs2/VbORZq6AAAcitAAB12gEAAKS/2u5YAVFMtZtcKtpu4XlSY4WNQFxlEZZWg3z6K1TmdFKXmozmuIOkodhGu2oKbOAA"
CUE_UNITS = [base64.b64decode(CUE_B64)[i:i + 60]
             for i in range(0, len(base64.b64decode(CUE_B64)), 60)]


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    print(line, flush=True)


# --------------------------------------------------------------------------
# mSBC decoding via libsbc (Arch: `sbc` package -> libsbc.so.1)

class MSBCDecoder:
    """One persistent libsbc mSBC decoder context (ctypes)."""

    def __init__(self):
        self.lib = None
        for name in ("libsbc.so.1", "libsbc.so"):
            try:
                self.lib = ctypes.CDLL(name)
                break
            except OSError:
                continue
        if self.lib is None:
            raise RuntimeError(
                "libsbc not found — install it:  sudo pacman -S sbc")
        self.lib.sbc_init_msbc.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.sbc_init_msbc.restype = ctypes.c_int
        self.lib.sbc_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)]
        self.lib.sbc_decode.restype = ctypes.c_ssize_t
        self.lib.sbc_finish.argtypes = [ctypes.c_void_p]
        self.ctx = ctypes.create_string_buffer(4096)
        if self.lib.sbc_init_msbc(self.ctx, 0) != 0:
            raise RuntimeError("sbc_init_msbc failed")
        self.out = ctypes.create_string_buffer(1024)
        self.lock = threading.Lock()

    def decode_frame(self, frame57):
        """57-byte mSBC frame -> 240 bytes of s16le PCM @ 16 kHz."""
        with self.lock:
            written = ctypes.c_size_t(0)
            used = self.lib.sbc_decode(self.ctx, frame57, len(frame57),
                                        self.out, 1024,
                                        ctypes.byref(written))
            if used <= 0 or written.value == 0:
                return b""
            return self.out.raw[:written.value]


class MSBCEncoder:
    """One persistent libsbc mSBC encoder context (ctypes) — the
    downlink twin of MSBCDecoder, for sending call audio TO the
    glasses (Discord output) over the same SCO link as the mic."""

    def __init__(self):
        self.lib = None
        for name in ("libsbc.so.1", "libsbc.so"):
            try:
                self.lib = ctypes.CDLL(name)
                break
            except OSError:
                continue
        if self.lib is None:
            raise RuntimeError(
                "libsbc not found — install it:  sudo pacman -S sbc")
        self.lib.sbc_init_msbc.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.sbc_init_msbc.restype = ctypes.c_int
        self.lib.sbc_encode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_ssize_t)]
        self.lib.sbc_encode.restype = ctypes.c_int
        self.lib.sbc_finish.argtypes = [ctypes.c_void_p]
        self.ctx = ctypes.create_string_buffer(4096)
        if self.lib.sbc_init_msbc(self.ctx, 0) != 0:
            raise RuntimeError("sbc_init_msbc (encoder) failed")
        self.out = ctypes.create_string_buffer(1024)
        self.lock = threading.Lock()

    def encode_frame(self, pcm240):
        """240 bytes of s16le PCM @ 16 kHz -> one 57-byte mSBC frame."""
        with self.lock:
            written = ctypes.c_ssize_t(0)
            rc = self.lib.sbc_encode(self.ctx, pcm240, len(pcm240),
                                     self.out, 1024,
                                     ctypes.byref(written))
            if rc < 0 or written.value <= 0:
                return b""
            return self.out.raw[:written.value]


class SCOParser:
    """Turns the SCO byte stream into PCM. Sync-scans each 60-byte
    packet for the frame header (AD 00 00 at offset 2); on a slip it
    resyncs byte-by-byte, so a dropped packet costs ~one frame."""

    def __init__(self, decoder):
        self.decoder = decoder
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        pcm = bytearray()
        while len(self.buf) >= 60:
            if (self.buf[2] == 0xAD and self.buf[3] == 0
                    and self.buf[4] == 0):
                pcm += self.decoder.decode_frame(bytes(self.buf[2:59]))
                del self.buf[:60]
            else:
                del self.buf[0]
        if len(self.buf) > 8192:      # should never happen; stay safe
            del self.buf[:4096]
        return bytes(pcm)


# --------------------------------------------------------------------------
# PipeWire source feed: continuous FIFO writer (PCM when live, silence
# otherwise, so the pipe-source node always has a clock).

class PipeSource:
    CHUNK = 240                     # 120 samples = 7.5 ms @ 16 kHz s16

    def __init__(self, fifo_path):
        self.fifo_path = fifo_path
        self.queue = deque()
        self.lock = threading.Lock()
        self.queued_bytes = 0
        self.running = True

    def push(self, pcm):
        with self.lock:
            self.queue.append(pcm)
            self.queued_bytes += len(pcm)
            while self.queued_bytes > 32000 and self.queue:
                self.queued_bytes -= len(self.queue.popleft())

    def _take(self):
        with self.lock:
            if not self.queue:
                return b"\x00" * self.CHUNK
            data = self.queue[0]
            if len(data) <= self.CHUNK:
                self.queue.popleft()
                self.queued_bytes -= len(data)
                return data + b"\x00" * (self.CHUNK - len(data))
            self.queue[0] = data[self.CHUNK:]
            self.queued_bytes -= self.CHUNK
            return data[:self.CHUNK]

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="pipe-source").start()

    def _run(self):
        os.makedirs(os.path.dirname(self.fifo_path), exist_ok=True)
        try:
            os.mkfifo(self.fifo_path)
        except FileExistsError:
            pass
        while self.running:
            fd = None
            try:
                fd = os.open(self.fifo_path,
                             os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(1.0)     # PipeWire reader not there yet
                continue
            os.set_blocking(fd, True)
            log("pipe-source: PipeWire reader connected")
            next_t = time.monotonic()
            try:
                while self.running:
                    os.write(fd, self._take())
                    next_t += 0.0075
                    delay = next_t - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    elif delay < -0.1:
                        next_t = time.monotonic()
            except OSError:
                log("pipe-source: reader went away; will reopen")
            finally:
                if fd is not None:
                    os.close(fd)
            time.sleep(0.5)


class SpkSink:
    """The downlink's PCM reservoir. Apps play into the 'glasses-spk'
    PipeWire sink; the pipe-tunnel module writes that PCM into this
    FIFO. The session's downlink thread drains it 240 bytes at a time
    (zero-padding when empty), so the SCO downlink is a continuous
    stream of real frames — silence included — never a void."""

    CHUNK = 240                     # 120 samples = 7.5 ms @ 16 kHz s16
    EDGE = 1920                     # live-edge window: 60 ms of audio

    def __init__(self, fifo_path):
        self.fifo_path = fifo_path
        self.buf = bytearray()
        self.lock = threading.Lock()

    def take(self, n=CHUNK):
        with self.lock:
            # Ride the live edge. The FIFO alone can hold ~2 s of
            # audio, and a startup burp (the app pumping while the
            # link was still opening) freezes that backlog in place:
            # every stage drains at exactly 1.0x, so it would NEVER
            # catch up — observed as "full seconds of delay"
            # (2026-10-26 22:56). Anything older than the edge is
            # latency wearing audio's clothing; drop it.
            excess = len(self.buf) - self.EDGE
            if excess > 0:
                del self.buf[:excess]
            data = bytes(self.buf[:n])
            del self.buf[:n]
        return data + b"\x00" * (n - len(data))

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="spk-sink").start()

    def _run(self):
        os.makedirs(os.path.dirname(self.fifo_path), exist_ok=True)
        try:
            os.mkfifo(self.fifo_path)
        except FileExistsError:
            pass
        while True:
            try:
                fd = os.open(self.fifo_path, os.O_RDONLY)
            except OSError:
                time.sleep(1.0)     # tunnel writer not there yet
                continue
            log("spk-sink: PipeWire writer connected")
            try:
                while True:
                    data = os.read(fd, 4096)
                    if not data:
                        break       # writer went away; reopen
                    with self.lock:
                        self.buf += data
                        if len(self.buf) > 32000:
                            del self.buf[:len(self.buf) - 32000]
            except OSError:
                pass
            finally:
                os.close(fd)
            log("spk-sink: writer went away; will reopen")
            time.sleep(0.5)


# --------------------------------------------------------------------------
# AG core: shared state the control socket drives.

def _pcm_rms(pcm):
    """RMS amplitude of s16le PCM — the Discord-mode voice meter."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    a = array("h")
    a.frombytes(pcm[:n * 2])
    return math.sqrt(sum(s * s for s in a) / n)


class AGCore:
    def __init__(self, pipe, decoder, encoder=None, spk=None):
        self.pipe = pipe
        self.decoder = decoder
        self.encoder = encoder
        self.spk = spk
        self.session = None
        self.device_address = GLASSES_MAC or None
        self.state = "down"         # down | ready | starting | live
        self.lock = threading.Lock()
        self.battery = None         # last +BIEV battery report (0-100)
        self.a2dp_parked = False    # we parked the glasses' A2DP card
        # Discord mode (tap-to-talk): armed flag, the MPRIS player
        # object (attached by main()), and the live-session clocks
        # the auto-close monitor watches.
        self.discord = False
        self.mpris = None
        self.live_since = None
        self.cap_deadline = None
        self.cap_ceiling = None
        self.cap_warned = False
        self.last_voice = 0.0
        self._monitor_started = False

    def status(self):
        sess = self.session
        connected = bool(sess and sess.alive and sess.slc)
        s = (f"state={self.state} glasses_connected={connected} "
             f"call={sess.call_state if sess else 'none'}")
        if self.battery is not None:
            s += f" battery={self.battery}"
        s += f" discord={'on' if self.discord else 'off'}"
        if self.discord and self.state == "live":
            if self.cap_warned:
                s += " cap=deferred"
            elif self.cap_deadline is not None:
                remain = max(0, int(self.cap_deadline
                                    - time.monotonic()))
                s += f" cap_in={remain}s"
        return s

    def cmd_start(self):
        with self.lock:
            if self.state == "live":
                return "ok: already live"
            sess = self.session
            if not sess or not sess.alive or not sess.slc:
                return "err: glasses not connected (no HFP session)"
            if self.state == "starting":
                return "ok: already starting"
            self.state = "starting"
            zombie = sess.call_state != "idle"
        if zombie:
            # A failed attempt left its silent call standing with no
            # audio link; start_dictation() no-ops against a non-idle
            # call, which would wedge the state at "starting" forever
            # (observed 2026-10-26 22:18). Tear the leftover down and
            # dial a fresh one.
            log("clearing leftover call from a failed start")
            sess.stop_dictation()
            time.sleep(0.3)
        self.park_a2dp()
        sess.start_dictation()
        return "ok: starting"

    def cmd_stop(self):
        with self.lock:
            sess = self.session
            was = self.state
            if self.state in ("live", "starting"):
                self.state = "ready"
                self.live_since = None
                self.cap_deadline = None
                self.cap_ceiling = None
                self.cap_warned = False
        if sess and was in ("live", "starting"):
            sess.stop_dictation()
            # Covers a stop issued mid-start (A2DP still parked, the
            # SCO worker gone): hand the music path back. In the
            # normal flow this is a no-op — a successful start already
            # restored A2DP the moment the voice link came up.
            self.restore_a2dp()
            return "ok: stopped"
        return "ok: not live"

    def cmd_toggle(self):
        if self.state == "live":
            return self.cmd_stop()
        return self.cmd_start()

    # -- Discord mode (tap-to-talk, power-saving) --------------------------
    # The glasses idle in A2DP media mode (cheap). While armed, the
    # daemon's MPRIS player advertises "Playing", so the temple tap's
    # AVRCP play/pause lands HERE (proven with a stub player
    # 2026-10-28) instead of starting a paused music app. Tap opens
    # the mic; the monitor below closes it on silence or at the cap.

    def set_discord(self, on):
        with self.lock:
            was = self.discord
            self.discord = on
        if on and not was:
            if not self._monitor_started:
                self._monitor_started = True
                threading.Thread(target=self._discord_monitor,
                                 daemon=True,
                                 name="discord-monitor").start()
            if self.mpris is not None:
                self.mpris.arm()
            log("discord mode ARMED — tap opens the mic")
        elif was and not on:
            if self.mpris is not None:
                self.mpris.disarm()
            if self.state in ("live", "starting"):
                self.cmd_stop()
            log("discord mode disarmed")
        return "ok: discord " + ("on" if on else "off")

    def cmd_discord(self, arg="toggle"):
        if arg == "on":
            return self.set_discord(True)
        if arg == "off":
            return self.set_discord(False)
        return self.set_discord(not self.discord)

    def _live_started_locked(self):
        # Called with self.lock held wherever state becomes "live".
        now = time.monotonic()
        self.live_since = now
        if self.discord:
            self.cap_deadline = now + DISCORD_CAP
            self.cap_ceiling = now + max(DISCORD_CAP_MAX, DISCORD_CAP)
            self.cap_warned = False
            # The tap itself counts as activity: the user gets the
            # full hangover window to start speaking before the
            # silence rule can close the mic.
            self.last_voice = now

    def note_audio(self, pcm):
        # Voice meter on the decoded mic stream (SCO reader thread).
        if not self.discord:
            return
        if _pcm_rms(pcm) >= DISCORD_VAD_RMS:
            self.last_voice = time.monotonic()

    def on_tap(self):
        """The temple tap arrived (MPRIS Play/Pause/PlayPause).

        An opener only. During a session the glasses keep their
        taps to themselves: AVRCP rides the A2DP side, which is
        parked for the call, and the taps surface on no other
        channel either (verified on hardware 2026-10-29: 4+ taps
        during continuous speech produced no AVRCP event, no
        +CKPD, no +VGS, nothing). A tap observed in a non-ready
        state is logged for the record and ignored."""
        if not self.discord:
            return
        if self.state == "ready":
            log("discord: tap — opening mic")
            threading.Thread(target=self._tap_open,
                             daemon=True).start()
        else:
            log(f"discord: tap ignored (state={self.state})")

    def _tap_open(self):
        log(f"discord: tap open -> {self.cmd_start()}")

    def _discord_monitor(self):
        while True:
            time.sleep(0.25)
            try:
                self._discord_tick()
            except Exception as e:      # never let the monitor die
                log(f"discord monitor error: {e}")

    def _discord_tick(self):
        if not self.discord:
            return
        with self.lock:
            if self.state != "live":
                return
            cap = self.cap_deadline
            ceiling = self.cap_ceiling
            warned = self.cap_warned
            last_voice = self.last_voice
        now = time.monotonic()
        if ceiling is not None and now >= ceiling:
            # The absolute limit: not even continuous speech holds
            # the session past this — the battery backstop.
            self._discord_close("absolute cap reached")
        elif cap is not None and now >= cap:
            if now - last_voice < DISCORD_HANGOVER:
                # Mid-speech at the cap: don't guillotine the
                # sentence. Warn once (a single mid tone, distinct
                # from the descending close chirp) and defer the
                # close to the next natural pause.
                if not warned:
                    with self.lock:
                        self.cap_warned = True
                    log("discord: cap reached mid-speech — "
                        "deferring close to the next pause")
                    sess = self.session
                    if sess is not None:
                        sess.play_cue_freqs((659,),
                                            blip=0.25, gap=0.0)
                return
            self._discord_close("cap — closed at pause" if warned
                                else "safety cap reached")
        elif now - last_voice >= DISCORD_HANGOVER:
            self._discord_close("silence")

    def _discord_close(self, reason):
        log(f"discord: closing mic ({reason})")
        sess = self.session
        if sess is not None:
            # Descending chirp first, while the link is still open —
            # the user hears the close coming, never a silent drop.
            sess.play_cue_freqs((880, 587))
        self.cmd_stop()

    # -- A2DP choreography --------------------------------------------------
    # The glasses refuse to OPEN an eSCO voice link while an A2DP media
    # stream is running to them (proven 2026-10-26 22:26: every attempt
    # ECONNREFUSED while a media stream was live; parking the card
    # made the start succeed at once). A2DP therefore stays parked for
    # the whole call — resuming it mid-call technically worked but the
    # glasses soon produced repeating double-beeps (22:31) — and is
    # handed back by cmd_stop, or immediately if the start fails.
    # Best-effort throughout: if pactl is
    # missing or errors, the start proceeds exactly as it did before
    # this choreography existed. We only ever restore a card WE parked,
    # and only if it was on a2dp-sink when we found it.

    @property
    def CARD(self):
        addr = self.device_address or GLASSES_MAC
        return "bluez_card." + addr.replace(":", "_") if addr else ""

    def _pactl(self, *args):
        try:
            r = subprocess.run(["pactl", *args], capture_output=True,
                               text=True, timeout=5)
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _card_profile(self):
        out = self._pactl("list", "cards")
        if not out:
            return None
        m = re.search(r"Name: " + re.escape(self.CARD) +
                      r"\n(?:.*\n)*?\tActive Profile: (\S+)", out)
        return m.group(1) if m else None

    def park_a2dp(self):
        if self._card_profile() != "a2dp-sink":
            return                      # not on A2DP: nothing to park
        if self._pactl("set-card-profile", self.CARD, "off") is None:
            return                      # couldn't park; proceed anyway
        with self.lock:
            self.a2dp_parked = True
        log("parked glasses A2DP for dictation start")
        # Let the transport close before the SCO attempt. Was 0.8 s;
        # trimmed 2026-10-29 for tap-to-talk latency — the SCO
        # worker's retry loop (12 attempts over ~3 s) absorbs the
        # remaining drain variance.
        time.sleep(0.2)

    def restore_a2dp(self):
        with self.lock:
            if not self.a2dp_parked:
                return
            self.a2dp_parked = False
        if self._pactl("set-card-profile", self.CARD,
                       "a2dp-sink") is not None:
            log("restored glasses A2DP")

    def reconcile_a2dp(self):
        # A park made by a PREVIOUS daemon process is invisible to
        # restore_a2dp (its flag died with that process), so a restart
        # while live leaves the card parked under a ready state and
        # nothing ever hands the music path back (observed
        # 2026-10-27). At connect time, a card sitting on "off" that
        # we did not park is an orphan: adopt and restore it.
        with self.lock:
            if self.a2dp_parked:
                return
        if self._card_profile() != "off":
            return
        if self._pactl("set-card-profile", self.CARD,
                       "a2dp-sink") is not None:
            log("adopted orphaned A2DP park — restored media mode")

    def cmd_media(self):
        # "Glasses are speakers now": end any call session, then put
        # the card on its media profile whether or not this daemon
        # process was the one who parked it.
        if self.state in ("live", "starting"):
            self.cmd_stop()
        if self._card_profile() not in (None, "a2dp-sink"):
            if self._pactl("set-card-profile", self.CARD,
                           "a2dp-sink") is not None:
                with self.lock:
                    self.a2dp_parked = False
                log("glasses set to media mode (A2DP)")
        return "ok: media"


# --------------------------------------------------------------------------
# One RFCOMM session with the glasses: the Samsung-replica AT engine,
# the silent-call dictation flow, and the SCO audio plumbing.

class Session:
    def __init__(self, sock, device_path, core):
        self.sock = sock
        self.device = str(device_path)
        self.core = core
        self.lock = threading.Lock()
        self.alive = True
        self.slc = False
        self.codecs = []
        self.codec_confirmed = None
        # call state: idle | dialing | active
        self.call_state = "idle"
        self.call_dir = 1
        self.clcc_stat = 0
        # SCO
        self.sco_sock = None
        self.sco_opening = False
        self.sco_lock = threading.Lock()   # cue vs downlink sends
        self.cue_hold = threading.Event()  # downlink pauses for cues
        self.parser = SCOParser(core.decoder)

    # -- RFCOMM plumbing --------------------------------------------------
    def send(self, text):
        shown = " / ".join(p for p in text.split("\r\n") if p)
        if shown:
            log(f"HF >> {shown}")
        with self.lock:
            try:
                self.sock.sendall(text.encode())
            except OSError:
                pass

    # -- codec / audio ------------------------------------------------------
    def offer_codec(self, why):
        self.codec_confirmed = None
        log(f"offering codec {CODEC_MSBC} ({why})")
        self.send(f"\r\n+BCS: {CODEC_MSBC}\r\n")

    def open_sco(self):
        if self.sco_sock is not None or self.sco_opening:
            return
        self.sco_opening = True
        threading.Thread(target=self._open_sco_worker,
                         daemon=True).start()

    def _open_sco_worker(self):
        # The glasses can refuse the first eSCO attempt (ECONNREFUSED)
        # if their audio path hasn't finished arming after the codec
        # confirmation — observed 2026-10-26 22:13 under A2DP load,
        # after identical attempts succeeded at 21:28/21:43. Retry for
        # a few seconds in a worker thread (never block the AT loop).
        s = None
        try:
            last = None
            address = self.core.device_address or GLASSES_MAC
            for attempt in range(12):
                if not self.alive or self.call_state != "active":
                    return
                if not address:
                    last = OSError("unknown glasses Bluetooth address")
                    break
                try:
                    s = socket.socket(socket.AF_BLUETOOTH,
                                      socket.SOCK_SEQPACKET,
                                      socket.BTPROTO_SCO)
                    s.setsockopt(SOL_BLUETOOTH, BT_VOICE,
                                 struct.pack("<H", BT_VOICE_MSBC))
                    s.settimeout(3)
                    s.connect(address)  # BlueZ device address string
                    break
                except OSError as e:
                    last = e
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass
                        s = None
                    if attempt == 0:
                        log(f"SCO connect refused ({e}); retrying")
                    time.sleep(0.25)
            if s is None:
                log(f"SCO connect failed after retries: {last}")
                # Don't leave the silent call standing with no audio:
                # end it so both sides return to a clean idle and the
                # next start dials fresh instead of hitting the zombie.
                self._end_call()
                with self.core.lock:
                    if self.core.state == "starting":
                        self.core.state = "ready"
                self.core.restore_a2dp()
                return
        finally:
            self.sco_opening = False
        s.settimeout(None)
        self.sco_sock = s
        with self.core.lock:
            self.core.state = "live"
            self.core._live_started_locked()
        log("SCO open — microphone live")
        # NOTE: A2DP deliberately stays parked for the call's
        # duration. Resuming it alongside the voice link worked, but
        # produced repeating double-beeps in the glasses within
        # minutes (2026-10-26 22:31; journal silent, stop cured it) —
        # coexistence renegotiation is the working theory. Phones
        # keep music paused for the whole call; so do we. cmd_stop
        # restores A2DP.
        threading.Thread(target=self._sco_reader, args=(s,),
                         daemon=True).start()
        threading.Thread(target=self._play_cue, args=(s,),
                         daemon=True).start()
        threading.Thread(target=self._downlink, args=(s,),
                         daemon=True).start()

    def _play_cue(self, s):
        try:
            for unit in CUE_UNITS:
                if self.sco_sock is not s:
                    return
                with self.sco_lock:
                    s.sendall(unit)
                time.sleep(0.0075)
        except OSError:
            pass

    def play_cue_freqs(self, freqs, blip=0.19, gap=0.055):
        """Play a short generated tone cue into the glasses over the
        live downlink: one sine blip per frequency, mSBC-encoded and
        framed exactly like the downlink stream. Used for the
        Discord-mode extend blip (single freq) and the descending
        close chirp (880 -> 587). The downlink thread holds while a
        cue plays, so cue frames never interleave with app audio."""
        s = self.sco_sock
        enc = self.core.encoder
        if s is None or enc is None:
            return
        units = []
        idx = 0

        def wrap(frame):
            nonlocal idx
            if len(frame) == 57:
                units.append(bytes((0x01, self.HEADER_B[idx % 4]))
                             + frame + b"\x00")
                idx += 1

        for freq in freqs:
            samples = array("h", (
                int(20000 * math.sin(2 * math.pi * freq * i / 16000))
                for i in range(int(blip * 16000))))
            for off in range(0, len(samples) - 119, 120):
                wrap(enc.encode_frame(
                    samples[off:off + 120].tobytes()))
            gap_frame = enc.encode_frame(b"\x00" * 240)
            for _ in range(int(gap / 0.0075)):
                wrap(gap_frame)
        self.cue_hold.set()
        try:
            for unit in units:
                if self.sco_sock is not s:
                    return
                with self.sco_lock:
                    try:
                        s.sendall(unit)
                    except OSError:
                        return
                time.sleep(0.0075)
        finally:
            self.cue_hold.clear()

    # Downlink framing, measured from the glasses' own uplink
    # (2026-10-26 capture): header [0x01, B] with B cycling
    # C8 -> F8 -> 08 -> 38, then the 57-byte frame, then 0x00.
    HEADER_B = (0xC8, 0xF8, 0x08, 0x38)

    def _downlink(self, s):
        """Stream call audio TO the glasses while the link is live:
        PCM from the glasses-spk sink (silence when no app is playing),
        mSBC-encoded and framed exactly as the glasses frame their
        own uplink. This makes the glasses a full-duplex Discord
        device — mic in, voices out — over the one SCO link, the way
        a phone call already works on them."""
        enc = self.core.encoder
        spk = self.core.spk
        if enc is None or spk is None:
            return
        time.sleep(0.45)            # let the connect cue finish first
        idx = 0
        next_t = time.monotonic()
        while self.sco_sock is s and self.alive:
            if self.cue_hold.is_set():
                # A cue owns the wire; resume on a fresh clock so we
                # never burst to "catch up" afterwards.
                time.sleep(0.01)
                next_t = time.monotonic()
                continue
            frame = enc.encode_frame(spk.take())
            if len(frame) == 57:
                unit = (bytes((0x01, self.HEADER_B[idx % 4]))
                        + frame + b"\x00")
                idx += 1
                with self.sco_lock:
                    if self.sco_sock is not s:
                        return
                    try:
                        s.sendall(unit)
                    except OSError:
                        return
            next_t += 0.0075
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:
                next_t = time.monotonic()

    def _sco_reader(self, s):
        try:
            while True:
                data = s.recv(1024)
                if not data:
                    break
                pcm = self.parser.feed(data)
                if pcm:
                    self.core.note_audio(pcm)
                    self.core.pipe.push(pcm)
        except OSError:
            pass
        log("SCO closed")
        if self.sco_sock is s:
            self.sco_sock = None
        if self.alive:
            # SCO dropped mid-dictation (out of range, interference):
            # fall back to ready so status tells the truth.
            with self.core.lock:
                if self.core.state == "live":
                    self.core.state = "ready"
                    self.core.live_since = None
                    self.core.cap_deadline = None
                    self.core.cap_ceiling = None
                    self.core.cap_warned = False

    def close_sco(self):
        if self.sco_sock is not None:
            try:
                self.sco_sock.close()
            except OSError:
                pass
            self.sco_sock = None

    # -- dictation flow -----------------------------------------------------
    def start_dictation(self):
        if self.call_state != "idle":
            return
        self.call_state = "dialing"
        self.call_dir = 0
        self.clcc_stat = 2
        threading.Thread(target=self._call_sequence,
                         daemon=True).start()

    def _call_sequence(self):
        # Silent gateway-declared outgoing call: no RING, no gesture.
        # The phase pacing was 0.4/0.6 s (phone-like); trimmed
        # 2026-10-29 because tap-to-talk made the dead time audible —
        # the glasses track the CIEV values, not the wall clock. If
        # call setup ever gets flaky, restore the slower pacing.
        self.send("\r\n+CIEV: 3,2\r\n")              # callsetup = dialing
        time.sleep(0.15)
        if self.call_state != "dialing":
            return
        self.clcc_stat = 3
        self.send("\r\n+CIEV: 3,3\r\n")              # callsetup = alerting
        time.sleep(0.25)
        if self.call_state != "dialing":
            return
        self.call_state = "active"
        self.clcc_stat = 0
        self.send("\r\n+CIEV: 2,1\r\n")              # call = 1
        self.send("\r\n+CIEV: 3,0\r\n")              # callsetup = 0
        log("silent call connected — requesting audio")
        # The glasses keep a SEPARATE gain for call audio (HFP scale
        # 0-15), set by the gateway — theirs sits near 5, which left
        # Discord output at a whisper no matter how high every PC
        # slider was (2026-10-26 22:50). A phone sets this on call
        # setup; so do we: full gain, once per call.
        self.send("\r\n+VGS: 15\r\n")
        log("set glasses speaker gain +VGS: 15")
        self.offer_codec("dictation start")

    def stop_dictation(self):
        self._end_call()

    def _end_call(self):
        was = self.call_state
        if was == "idle":
            self.close_sco()
            return
        self.call_state = "idle"
        self.clcc_stat = 0
        if was == "active":
            self.send("\r\n+CIEV: 2,0\r\n")          # call = 0
        self.send("\r\n+CIEV: 3,0\r\n")              # callsetup = 0
        self.close_sco()
        log("call ended")

    # -- AT command handling (the proven Samsung-replica script) ------------
    def handle(self, cmd):
        u = cmd.upper().strip()

        # Unsolicited indications from the glasses are never answered.
        if u.startswith("+BIEV:"):
            # HF indicator report: 1 = enhanced safety, 2 = battery.
            try:
                ind, val = (int(x) for x in u[6:].split(",")[:2])
                if ind == 2:
                    with self.core.lock:
                        self.core.battery = val
                    log(f"glasses battery: {val}%")
            except ValueError:
                pass
            return
        if u.startswith("+"):
            return

        if u.startswith("AT+BRSF="):
            self.send(f"\r\n+BRSF: {AG_BRSF}\r\n" + OK)
        elif u.startswith("AT+BAC="):
            self.codecs = [c.strip()
                           for c in u.split("=", 1)[1].split(",")]
            self.send(OK)
        elif u == "AT+CIND=?":
            self.send("\r\n" + CIND_TEST + "\r\n" + OK)
        elif u == "AT+CIND?":
            self.send(f"\r\n+CIND: {CIND_PHONE}\r\n" + OK)
        elif u.startswith("AT+CMER"):
            self.send(OK)
            if not self.slc:
                self.slc = True
                with self.core.lock:
                    if self.core.state == "down":
                        self.core.state = "ready"
                log("SLC established — glasses connected")
                self.core.reconcile_a2dp()
        elif u.startswith("AT+BIA="):
            self.send(OK)
        elif u == "ATA":
            self.send(OK)
        elif u.startswith("AT+CHUP") or u.startswith("AT+CHLD=0"):
            self.send(OK)
            if self.call_state != "idle":
                log("call ended from the glasses side")
                self._end_call()
                with self.core.lock:
                    if self.core.state in ("live", "starting"):
                        self.core.state = "ready"
        elif u == "AT+BIND=?":
            # Assignable HF indicators: 1 safety, 2 battery level.
            self.send("\r\n+BIND: (1,2)\r\n" + OK)
        elif u == "AT+BIND?":
            # We activate both — battery is the one we consume.
            self.send("\r\n+BIND: 1,1\r\n+BIND: 2,1\r\n" + OK)
        elif u.startswith("AT+BIND="):
            self.send(OK)
        elif u == "AT+BCC":
            # HF requests a codec connection: grant it only while a
            # dictation call is up, so the mic never opens uninvited.
            self.send(OK)
            if self.call_state == "active":
                self.offer_codec("on AT+BCC request")
        elif u.startswith("AT+BCS="):
            try:
                self.codec_confirmed = int(u.split("=", 1)[1])
            except ValueError:
                self.codec_confirmed = None
            self.send(OK)
            if (self.codec_confirmed == CODEC_MSBC
                    and self.call_state == "active"):
                self.open_sco()
        elif u.startswith("AT+BVRA"):
            # The glasses' assistant button: that path belongs to the
            # Meta phone app; acknowledge and do nothing.
            self.send(OK)
        elif u.startswith("AT+NREC"):
            self.send(OK)
        elif u.startswith("AT+COPS"):
            if "?" in u and "=" not in u:
                self.send('\r\n+COPS: 0,0,"FakeNet"\r\n' + OK)
            else:
                self.send(OK)
        elif u.startswith("AT+CNUM"):
            self.send(OK)
        elif u.startswith("AT+CLCC"):
            if self.call_state != "idle":
                self.send(f'\r\n+CLCC: 1,{self.call_dir},'
                          f'{self.clcc_stat},0,0,"15555550100",129\r\n'
                          + OK)
            else:
                self.send(OK)
        elif u.startswith("AT+CHLD"):
            if "=?" in u:
                self.send("\r\n+CHLD: (0,1,2,3,4)\r\n" + OK)
            else:
                self.send(OK)
        elif u.startswith("AT+VGS="):
            # The glasses reporting their speaker gain (temple
            # gesture). Acknowledge and note it — but never fight the
            # user by pushing our own value back mid-call.
            self.send(OK)
            log(f"glasses speaker gain: {u.split('=', 1)[1]}")
        elif u.startswith("AT+VGM"):
            self.send(OK)
        elif u.startswith(("AT+CLIP", "AT+CCWA", "AT+CMEE",
                           "AT+BSIR", "AT+BTRH", "ATD")):
            self.send(OK)
        elif u.startswith("AT+XAPL"):
            # Apple accessory identification — the glasses introduce
            # themselves with their vendor id (0x01AB). Accept it:
            # answering ERROR makes them withhold battery reports.
            self.send(OK)
        elif u.startswith("AT+IPHONEACCEV"):
            # Apple battery reporting: <count> then key/value pairs;
            # key 1 = battery level 0-9. Always OK, or they stop.
            try:
                fields = [int(x) for x in
                          u.split("=", 1)[1].split(",") if x != ""]
                pairs = fields[1:]
                for i in range(0, len(pairs) - 1, 2):
                    if pairs[i] == 1:
                        pct = max(0, min(100, pairs[i + 1] * 10))
                        with self.core.lock:
                            changed = self.core.battery != pct
                            self.core.battery = pct
                        if changed:
                            log(f"glasses battery: ~{pct}% "
                                f"(Apple level {pairs[i + 1]})")
            except ValueError:
                pass
            self.send(OK)
        elif u in ("AT", "ATZ", "ATE0", "ATE1"):
            self.send(OK)
        else:
            self.send(ERROR)

    # -- main loop ------------------------------------------------------------
    def run(self):
        log(f"session start: {self.device}")
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
                    if cmd:
                        log(f"HF << {cmd}")
                        self.handle(cmd)
        except OSError:
            pass
        finally:
            self.alive = False
            self.slc = False
            self.close_sco()
            try:
                self.sock.close()
            except OSError:
                pass
            with self.core.lock:
                if self.core.session is self:
                    self.core.session = None
                    self.core.state = "down"
                    self.core.live_since = None
                    self.core.cap_deadline = None
                    self.core.cap_ceiling = None
                    self.core.cap_warned = False
            log("session end")


# --------------------------------------------------------------------------
# MPRIS player: the tap swallower for Discord mode. While armed, this
# object owns org.mpris.MediaPlayer2.glassesag on the SESSION bus and
# advertises PlaybackStatus "Playing". The desktop's AVRCP bridge
# routes the glasses' temple-tap play/pause to a Playing player in
# preference to paused ones (proven 2026-10-28 with a stub player:
# the stub took every tap, paused Spotify never moved), so the taps
# land here as Play/Pause/PlayPause calls -> core.on_tap(). The name
# is only owned while armed; disarming releases it, and taps go back
# to behaving like ordinary media keys.

class MPRISPlayer(dbus.service.Object):
    ROOT = "org.mpris.MediaPlayer2"
    PLAYER = "org.mpris.MediaPlayer2.Player"
    PROPS = "org.freedesktop.DBus.Properties"

    def __init__(self, core):
        self.core = core
        self._name = None           # BusName while armed
        self.armed = False

    def arm(self):
        if self.armed:
            return
        try:
            bus = dbus.SessionBus()
            dbus.service.Object.__init__(self, bus, MPRIS_PATH)
            self._name = dbus.service.BusName(MPRIS_NAME, bus,
                                              do_not_queue=True)
        except dbus.DBusException as e:
            log(f"discord: MPRIS registration failed: "
                f"{e.get_dbus_name()}")
            return
        self.armed = True
        log("discord: MPRIS player live (advertising Playing)")

    def disarm(self):
        if not self.armed:
            return
        try:
            self.remove_from_connection()
        except Exception:
            pass
        self._name = None           # dropping the BusName releases it
        self.armed = False
        log("discord: MPRIS player released")

    # -- property plumbing ----------------------------------------------
    def _root_props(self):
        return {
            "CanQuit": dbus.Boolean(False),
            "CanRaise": dbus.Boolean(False),
            "HasTrackList": dbus.Boolean(False),
            "Identity": dbus.String("glasses-ag (Discord mode)"),
            "SupportedUriSchemes": dbus.Array([], signature="s"),
            "SupportedMimeTypes": dbus.Array([], signature="s"),
        }

    def _player_props(self):
        return {
            "PlaybackStatus": dbus.String(
                "Playing" if self.armed else "Stopped"),
            "LoopStatus": dbus.String("None"),
            "Rate": dbus.Double(1.0),
            "Shuffle": dbus.Boolean(False),
            "Metadata": dbus.Dictionary({}, signature="sv"),
            "Volume": dbus.Double(1.0),
            "Position": dbus.Int64(0),
            "MinimumRate": dbus.Double(1.0),
            "MaximumRate": dbus.Double(1.0),
            "CanGoNext": dbus.Boolean(False),
            "CanGoPrevious": dbus.Boolean(False),
            "CanPlay": dbus.Boolean(True),
            "CanPause": dbus.Boolean(True),
            "CanSeek": dbus.Boolean(False),
            "CanControl": dbus.Boolean(True),
        }

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        props = (self._player_props() if interface == self.PLAYER
                 else self._root_props())
        if prop in props:
            return props[prop]
        raise dbus.exceptions.DBusException(
            f"unknown property {prop}",
            name="org.freedesktop.DBus.Error.UnknownProperty")

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface == self.PLAYER:
            return self._player_props()
        if interface == self.ROOT:
            return self._root_props()
        return {}

    @dbus.service.signal(PROPS, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    # -- root interface ---------------------------------------------------
    @dbus.service.method(ROOT, in_signature="", out_signature="")
    def Raise(self):
        pass

    @dbus.service.method(ROOT, in_signature="", out_signature="")
    def Quit(self):
        pass

    # -- player interface: the tap lands here ------------------------------
    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Play(self):
        self.core.on_tap()

    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Pause(self):
        self.core.on_tap()

    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def PlayPause(self):
        self.core.on_tap()

    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Stop(self):
        if self.core.state == "live":
            log("discord: MPRIS Stop — closing mic")
            self.core.cmd_stop()

    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Next(self):
        pass

    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Previous(self):
        pass

    @dbus.service.method(PLAYER, in_signature="x", out_signature="")
    def Seek(self, offset):
        pass

    @dbus.service.method(PLAYER, in_signature="ox", out_signature="")
    def SetPosition(self, track, position):
        pass

    @dbus.service.method(PLAYER, in_signature="s", out_signature="")
    def OpenUri(self, uri):
        pass


# --------------------------------------------------------------------------
# BlueZ profile endpoints. Registration must happen BEFORE the glasses
# (re)connect, or BlueZ never offers us their HFP service.

class Profile(dbus.service.Object):
    def __init__(self, bus, path, core):
        super().__init__(bus, path)
        self.bus = bus
        self.core = core

    @dbus.service.method(PROFILE_IFACE, in_signature="", out_signature="")
    def Release(self):
        log("profile released by BlueZ")

    @dbus.service.method(PROFILE_IFACE, in_signature="oha{sv}",
                         out_signature="")
    def NewConnection(self, device, fd, properties):
        # Gate: serve Meta glasses only. Refusing any other device
        # here makes its HFP attempt fail fast so it can fall back
        # (for example, another headset can use HSP via WirePlumber).
        # Identification is either an exact address pin configured by
        # the owner, or the glasses' advertised name prefix.
        try:
            props = dbus.Interface(
                self.bus.get_object(BLUEZ, device),
                "org.freedesktop.DBus.Properties")

            def prop(name):
                try:
                    return str(props.Get("org.bluez.Device1", name))
                except dbus.DBusException:
                    return ""

            addr = prop("Address").upper()
            name = prop("Name") or prop("Alias")
        except dbus.DBusException:
            addr = "?"
            name = ""
        if GLASSES_MAC:
            accepted = addr == GLASSES_MAC
        else:
            accepted = bool(
                name and GLASSES_NAME_PREFIX and
                name.casefold().startswith(GLASSES_NAME_PREFIX.casefold()))
        if not accepted:
            log(f"refusing HFP connection from {addr} ({device})")
            raise dbus.exceptions.DBusException(
                "glasses-ag serves Meta glasses only",
                name="org.bluez.Error.Rejected")
        if addr and addr != "?":
            self.core.device_address = addr
        fd_int = fd.take()
        try:
            sock = socket.fromfd(fd_int, socket.AF_BLUETOOTH,
                                 socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        finally:
            os.close(fd_int)
        sock.settimeout(None)
        sess = Session(sock, device, self.core)
        with self.core.lock:
            self.core.session = sess
        threading.Thread(target=sess.run, daemon=True).start()

    @dbus.service.method(PROFILE_IFACE, in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        log(f"RequestDisconnection for {device}")


def register(bus, path, uuid, options):
    manager = dbus.Interface(bus.get_object(BLUEZ, MANAGER_PATH),
                             "org.bluez.ProfileManager1")
    try:
        manager.RegisterProfile(path, uuid, options)
        log(f"registered {uuid}")
        return True
    except dbus.DBusException as e:
        log(f"registration failed for {uuid}: {e.get_dbus_name()}")
        return False


def unregister(bus, path):
    try:
        manager = dbus.Interface(bus.get_object(BLUEZ, MANAGER_PATH),
                                 "org.bluez.ProfileManager1")
        manager.UnregisterProfile(path)
    except dbus.DBusException:
        pass


# --------------------------------------------------------------------------
# Control socket: line protocol — start | stop | toggle | media |
# discord [on|off] | status.

def control_server(core):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    try:
        os.unlink(CTL_PATH)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(CTL_PATH)
    srv.listen(4)
    log(f"control socket at {CTL_PATH}")
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2.0)
            try:
                raw = conn.recv(256).decode(errors="replace").strip()
            except OSError:
                continue
            cmd = raw.lower()
            if cmd == "start":
                reply = core.cmd_start()
            elif cmd == "stop":
                reply = core.cmd_stop()
            elif cmd == "toggle":
                reply = core.cmd_toggle()
            elif cmd == "media":
                reply = core.cmd_media()
            elif cmd == "discord":
                reply = core.cmd_discord("toggle")
            elif cmd == "discord_on":
                reply = core.cmd_discord("on")
            elif cmd == "discord_off":
                reply = core.cmd_discord("off")
            elif cmd == "status":
                reply = "ok: " + core.status()
            else:
                reply = f"err: unknown command {raw!r}"
            try:
                conn.sendall((reply + "\n").encode())
            except OSError:
                pass


# --------------------------------------------------------------------------

def main():
    log("glasses-ag starting")
    try:
        decoder = MSBCDecoder()
        encoder = MSBCEncoder()
    except RuntimeError as e:
        log(f"FATAL: {e}")
        return 1
    pipe = PipeSource(FIFO_PATH)
    pipe.start()
    spk = SpkSink(SPK_FIFO_PATH)
    spk.start()
    core = AGCore(pipe, decoder, encoder, spk)
    core.mpris = MPRISPlayer(core)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    Profile(bus, AG_PATH_HF, core)
    Profile(bus, AG_PATH_AG, core)

    ok = False
    for attempt in range(20):
        ok1 = register(bus, AG_PATH_HF, HF_UUID,
                       {"Name": "GlassesAG Handsfree", "Role": "client"})
        ok2 = register(bus, AG_PATH_AG, AG_UUID,
                       {"Name": "Voice gateway", "Role": "server",
                        "Channel": dbus.UInt16(13),
                        "RequireAuthentication": dbus.Boolean(False),
                        "RequireAuthorization": dbus.Boolean(False),
                        "AutoConnect": dbus.Boolean(False),
                        "Version": dbus.UInt16(0x0109),
                        "Features": dbus.UInt16(0x0120)})
        ok = ok1 and ok2
        if ok:
            break
        log(f"registration attempt {attempt + 1} failed; retrying in 3 s "
            "(is WirePlumber still holding HFP? see 51-glasses-ag.conf)")
        time.sleep(3)
    if not ok:
        log("FATAL: could not register HFP profiles")
        return 1

    threading.Thread(target=control_server, args=(core,),
                     daemon=True).start()
    log("ready — waiting for the glasses to connect")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        unregister(bus, AG_PATH_HF)
        unregister(bus, AG_PATH_AG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
