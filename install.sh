#!/usr/bin/env bash
# glasses-ag installer — user-level only, no sudo. If a system package
# is missing it tells you the one command to run and stops.
set -euo pipefail
cd "$(dirname "$0")"

echo "== checking dependencies =="
python3 -c "import dbus" 2>/dev/null || {
  echo "MISSING: python-dbus / python-gobject"
  echo "Run:  sudo pacman -S python-dbus python-gobject"
  exit 1; }
ldconfig -p | grep "libsbc" > /dev/null || {
  echo "MISSING: libsbc (mSBC codec library)"
  echo "Run:  sudo pacman -S sbc"
  exit 1; }
echo "dependencies OK (dbus-python, libsbc)"

RT="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "== installing files =="
install -Dm755 glasses_ag.py "$HOME/.local/bin/glasses_ag.py"
install -Dm644 hfp_registry.py "$HOME/.local/bin/hfp_registry.py"
install -Dm755 glasses-mic  "$HOME/.local/bin/glasses-mic"
install -Dm755 glasses-waybar "$HOME/.local/bin/glasses-waybar"
install -Dm644 glasses-ag.service \
  "$HOME/.config/systemd/user/glasses-ag.service"
install -Dm644 51-glasses-ag.conf \
  "$HOME/.config/wireplumber/wireplumber.conf.d/51-glasses-ag.conf"
mkdir -p "$HOME/.config/pipewire/pipewire.conf.d"
sed "s|__RUNTIME_DIR__|$RT|g" 52-glasses-mic.conf.tmpl \
  > "$HOME/.config/pipewire/pipewire.conf.d/52-glasses-mic.conf"
sed "s|__RUNTIME_DIR__|$RT|g" 53-glasses-spk.conf.tmpl \
  > "$HOME/.config/pipewire/pipewire.conf.d/53-glasses-spk.conf"
echo "installed to ~/.local/bin, ~/.config/systemd/user,"
echo "  ~/.config/wireplumber/wireplumber.conf.d, ~/.config/pipewire/pipewire.conf.d"

echo "== restarting audio stack, then the daemon =="
echo "(WirePlumber first releases HFP; PipeWire picks up the mic source)"
systemctl --user daemon-reload
systemctl --user restart pipewire.service
systemctl --user restart wireplumber.service
systemctl --user enable --now glasses-ag.service
sleep 2

echo
echo "== verify =="
systemctl --user --no-pager status glasses-ag.service | head -6 || true
echo
echo "Glasses mic node (should list a 'glasses-mic' source):"
wpctl status 2>/dev/null | grep -i -A2 "glasses" || \
  echo "  (not visible yet — check: journalctl --user -u glasses-ag -e)"
echo
echo "Now (re)connect the glasses, then:"
echo "  glasses-mic status     # state=ready once the HFP session is up"
echo "  glasses-mic toggle     # two blips = mic live; again = off"
echo
echo "Other Bluetooth headsets should still offer a headset profile via HSP."
