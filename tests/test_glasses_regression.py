"""Glasses-tenant regression at the fake boundary: with dbus/gi faked,
import the real daemon module and check that (a) a glasses-fingerprint
connection still routes to the untouched Samsung-replica Session, and a
headset to the generic one, and (b) the glasses AT script's replies are
byte-for-byte what they were. No Bluetooth, PipeWire, or root."""

import os
import socket as real_socket
import sys
import threading
import types

import pytest

import hfp_registry as hr


def _install_fakes(monkeypatch):
    def passthrough(*a, **k):
        return lambda f: f

    class Obj:
        def __init__(self, *a, **k):
            pass

    dbus = types.ModuleType("dbus")
    svc = types.ModuleType("dbus.service")
    svc.Object, svc.method, svc.signal = Obj, passthrough, passthrough
    exc = types.ModuleType("dbus.exceptions")

    class DBusException(Exception):
        def __init__(self, msg="", name=""):
            super().__init__(msg)
            self.name = name
    exc.DBusException = DBusException
    dbus.DBusException = DBusException
    ml = types.ModuleType("dbus.mainloop")
    glib = types.ModuleType("dbus.mainloop.glib")
    gi = types.ModuleType("gi")
    rep = types.ModuleType("gi.repository")
    rep.GLib = object()
    dbus.service, dbus.exceptions, dbus.mainloop = svc, exc, ml
    ml.glib = glib
    gi.repository = rep
    for name, mod in (("dbus", dbus), ("dbus.service", svc),
                      ("dbus.exceptions", exc), ("dbus.mainloop", ml),
                      ("dbus.mainloop.glib", glib), ("gi", gi),
                      ("gi.repository", rep)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "glasses_ag", raising=False)
    import glasses_ag
    return glasses_ag


@pytest.fixture
def ga(monkeypatch):
    return _install_fakes(monkeypatch)


class FakeSock:
    def __init__(self):
        self.sent = b""

    def sendall(self, d):
        self.sent += d

    def recv(self, n):
        return b""

    def close(self):
        pass

    def settimeout(self, t):
        pass


class Core:
    """Just enough AGCore surface for Session.handle / run."""

    def __init__(self):
        self.lock = threading.RLock()
        self.state = "down"
        self.session = None
        self.decoder = None
        self.battery = None
        self.device_address = None
        self.live_since = self.cap_deadline = self.cap_ceiling = None
        self.cap_warned = False
        self.reconciled = 0

    def reconcile_a2dp(self):
        self.reconciled += 1


def replies(text):
    return [p for p in text.split("\r\n") if p]


def test_glasses_samsung_script_replies_unchanged(ga):
    core = Core()
    s = ga.Session(FakeSock(), "/dev/g", core, address="AA", registry=None)

    def ask(cmd):
        start = len(s.sock.sent)
        s.handle(cmd)
        return replies(s.sock.sent[start:].decode())

    assert ask("AT+BRSF=3584") == ["+BRSF: 4079", "OK"]
    assert ask("AT+BAC=1,2") == ["OK"]
    assert ask("AT+CIND=?")[0].startswith('+CIND: ("service",(0-1))')
    assert ask("AT+CIND?") == ["+CIND: 1,0,0,0,4,0,5", "OK"]
    assert ask("AT+CMER=3,0,0,1") == ["OK"]
    assert s.slc and core.state == "ready" and core.reconciled == 1
    assert ask("AT+CHLD=?") == ["+CHLD: (0,1,2,3,4)", "OK"]
    assert ask("AT+BIND=?") == ["+BIND: (1,2)", "OK"]
    assert ask("AT+CLCC") == ["OK"]                  # idle
    assert ask("AT+BCC") == ["OK"]                   # no call: no offer
    assert ask("AT+XAPL=ab-cd,2") == ["OK"]          # glasses quirk kept
    assert ask("AT+IPHONEACCEV=1,1,5") == ["OK"]
    assert core.battery == 50
    assert ask("AT+NOPE") == ["ERROR"]


def test_glasses_connection_routes_to_session_and_headset_to_generic(
        ga, monkeypatch):
    core = Core()
    reg = hr.DeviceRegistry(hr.FingerprintConfig("", "Meta Glasses"),
                            log=lambda m: None)
    prof = ga.Profile(None, "/p", core, reg)

    infos = {
        "/g": hr.DeviceInfo("AA:BB:CC:DD:EE:01", name="Meta Glasses 1A",
                            paired=True),
        "/h": hr.DeviceInfo("11:22:33:44:55:66", name="Corsair HS55",
                            paired=True),
        "/x": hr.DeviceInfo("?"),
    }
    monkeypatch.setattr(prof, "read_device_info", lambda d: infos[d])
    monkeypatch.setattr(ga.socket, "fromfd",
                        lambda *a: FakeSock())
    monkeypatch.setattr(os, "close", lambda fd: None)
    started = []
    monkeypatch.setattr(
        threading, "Thread",
        lambda target=None, **k: types.SimpleNamespace(
            start=lambda: started.append(target.__self__)))

    class Fd:
        def take(self):
            return 99

    prof.NewConnection("/g", Fd(), {})
    prof.NewConnection("/h", Fd(), {})
    g, h = started
    assert type(g).__name__ == "Session"             # frozen glasses
    assert type(h).__name__ == "GenericSession"
    assert core.session is g                         # shared controller
    assert core.device_address == "AA:BB:CC:DD:EE:01"
    assert reg.get("11:22:33:44:55:66").session is h
    assert reg.get("AA:BB:CC:DD:EE:01").session is g

    # The generic device never touches the glasses controller.
    h.sock = FakeSock()
    h.handle("AT+BRSF=0")
    assert core.session is g and core.state == "down"

    # Only an unidentifiable device is refused at connect.
    with pytest.raises(sys.modules["dbus"].DBusException) as ei:
        prof.NewConnection("/x", Fd(), {})
    assert ei.value.name == "org.bluez.Error.Rejected"
    assert "unreadable" in str(ei.value)


def test_reconnect_closes_superseded_generic_link(ga, monkeypatch):
    core = Core()
    reg = hr.DeviceRegistry(hr.FingerprintConfig("", "Meta Glasses"),
                            log=lambda m: None)
    prof = ga.Profile(None, "/p", core, reg)
    info = hr.DeviceInfo("11:22:33:44:55:66", name="Corsair HS55",
                         paired=True)
    monkeypatch.setattr(prof, "read_device_info", lambda d: info)
    socks = []

    def fromfd(*a):
        socks.append(FakeSock())
        socks[-1].closed = False
        socks[-1].close = lambda s=socks[-1]: setattr(s, "closed", True)
        return socks[-1]
    monkeypatch.setattr(ga.socket, "fromfd", fromfd)
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(
        threading, "Thread",
        lambda target=None, **k: types.SimpleNamespace(start=lambda: None))

    class Fd:
        def take(self):
            return 99

    prof.NewConnection("/h", Fd(), {})
    prof.NewConnection("/h", Fd(), {})
    assert socks[0].closed and not socks[1].closed
