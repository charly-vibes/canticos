/**
 * Vitral — keep Ptyxis transparent in fullscreen
 *
 * Ptyxis intentionally makes the VTE background opaque when entering fullscreen
 * (commit "window: make background opaque when in fullscreen", May 2025).
 * This extension works around it by applying compositor-level opacity to the
 * Ptyxis window actor when fullscreen, bypassing Ptyxis's internal override.
 * Opacity is read from the active Ptyxis profile so it stays in sync with the
 * profile setting (and the tmux Ctrl+0 toggle).
 */

import Meta from 'gi://Meta';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const PTYXIS_WM_CLASS = 'org.gnome.Ptyxis';
const FALLBACK_OPACITY = 0.75;

export default class Vitral extends Extension {
  private _windowSignals: Map<Meta.Window, number[]> = new Map();
  private _displaySignalId = 0;

  enable(): void {
    for (const actor of global.get_window_actors()) {
      const win = actor.get_meta_window();
      if (this._isPtyxis(win)) this._trackWindow(win);
    }

    this._displaySignalId = global.display.connect(
      'window-created',
      (_: Meta.Display, win: Meta.Window) => {
        if (this._isPtyxis(win)) this._trackWindow(win);
      }
    );
  }

  disable(): void {
    if (this._displaySignalId) {
      global.display.disconnect(this._displaySignalId);
      this._displaySignalId = 0;
    }
    for (const [win, ids] of this._windowSignals) {
      this._setOpacity(win, 255);
      for (const id of ids) try { win.disconnect(id); } catch {}
    }
    this._windowSignals.clear();
  }

  // EXCL-003: exact match instead of includes()
  private _isPtyxis(win: Meta.Window): boolean {
    return (win.get_wm_class() ?? '') === PTYXIS_WM_CLASS;
  }

  // CLAR-001/EXCL-001: read from active profile so Ctrl+0 stays in sync
  private _ptyxisOpacity(): number {
    try {
      const app = new Gio.Settings({ schema_id: 'org.gnome.Ptyxis' });
      const uuid = app.get_string('default-profile-uuid');
      const profile = new Gio.Settings({
        schema_id: 'org.gnome.Ptyxis.Profile',
        path: `/org/gnome/Ptyxis/Profiles/${uuid}/`,
      });
      return profile.get_double('opacity');
    } catch {
      return FALLBACK_OPACITY;
    }
  }

  // EDGE-001: retry via idle_add if actor isn't composited yet
  private _setOpacity(win: Meta.Window, opacity: number): void {
    const actor = win.get_compositor_private();
    if (actor) {
      actor.opacity = opacity;
    } else {
      GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
        const a = win.get_compositor_private();
        if (a) a.opacity = opacity;
        return GLib.SOURCE_REMOVE;
      });
    }
  }

  private _trackWindow(win: Meta.Window): void {
    // EDGE-003: guard against tracking the same window twice
    if (this._windowSignals.has(win)) return;

    const fullscreenId = win.connect('notify::fullscreen', (w: Meta.Window) => {
      const opacity = w.is_fullscreen()
        ? Math.round(this._ptyxisOpacity() * 255)
        : 255;
      // CORR-002: defer past the transition animation frame
      GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
        this._setOpacity(w, opacity);
        return GLib.SOURCE_REMOVE;
      });
    });

    const unmanagedId = win.connect('unmanaged', (w: Meta.Window) => {
      const ids = this._windowSignals.get(w) ?? [];
      for (const id of ids) try { w.disconnect(id); } catch {}
      this._windowSignals.delete(w);
    });

    this._windowSignals.set(win, [fullscreenId, unmanagedId]);

    // Apply immediately if already fullscreen when extension enables
    if (win.is_fullscreen()) {
      this._setOpacity(win, Math.round(this._ptyxisOpacity() * 255));
    }
  }
}
