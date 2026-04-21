// Minimal GJS type declarations for Vitral extension

declare module 'gi://Meta' {
  export class Window {
    get_wm_class(): string | null;
    is_fullscreen(): boolean;
    get_compositor_private(): WindowActor | null;
    connect(signal: 'notify::fullscreen' | 'unmanaged' | string, callback: (win: Window) => void): number;
    disconnect(id: number): void;
  }

  export class WindowActor {
    get_meta_window(): Window;
    opacity: number; // 0–255, compositor-level
  }

  export class Display {
    connect(signal: 'window-created', callback: (display: Display, win: Window) => void): number;
    disconnect(id: number): void;
  }
}

declare module 'gi://GLib' {
  export const PRIORITY_DEFAULT_IDLE: number;
  export const SOURCE_REMOVE: boolean;
  export function idle_add(priority: number, callback: () => boolean): number;
}

declare module 'gi://Gio' {
  export class Settings {
    constructor(params: { schema_id: string; path?: string });
    get_string(key: string): string;
    get_double(key: string): number;
  }
}

declare module 'resource:///org/gnome/shell/extensions/extension.js' {
  export class Extension {
    constructor(metadata: any);
    enable(): void;
    disable(): void;
    path: string;
    metadata: any;
    uuid: string;
  }
}

declare const global: {
  display: import('gi://Meta').Display;
  get_window_actors(): import('gi://Meta').WindowActor[];
};
