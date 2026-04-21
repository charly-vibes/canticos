/**
 * Vitral — Ptyxis transparency controls
 *
 *   Ctrl+0  — toggle opacity (0.75 ↔ 1.0)
 *   Ctrl+8  — decrease opacity by 0.05 (more transparent)
 *   Ctrl+9  — increase opacity by 0.05 (less transparent)
 *
 * Works in windowed and quake-terminal mode. Fullscreen transparency is
 * blocked by Ptyxis (intentional) + Wayland direct scanout. That would
 * require patching Ptyxis source (issue #408).
 */

import Meta from 'gi://Meta';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const TRANSPARENT_OPACITY = 0.75;
const OPACITY_STEP = 0.05;
const OPACITY_MIN = 0.1;

export default class Vitral extends Extension {
  private _settings: Gio.Settings | null = null;
  private _profileSettings: Gio.Settings | null = null;

  enable(): void {
    this._settings = this.getSettings();
    this._initProfileSettings();

    const flags = Meta.KeyBindingFlags.NONE;
    const modes = Shell.ActionMode.ALL;
    Main.wm.addKeybinding('toggle-transparency', this._settings, flags, modes,
      () => this._toggleTransparency());
    Main.wm.addKeybinding('increase-opacity', this._settings, flags, modes,
      () => this._stepOpacity(OPACITY_STEP));
    Main.wm.addKeybinding('decrease-opacity', this._settings, flags, modes,
      () => this._stepOpacity(-OPACITY_STEP));
  }

  disable(): void {
    Main.wm.removeKeybinding('toggle-transparency');
    Main.wm.removeKeybinding('increase-opacity');
    Main.wm.removeKeybinding('decrease-opacity');
    this._settings = null;
    this._profileSettings = null;
  }

  private _initProfileSettings(): void {
    try {
      const app = new Gio.Settings({ schema_id: 'org.gnome.Ptyxis' });
      const uuid = app.get_string('default-profile-uuid');
      this._profileSettings = new Gio.Settings({
        schema_id: 'org.gnome.Ptyxis.Profile',
        path: `/org/gnome/Ptyxis/Profiles/${uuid}/`,
      });
    } catch {}
  }

  private _toggleTransparency(): void {
    if (!this._profileSettings) return;
    const current = this._profileSettings.get_double('opacity');
    this._setAllProfilesOpacity(current < 1.0 ? 1.0 : TRANSPARENT_OPACITY);
  }

  private _stepOpacity(delta: number): void {
    if (!this._profileSettings) return;
    const current = this._profileSettings.get_double('opacity');
    const next = Math.round(Math.min(1.0, Math.max(OPACITY_MIN, current + delta)) * 100) / 100;
    this._setAllProfilesOpacity(next);
  }

  private _setAllProfilesOpacity(value: number): void {
    try {
      const app = new Gio.Settings({ schema_id: 'org.gnome.Ptyxis' });
      const uuids = app.get_strv('profile-uuids');
      for (const uuid of uuids) {
        const profile = new Gio.Settings({
          schema_id: 'org.gnome.Ptyxis.Profile',
          path: `/org/gnome/Ptyxis/Profiles/${uuid}/`,
        });
        profile.set_double('opacity', value);
      }
    } catch {
      if (this._profileSettings) this._profileSettings.set_double('opacity', value);
    }
  }
}
