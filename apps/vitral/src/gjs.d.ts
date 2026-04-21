// Minimal GJS type declarations for Vitral extension

declare module 'gi://Meta' {
  export enum KeyBindingFlags { NONE = 0 }
}

declare module 'gi://Shell' {
  export enum ActionMode { ALL = -1 }
}

declare module 'gi://Gio' {
  export class Settings {
    constructor(params: { schema_id: string; path?: string });
    get_string(key: string): string;
    get_double(key: string): number;
    set_double(key: string, value: number): boolean;
    get_strv(key: string): string[];
  }
}

declare module 'resource:///org/gnome/shell/ui/main.js' {
  export const wm: {
    addKeybinding(name: string, settings: import('gi://Gio').Settings, flags: number, modes: number, callback: () => void): void;
    removeKeybinding(name: string): void;
  };
}

declare module 'resource:///org/gnome/shell/extensions/extension.js' {
  export class Extension {
    constructor(metadata: any);
    enable(): void;
    disable(): void;
    getSettings(schema?: string): import('gi://Gio').Settings;
  }
}
