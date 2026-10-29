"""hfp_media — codec and FIFO helpers shared by the glasses path and the
generic-device audio path.

Pure Python plus ctypes/libsbc at construction time only: importable
without D-Bus, PipeWire, or Bluetooth, so tests can fake the boundary.
These classes were moved verbatim out of glasses_ag.py in v2 Step C so
the generic path reuses the proven decoder/encoder/parser/FIFO code
instead of forking it. The only additions are stop() on PipeSource and
SpkSink (per-device instances come and go; the glasses' two live for
the daemon's lifetime and never call it) and the H2 header table.
"""

import ctypes
import os
import threading
import time
from collections import deque
from datetime import datetime

# H2 sync-header second byte cycles through these four values on the
# wire (HFP 1.8 §5.7.3 / measured from the glasses' own uplink).
H2_HEADER_B = (0xC8, 0xF8, 0x08, 0x38)


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

    def stop(self):
        """End the writer thread (per-device instances only)."""
        self.running = False

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
        self.running = True

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

    def stop(self):
        """End the reader thread (per-device instances only). The
        reader may be parked in a blocking open() waiting for a
        writer, so nudge it by opening the write end ourselves."""
        self.running = False
        try:
            fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass

    def _run(self):
        os.makedirs(os.path.dirname(self.fifo_path), exist_ok=True)
        try:
            os.mkfifo(self.fifo_path)
        except FileExistsError:
            pass
        while self.running:
            try:
                fd = os.open(self.fifo_path, os.O_RDONLY)
            except OSError:
                time.sleep(1.0)     # tunnel writer not there yet
                continue
            if not self.running:
                os.close(fd)
                break
            log("spk-sink: PipeWire writer connected")
            try:
                while self.running:
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
