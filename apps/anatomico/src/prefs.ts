/**
 * Anatómico — Preferences UI
 *
 * Lets the user pick the keyboard shortcut that toggles the overlay.
 * Runs in a separate process (not inside GNOME Shell), so it uses Gtk4 + Adw.
 */

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gdk from 'gi://Gdk';
import { ExtensionPreferences } from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const KEYBINDING_KEY = 'toggle-anatomico';

export default class AnatomicoPrefences extends ExtensionPreferences {
  fillPreferencesWindow(window: Adw.PreferencesWindow): void {
    const settings = this.getSettings('org.gnome.shell.extensions.anatomico');

    const page = new Adw.PreferencesPage({
      title: 'General',
      icon_name: 'preferences-system-symbolic',
    });

    const group = new Adw.PreferencesGroup({
      title: 'Keyboard Shortcuts',
    });

    const row = new Adw.ActionRow({
      title: 'Toggle Overlay',
      subtitle: 'Shortcut to activate/deactivate the annotation overlay',
    });

    const keybindings = settings.get_strv(KEYBINDING_KEY);
    const current = keybindings.length > 0 ? keybindings[0] : '';

    const shortcutLabel = new Gtk.ShortcutLabel({
      accelerator: current,
      valign: Gtk.Align.CENTER,
    });

    const editBtn = new Gtk.Button({
      icon_name: 'document-edit-symbolic',
      valign: Gtk.Align.CENTER,
      css_classes: ['flat'],
    });

    editBtn.connect('clicked', () => {
      _captureKeybinding(window, settings, shortcutLabel);
    });

    row.add_suffix(shortcutLabel);
    row.add_suffix(editBtn);
    row.set_activatable_widget(editBtn);

    group.add(row);
    page.add(group);
    window.add(page);
  }
}

function _captureKeybinding(
  parent: Adw.PreferencesWindow,
  settings: any,
  shortcutLabel: Gtk.ShortcutLabel,
): void {
  const dialog = new Adw.Window({
    modal: true,
    transient_for: parent,
    title: '',
    default_width: 360,
    default_height: 200,
  });

  const box = new Gtk.Box({
    orientation: Gtk.Orientation.VERTICAL,
    spacing: 12,
    margin_top: 30,
    margin_bottom: 30,
    margin_start: 30,
    margin_end: 30,
    valign: Gtk.Align.CENTER,
  });

  box.append(new Gtk.Label({
    label: 'Press a key combination',
    css_classes: ['title-2'],
  }));

  box.append(new Gtk.Label({
    label: 'Escape to cancel \u00b7 BackSpace to disable shortcut',
    css_classes: ['dim-label'],
  }));

  const toolbar = new Adw.ToolbarView();
  toolbar.add_top_bar(new Adw.HeaderBar());
  toolbar.set_content(box);
  dialog.set_content(toolbar);

  const controller = new Gtk.EventControllerKey();
  controller.connect('key-pressed', (
    _ctrl: any,
    keyval: number,
    _keycode: number,
    state: number,
  ) => {
    if (keyval === Gdk.KEY_Escape) {
      dialog.close();
      return true;
    }

    if (keyval === Gdk.KEY_BackSpace) {
      settings.set_strv(KEYBINDING_KEY, []);
      shortcutLabel.set_accelerator('');
      dialog.close();
      return true;
    }

    // Ignore bare modifier keys
    const MODIFIER_KEYS = [
      Gdk.KEY_Control_L, Gdk.KEY_Control_R,
      Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
      Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
      Gdk.KEY_Super_L, Gdk.KEY_Super_R,
      Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
    ];
    if (MODIFIER_KEYS.includes(keyval)) {
      return true;
    }

    const mask = state & (
      Gdk.ModifierType.CONTROL_MASK |
      Gdk.ModifierType.SHIFT_MASK |
      Gdk.ModifierType.ALT_MASK |
      Gdk.ModifierType.SUPER_MASK
    );

    const accel = Gtk.accelerator_name(keyval, mask);
    if (accel) {
      settings.set_strv(KEYBINDING_KEY, [accel]);
      shortcutLabel.set_accelerator(accel);
      dialog.close();
    }

    return true;
  });

  dialog.add_controller(controller);
  dialog.present();
}
