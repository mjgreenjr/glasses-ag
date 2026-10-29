# The glasses on your bar

The included `glasses-waybar` helper prints Waybar-style JSON. It
works as a Waybar custom module and as a command module in shells
that accept that format, including Omarchy's Quickshell-based bar.
Battery display requires a daemon build that reports battery status.

For Omarchy users, one safety note: once your own
`~/.config/omarchy/shell.json` exists it is canonical — there is no
deep-merge with the defaults. If you don't have one yet, let an
`omarchy bar ...` command or the bar's own gestures create it before
hand-editing, so you don't have to reconstruct the default layout.

## Command module (uses glasses-waybar)

Add to `bar.layout.right` in shell.json, near the bluetooth entry:

```json
{ "id": "glasses", "type": "command",
  "exec": "~/.local/bin/glasses-waybar", "interval": 5,
  "onClick": "glasses-mic toggle" }
```

The script prints Waybar-style JSON (the shell's documented
command-module format): icon + battery as the label, the full
status as the tooltip, and it prints nothing while the glasses
are disconnected so the module collapses.

## Native bar plugins

A native QML plugin is not included in this repository. The command
module above is the supported bar integration here.

## Icon

The helper uses the Nerd Font Font Awesome "glasses" glyph
(U+F530). If it renders as a box, swap the `ICON` constant in
`glasses-waybar` — `🕶` works with a color emoji font.
