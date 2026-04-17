#!/usr/bin/env bash
set -euo pipefail

UUID="anatomico@local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"

echo "Installing Anatómico → $EXT_DIR"

# Clean previous install
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"

# Copy built files
cp dist/extension.js  "$EXT_DIR/"
cp dist/prefs.js      "$EXT_DIR/"
cp dist/metadata.json "$EXT_DIR/"
cp dist/stylesheet.css "$EXT_DIR/"

# Copy and compile GSettings schemas
if [ -d dist/schemas ]; then
  cp -r dist/schemas "$EXT_DIR/"
  echo "  Compiling GSettings schemas…"
  glib-compile-schemas "$EXT_DIR/schemas/"
fi

echo ""
echo "✓ Installed!"
echo ""
echo "Next steps:"
echo "  1. Restart GNOME Shell:"
echo "     • Wayland: log out and log back in"
echo "     • X11:     press Alt+F2, type 'r', press Enter"
echo ""
echo "  2. Enable the extension:"
echo "     gnome-extensions enable $UUID"
echo ""
echo "  3. Press Super+Z to activate!"
