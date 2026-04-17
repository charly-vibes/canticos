// Minimal GJS type declarations for GNOME Shell extension development
// For full types, install: npm i @girs/gnome-shell

declare module 'gi://St' {
  import type Clutter from 'gi://Clutter';

  export class Widget extends Clutter.Actor {
    constructor(params?: Partial<{
      reactive: boolean;
      can_focus: boolean;
      track_hover: boolean;
      x: number;
      y: number;
      width: number;
      height: number;
      style: string;
      style_class: string;
      layout_manager: Clutter.LayoutManager;
      x_expand: boolean;
      y_expand: boolean;
      x_align: Clutter.ActorAlign;
      y_align: Clutter.ActorAlign;
    }>);
    add_child(child: Clutter.Actor): void;
    remove_child(child: Clutter.Actor): void;
    remove_all_children(): void;
    destroy(): void;
    grab_key_focus(): void;
    set_size(width: number, height: number): void;
    set_position(x: number, y: number): void;
    connect(signal: string, callback: (...args: any[]) => any): number;
    disconnect(id: number): void;
    style: string | null;
    style_class: string;
    visible: boolean;
    reactive: boolean;
    width: number;
    height: number;
    x: number;
    y: number;
  }

  export class DrawingArea extends Widget {
    constructor(params?: Partial<{
      reactive: boolean;
      can_focus: boolean;
      width: number;
      height: number;
      style: string;
      x_expand: boolean;
      y_expand: boolean;
    }>);
    connect(signal: 'repaint', callback: (area: DrawingArea) => void): number;
    connect(signal: string, callback: (...args: any[]) => any): number;
    get_context(): any; // Cairo.Context
    queue_repaint(): void;
  }

  export class BoxLayout extends Widget {
    constructor(params?: Partial<{
      vertical: boolean;
      style: string;
      style_class: string;
      x_align: Clutter.ActorAlign;
      y_align: Clutter.ActorAlign;
      x_expand: boolean;
      y_expand: boolean;
      reactive: boolean;
    }>);
  }

  export class Label extends Widget {
    constructor(params?: Partial<{
      text: string;
      style: string;
      style_class: string;
      x_align: Clutter.ActorAlign;
      y_align: Clutter.ActorAlign;
    }>);
    text: string;
    set_text(text: string): void;
  }

  export class Button extends Widget {
    constructor(params?: Partial<{
      label: string;
      style: string;
      style_class: string;
      toggle_mode: boolean;
      checked: boolean;
      can_focus: boolean;
      reactive: boolean;
      x_expand: boolean;
      y_expand: boolean;
    }>);
    label: string;
    checked: boolean;
    set_checked(checked: boolean): void;
    connect(signal: 'clicked', callback: (button: Button) => void): number;
    connect(signal: string, callback: (...args: any[]) => any): number;
  }

  export class Entry extends Widget {
    constructor(params?: Partial<{
      style: string;
      style_class: string;
      hint_text: string;
      can_focus: boolean;
      reactive: boolean;
    }>);
    get_text(): string;
    set_text(text: string): void;
    text: string;
    get_clutter_text(): Clutter.Text;
  }
}

declare module 'gi://Clutter' {
  export class Actor {
    add_child(child: Actor): void;
    remove_child(child: Actor): void;
    remove_all_children(): void;
    destroy(): void;
    set_size(width: number, height: number): void;
    set_position(x: number, y: number): void;
    connect(signal: string, callback: (...args: any[]) => any): number;
    disconnect(id: number): void;
    grab_key_focus(): void;
    width: number;
    height: number;
    x: number;
    y: number;
    visible: boolean;
    reactive: boolean;
  }

  export class LayoutManager {}
  export class BinLayout extends LayoutManager {
    constructor();
  }
  export class Text extends Actor {
    connect(signal: string, callback: (...args: any[]) => any): number;
  }

  export enum ActorAlign {
    FILL = 0,
    START = 1,
    CENTER = 2,
    END = 3,
  }

  export enum EventType {
    NOTHING = 0,
    BUTTON_PRESS = 4,
    BUTTON_RELEASE = 5,
    MOTION = 6,
    KEY_PRESS = 8,
    KEY_RELEASE = 9,
  }

  export const EVENT_STOP: boolean;
  export const EVENT_PROPAGATE: boolean;

  // Key symbols
  export const KEY_Escape: number;
  export const KEY_Return: number;
  export const KEY_BackSpace: number;
  export const KEY_z: number;
  export const KEY_Z: number;
  export const KEY_c: number;
  export const KEY_p: number;
  export const KEY_a: number;
  export const KEY_r: number;
  export const KEY_e: number;
  export const KEY_t: number;
  export const KEY_plus: number;
  export const KEY_minus: number;
  export const KEY_equal: number;
  export const KEY_1: number;
  export const KEY_2: number;
  export const KEY_3: number;
  export const KEY_4: number;
  export const KEY_5: number;

  export interface Event {
    get_coords(): [number, number];
    get_key_symbol(): number;
    get_state(): number;
    type(): EventType;
  }

  export enum ModifierType {
    SHIFT_MASK = 1,
    CONTROL_MASK = 4,
    MOD1_MASK = 8,
    SUPER_MASK = 67108864,
  }
}

declare module 'gi://Meta' {
  export enum KeyBindingFlags {
    NONE = 0,
    PER_WINDOW = 1,
    BUILTIN = 2,
    IS_REVERSED = 4,
  }
}

declare module 'gi://Shell' {
  export enum ActionMode {
    NONE = 0,
    NORMAL = 1,
    OVERVIEW = 2,
    LOCK_SCREEN = 4,
    ALL = -1,
  }
}

declare module 'gi://Gio' {
  export class Settings {
    constructor(params: { schema_id: string });
    get_boolean(key: string): boolean;
    set_boolean(key: string, value: boolean): boolean;
    get_double(key: string): number;
    set_double(key: string, value: number): boolean;
    get_string(key: string): string;
    set_string(key: string, value: string): boolean;
    get_strv(key: string): string[];
    set_strv(key: string, value: string[]): boolean;
  }
}

declare module 'gi://GLib' {
  export function get_home_dir(): string;
}

// GNOME Shell internal modules (resource imports)
declare module 'resource:///org/gnome/shell/ui/main.js' {
  export const layoutManager: {
    primaryMonitor: { x: number; y: number; width: number; height: number };
    monitors: { x: number; y: number; width: number; height: number }[];
    addTopChrome(actor: import('gi://Clutter').Actor, params?: any): void;
    removeChrome(actor: import('gi://Clutter').Actor): void;
    uiGroup: import('gi://Clutter').Actor;
  };
  export const wm: {
    addKeybinding(
      name: string,
      settings: import('gi://Gio').Settings,
      flags: number,
      modes: number,
      callback: () => void
    ): void;
    removeKeybinding(name: string): void;
  };
}

declare module 'resource:///org/gnome/shell/extensions/extension.js' {
  export class Extension {
    constructor(metadata: any);
    enable(): void;
    disable(): void;
    getSettings(schema?: string): import('gi://Gio').Settings;
    path: string;
    metadata: any;
    uuid: string;
  }
}

// ── Gtk4 / Adw / Gdk (used by prefs.ts, runs outside GNOME Shell) ──

declare module 'gi://Gtk' {
  export enum Align {
    FILL = 0,
    START = 1,
    END = 2,
    CENTER = 3,
    BASELINE = 4,
  }

  export enum Orientation {
    HORIZONTAL = 0,
    VERTICAL = 1,
  }

  export class Widget {
    add_controller(controller: EventController): void;
    add_css_class(name: string): void;
  }

  export class Box extends Widget {
    constructor(params?: Partial<{
      orientation: Orientation;
      spacing: number;
      margin_top: number;
      margin_bottom: number;
      margin_start: number;
      margin_end: number;
      valign: Align;
      halign: Align;
    }>);
    append(child: Widget): void;
  }

  export class Label extends Widget {
    constructor(params?: Partial<{
      label: string;
      css_classes: string[];
    }>);
    set_text(text: string): void;
  }

  export class Button extends Widget {
    constructor(params?: Partial<{
      label: string;
      icon_name: string;
      valign: Align;
      halign: Align;
      css_classes: string[];
    }>);
    connect(signal: 'clicked', callback: () => void): number;
    connect(signal: string, callback: (...args: any[]) => any): number;
  }

  export class ShortcutLabel extends Widget {
    constructor(params?: Partial<{
      accelerator: string;
      valign: Align;
    }>);
    accelerator: string;
    set_accelerator(accel: string): void;
  }

  export class EventController {}
  export class EventControllerKey extends EventController {
    constructor();
    connect(signal: 'key-pressed', callback: (
      controller: EventControllerKey,
      keyval: number,
      keycode: number,
      state: number,
    ) => boolean): number;
  }

  export function accelerator_name(keyval: number, mods: number): string | null;
}

declare module 'gi://Adw' {
  import type Gtk from 'gi://Gtk';

  export class PreferencesWindow extends Gtk.Widget {
    add(page: PreferencesPage): void;
  }

  export class PreferencesPage extends Gtk.Widget {
    constructor(params?: Partial<{
      title: string;
      icon_name: string;
    }>);
    add(group: PreferencesGroup): void;
  }

  export class PreferencesGroup extends Gtk.Widget {
    constructor(params?: Partial<{
      title: string;
      description: string;
    }>);
    add(row: Gtk.Widget): void;
  }

  export class ActionRow extends Gtk.Widget {
    constructor(params?: Partial<{
      title: string;
      subtitle: string;
    }>);
    add_suffix(widget: Gtk.Widget): void;
    set_activatable_widget(widget: Gtk.Widget): void;
  }

  export class HeaderBar extends Gtk.Widget {
    constructor();
  }

  export class ToolbarView extends Gtk.Widget {
    constructor();
    add_top_bar(bar: Gtk.Widget): void;
    set_content(content: Gtk.Widget): void;
  }

  export class Window extends Gtk.Widget {
    constructor(params?: Partial<{
      modal: boolean;
      transient_for: Gtk.Widget;
      title: string;
      default_width: number;
      default_height: number;
    }>);
    set_content(content: Gtk.Widget): void;
    add_controller(controller: Gtk.EventController): void;
    present(): void;
    close(): void;
  }
}

declare module 'gi://Gdk' {
  export enum ModifierType {
    SHIFT_MASK = 1,
    CONTROL_MASK = 4,
    ALT_MASK = 8,
    SUPER_MASK = 67108864,
  }

  export const KEY_Escape: number;
  export const KEY_BackSpace: number;
  export const KEY_Control_L: number;
  export const KEY_Control_R: number;
  export const KEY_Shift_L: number;
  export const KEY_Shift_R: number;
  export const KEY_Alt_L: number;
  export const KEY_Alt_R: number;
  export const KEY_Super_L: number;
  export const KEY_Super_R: number;
  export const KEY_Meta_L: number;
  export const KEY_Meta_R: number;
}

declare module 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js' {
  export class ExtensionPreferences {
    constructor(metadata: any);
    fillPreferencesWindow(window: import('gi://Adw').PreferencesWindow): void;
    getSettings(schema?: string): import('gi://Gio').Settings;
    path: string;
    metadata: any;
    uuid: string;
  }
}

// Cairo context (passed via St.DrawingArea repaint)
declare namespace CairoContext {
  interface Context {
    setSourceRGBA(r: number, g: number, b: number, a: number): void;
    setSourceRGB(r: number, g: number, b: number): void;
    setLineWidth(width: number): void;
    setLineCap(cap: number): void;
    setLineJoin(join: number): void;
    moveTo(x: number, y: number): void;
    lineTo(x: number, y: number): void;
    rectangle(x: number, y: number, w: number, h: number): void;
    arc(cx: number, cy: number, radius: number, angle1: number, angle2: number): void;
    stroke(): void;
    fill(): void;
    strokePreserve(): void;
    fillPreserve(): void;
    save(): void;
    restore(): void;
    translate(x: number, y: number): void;
    rotate(angle: number): void;
    scale(sx: number, sy: number): void;
    paint(): void;
    closePath(): void;
    newPath(): void;
    clip(): void;
    setOperator(op: number): void;
    selectFontFace(family: string, slant: number, weight: number): void;
    setFontSize(size: number): void;
    showText(text: string): void;
    textExtents(text: string): { width: number; height: number; x_bearing: number; y_bearing: number };
    $dispose(): void;
  }
}

// Cairo constants
declare namespace imports {
  namespace cairo {
    const LINE_CAP_ROUND: number;
    const LINE_JOIN_ROUND: number;
    const OPERATOR_CLEAR: number;
    const OPERATOR_OVER: number;
    const OPERATOR_SOURCE: number;
    const FONT_SLANT_NORMAL: number;
    const FONT_WEIGHT_NORMAL: number;
    const FONT_WEIGHT_BOLD: number;
  }
}
