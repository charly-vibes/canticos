#!/usr/bin/env bash
set -euo pipefail

UUID="vitral@local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"

echo "Installing Vitral → $EXT_DIR"

rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"

cp dist/extension.js  "$EXT_DIR/"
cp dist/metadata.json "$EXT_DIR/"

if [ -d dist/schemas ]; then
  cp -r dist/schemas "$EXT_DIR/"
  echo "  Compiling GSettings schemas…"
  glib-compile-schemas "$EXT_DIR/schemas/"
fi

echo ""
echo "✓ Installed!"
echo ""
echo "Next steps:"
echo "  1. Log out and back in (Wayland)"
echo "  2. gnome-extensions enable $UUID"
echo "  3. Press Ctrl+0 to toggle transparency"
