# Anatómico

> *"Quiero un zoom anatómico"* — Soda Stereo, *Zoom* (1995)

A minimal [ZoomIt](https://learn.microsoft.com/en-us/sysinternals/downloads/zoomit) clone for **GNOME Shell** — native Wayland, no XWayland needed.

Written in TypeScript, compiled to GJS-compatible JavaScript, and installed as a standard GNOME Shell extension.

## Features

| Feature      | Details                                    |
| ------------ | ------------------------------------------ |
| Freehand pen | Draw anywhere on screen                    |
| Arrows       | Click + drag for arrows with heads         |
| Rectangles   | Click + drag for outlined rectangles       |
| Ellipses     | Click + drag for outlined ellipses         |
| Text         | Click to place, type, press Enter          |
| Zoom         | Uses GNOME's built-in screen magnifier     |
| Undo         | Ctrl+Z to undo last stroke                 |
| 5 colors     | Red, Blue, Green, Yellow, White (keys 1–5) |
| 4 line widths| Cycle with toolbar button                  |
| Configurable hotkey | Change the activation shortcut via Extensions preferences |

## Requirements

- GNOME Shell 45, 46, 47, or 48
- Node.js 18+ (build only)
- `glib-compile-schemas` (usually pre-installed)

## Quick Start

```bash
# Install build deps
npm install

# Type-check (optional)
npm run check

# Build TypeScript → JavaScript
npm run build

# Install to GNOME Shell
bash install.sh

# Restart GNOME Shell (Wayland = log out/in, X11 = Alt+F2 → r)
# Then enable:
gnome-extensions enable anatomico@local

# Activate with Super+Z!
```

## Keyboard Shortcuts

| Key        | Action               |
| ---------- | -------------------- |
| `Super+Z`  | Toggle overlay on/off (configurable) |
| `P`        | Pen tool             |
| `A`        | Arrow tool           |
| `R`        | Rectangle tool       |
| `E`        | Ellipse tool         |
| `T`        | Text tool            |
| `1`–`5`    | Switch color         |
| `+` / `-`  | Zoom in / out        |
| `Ctrl+Z`   | Undo last stroke     |
| `C`        | Clear all strokes    |
| `Esc`      | Close overlay        |

## Bluefin / Atomic Distro Notes

This extension installs entirely to `~/.local/share/gnome-shell/extensions/` — **no rpm-ostree layering needed**. The only system dependency is `glib-compile-schemas`, which ships with every GNOME installation.

## Project Structure

```
anatomico/
├── src/
│   ├── extension.ts          # Main extension (TypeScript)
│   ├── prefs.ts              # Preferences UI (keybinding config)
│   ├── gjs.d.ts              # GJS/GNOME Shell type declarations
│   ├── metadata.json         # Extension metadata
│   ├── stylesheet.css        # CSS overrides (minimal)
│   └── schemas/              # GSettings schema (keybinding)
├── build.mjs                 # esbuild compile script
├── install.sh                # Install to GNOME Shell
├── tsconfig.json             # TypeScript config
├── package.json              # npm scripts + deps
└── README.md
```

## Development

```bash
# Build, install, and reload the extension
npm run dev

# Type-check without building
npm run check

# View GNOME Shell logs for debugging
journalctl -f -o cat /usr/bin/gnome-shell
```

## How It Works

The extension creates a fullscreen transparent `St.DrawingArea` overlay using `Main.layoutManager.addTopChrome()`. Because this runs inside GNOME Shell's Mutter compositor, it works natively on Wayland — no layer-shell protocol needed.

Drawing is done via Cairo on the `St.DrawingArea` repaint signal. Strokes are stored as typed data structures and re-rendered on each frame. Zoom leverages GNOME's built-in accessibility magnifier via `org.gnome.desktop.a11y.magnifier` GSettings.

## License

MIT
