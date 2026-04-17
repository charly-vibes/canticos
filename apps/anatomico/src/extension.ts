/**
 * Anatómico — "Quiero un zoom anatómico" (Soda Stereo, Zoom, 1995)
 *
 * A minimal ZoomIt clone for GNOME Shell (Wayland-native).
 * Features: freehand pen, arrows, rectangles, ellipses, text overlay,
 *           screen zoom, undo, color/width switching.
 *
 * Activate with Super+Z. Press Escape to exit.
 */

import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import Gio from 'gi://Gio';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

// ── Types ──────────────────────────────────────────────────────────

interface Point {
  x: number;
  y: number;
}

type RGBA = [number, number, number, number]; // each 0–1

type ToolType = 'pen' | 'arrow' | 'rect' | 'ellipse' | 'text';

interface StrokeBase {
  color: RGBA;
  lineWidth: number;
}
interface PenStroke extends StrokeBase {
  tool: 'pen';
  points: Point[];
}
interface ArrowStroke extends StrokeBase {
  tool: 'arrow';
  start: Point;
  end: Point;
}
interface RectStroke extends StrokeBase {
  tool: 'rect';
  start: Point;
  end: Point;
}
interface EllipseStroke extends StrokeBase {
  tool: 'ellipse';
  start: Point;
  end: Point;
}
interface TextStroke extends StrokeBase {
  tool: 'text';
  position: Point;
  text: string;
  fontSize: number;
}

type Stroke = PenStroke | ArrowStroke | RectStroke | EllipseStroke | TextStroke;

// ── Cairo constants (available globally in GJS) ────────────────────

const CAIRO_LINE_CAP_ROUND = 1;
const CAIRO_LINE_JOIN_ROUND = 1;
const CAIRO_OPERATOR_CLEAR = 0;
const CAIRO_OPERATOR_OVER = 2;
const CAIRO_FONT_SLANT_NORMAL = 0;
const CAIRO_FONT_WEIGHT_BOLD = 1;

// ── Palette ────────────────────────────────────────────────────────

const COLORS: { name: string; rgba: RGBA }[] = [
  { name: 'Red',    rgba: [0.9, 0.1, 0.1, 1] },
  { name: 'Blue',   rgba: [0.15, 0.35, 0.9, 1] },
  { name: 'Green',  rgba: [0.1, 0.75, 0.2, 1] },
  { name: 'Yellow', rgba: [1, 0.85, 0, 1] },
  { name: 'White',  rgba: [1, 1, 1, 1] },
];

const TOOLS: { type: ToolType; label: string; key: string }[] = [
  { type: 'pen',     label: 'Pen',     key: 'P' },
  { type: 'arrow',   label: 'Arrow',   key: 'A' },
  { type: 'rect',    label: 'Rect',    key: 'R' },
  { type: 'ellipse', label: 'Ellipse', key: 'E' },
  { type: 'text',    label: 'Text',    key: 'T' },
];

const LINE_WIDTHS = [2, 4, 7, 12];

// ── Keybinding settings (schema embedded via build) ────────────────

const KEYBINDING_KEY = 'toggle-anatomico';

// ── Extension ──────────────────────────────────────────────────────

export default class Anatomico extends Extension {
  private _overlay: St.Widget | null = null;
  private _canvas: St.DrawingArea | null = null;
  private _toolbar: St.BoxLayout | null = null;
  private _toolButtons: Map<ToolType, St.Button> = new Map();
  private _colorButtons: Map<number, St.Button> = new Map();
  private _statusLabel: St.Label | null = null;
  private _textEntry: St.Entry | null = null;

  private _strokes: Stroke[] = [];
  private _currentStroke: Stroke | null = null;
  private _currentTool: ToolType = 'pen';
  private _currentColor: RGBA = [...COLORS[0].rgba] as RGBA;
  private _currentLineWidth: number = LINE_WIDTHS[1];
  private _colorIndex = 0;
  private _widthIndex = 1;
  private _isActive = false;
  private _isDrawing = false;

  // Zoom state
  private _zoomSettings: Gio.Settings | null = null;
  private _a11ySettings: Gio.Settings | null = null;
  private _zoomLevel = 1.0;

  // Signal IDs for cleanup
  private _signalIds: number[] = [];
  private _settings: Gio.Settings | null = null;

  enable(): void {
    this._settings = this.getSettings();
    Main.wm.addKeybinding(
      KEYBINDING_KEY,
      this._settings,
      Meta.KeyBindingFlags.NONE,
      Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
      () => this._toggle()
    );
  }

  disable(): void {
    this._deactivate();
    Main.wm.removeKeybinding(KEYBINDING_KEY);
    this._settings = null;
  }

  // ── Activate / Deactivate ──────────────────────────────────────

  private _toggle(): void {
    if (this._isActive) {
      this._deactivate();
    } else {
      this._activate();
    }
  }

  private _activate(): void {
    if (this._isActive) return;
    this._isActive = true;
    this._strokes = [];
    this._currentStroke = null;

    const monitor = Main.layoutManager.primaryMonitor;

    // Fullscreen transparent overlay
    this._overlay = new St.Widget({
      reactive: true,
      can_focus: true,
      x: monitor.x,
      y: monitor.y,
      width: monitor.width,
      height: monitor.height,
      style: 'background-color: rgba(0,0,0,0);',
    });

    // Drawing canvas (non-reactive — overlay handles all input)
    this._canvas = new St.DrawingArea({
      reactive: false,
      can_focus: false,
      width: monitor.width,
      height: monitor.height,
      x_expand: true,
      y_expand: true,
    });
    this._overlay.add_child(this._canvas);

    // Canvas: repaint only
    const repaintId = this._canvas.connect('repaint', (area: St.DrawingArea) => {
      this._onRepaint(area);
    });
    this._signalIds.push(repaintId);
    this._canvas.queue_repaint(); // Clear to transparent immediately

    // Input events on the overlay (more reliable than St.DrawingArea on GNOME 49)
    const pressId = this._overlay.connect('button-press-event',
      (_actor: any, event: Clutter.Event) => this._onButtonPress(event));
    this._signalIds.push(pressId);

    const releaseId = this._overlay.connect('button-release-event',
      (_actor: any, event: Clutter.Event) => this._onButtonRelease(event));
    this._signalIds.push(releaseId);

    const motionId = this._overlay.connect('motion-event',
      (_actor: any, event: Clutter.Event) => this._onMotion(event));
    this._signalIds.push(motionId);

    const keyId = this._overlay.connect('key-press-event',
      (_actor: any, event: Clutter.Event) => this._onKeyPress(event));
    this._signalIds.push(keyId);

    // Build toolbar
    this._buildToolbar(monitor.width);
    this._overlay.add_child(this._toolbar!);

    // Add overlay above everything
    Main.layoutManager.addTopChrome(this._overlay);
    this._overlay.grab_key_focus();

    // Init zoom settings
    try {
      this._a11ySettings = new Gio.Settings({ schema_id: 'org.gnome.desktop.a11y.applications' });
      this._zoomSettings = new Gio.Settings({ schema_id: 'org.gnome.desktop.a11y.magnifier' });
    } catch {
      // Zoom settings unavailable — not critical
    }
  }

  private _deactivate(): void {
    if (!this._isActive) return;
    this._isActive = false;

    // Reset zoom if it was changed
    this._resetZoom();

    // Disconnect signals
    for (const id of this._signalIds) {
      try { this._overlay?.disconnect(id); } catch {}
      try { this._canvas?.disconnect(id); } catch {}
    }
    this._signalIds = [];

    // Destroy overlay (destroys all children too)
    if (this._overlay) {
      Main.layoutManager.removeChrome(this._overlay);
      this._overlay.destroy();
    }

    this._overlay = null;
    this._canvas = null;
    this._toolbar = null;
    this._textEntry = null;
    this._toolButtons.clear();
    this._colorButtons.clear();
    this._statusLabel = null;
    this._strokes = [];
    this._currentStroke = null;
    this._zoomSettings = null;
    this._a11ySettings = null;
    this._zoomLevel = 1.0;
  }

  // ── Toolbar ────────────────────────────────────────────────────

  private _buildToolbar(monitorWidth: number): void {
    this._toolbar = new St.BoxLayout({
      vertical: false,
      style: `
        background-color: rgba(30, 30, 30, 0.92);
        border-radius: 8px;
        padding: 4px 10px;
        spacing: 6px;
      `,
      x_align: Clutter.ActorAlign.CENTER,
      y_align: Clutter.ActorAlign.START,
      reactive: true,
    });
    this._toolbar.set_position(monitorWidth / 2 - 350, 6);

    // Tool buttons
    for (const tool of TOOLS) {
      const btn = new St.Button({
        label: `${tool.label} (${tool.key})`,
        style: this._toolBtnStyle(tool.type === this._currentTool),
        reactive: true,
        can_focus: false,
      });
      btn.connect('clicked', () => this._selectTool(tool.type));
      this._toolbar.add_child(btn);
      this._toolButtons.set(tool.type, btn);
    }

    // Separator
    this._toolbar.add_child(new St.Label({
      text: '│',
      style: 'color: rgba(255,255,255,0.3); font-size: 14px; padding: 0 2px;',
    }));

    // Color buttons
    for (let i = 0; i < COLORS.length; i++) {
      const c = COLORS[i];
      const cssColor = `rgba(${c.rgba[0] * 255},${c.rgba[1] * 255},${c.rgba[2] * 255},1)`;
      const btn = new St.Button({
        label: ' ',
        style: `
          background-color: ${cssColor};
          min-width: 22px; min-height: 22px;
          border-radius: 11px;
          border: 2px solid ${i === this._colorIndex ? 'white' : 'rgba(255,255,255,0.2)'};
          margin: 2px;
        `,
        reactive: true,
        can_focus: false,
      });
      const idx = i;
      btn.connect('clicked', () => this._selectColor(idx));
      this._toolbar.add_child(btn);
      this._colorButtons.set(i, btn);
    }

    // Separator
    this._toolbar.add_child(new St.Label({
      text: '│',
      style: 'color: rgba(255,255,255,0.3); font-size: 14px; padding: 0 2px;',
    }));

    // Width button
    const widthBtn = new St.Button({
      label: `W: ${this._currentLineWidth}`,
      style: this._actionBtnStyle(),
      reactive: true,
      can_focus: false,
    });
    widthBtn.connect('clicked', () => {
      this._widthIndex = (this._widthIndex + 1) % LINE_WIDTHS.length;
      this._currentLineWidth = LINE_WIDTHS[this._widthIndex];
      widthBtn.label = `W: ${this._currentLineWidth}`;
    });
    this._toolbar.add_child(widthBtn);

    // Zoom buttons
    const zoomInBtn = new St.Button({
      label: 'Zoom+',
      style: this._actionBtnStyle(),
      reactive: true,
      can_focus: false,
    });
    zoomInBtn.connect('clicked', () => this._zoomIn());
    this._toolbar.add_child(zoomInBtn);

    const zoomOutBtn = new St.Button({
      label: 'Zoom−',
      style: this._actionBtnStyle(),
      reactive: true,
      can_focus: false,
    });
    zoomOutBtn.connect('clicked', () => this._zoomOut());
    this._toolbar.add_child(zoomOutBtn);

    // Undo
    const undoBtn = new St.Button({
      label: 'Undo',
      style: this._actionBtnStyle(),
      reactive: true,
      can_focus: false,
    });
    undoBtn.connect('clicked', () => this._undo());
    this._toolbar.add_child(undoBtn);

    // Clear
    const clearBtn = new St.Button({
      label: 'Clear',
      style: this._actionBtnStyle(),
      reactive: true,
      can_focus: false,
    });
    clearBtn.connect('clicked', () => this._clearAll());
    this._toolbar.add_child(clearBtn);

    // Status label
    this._statusLabel = new St.Label({
      text: 'Esc to close',
      style: 'color: rgba(255,255,255,0.5); font-size: 11px; padding: 4px 6px;',
    });
    this._toolbar.add_child(this._statusLabel);
  }

  private _toolBtnStyle(active: boolean): string {
    return `
      color: ${active ? '#fff' : 'rgba(255,255,255,0.6)'};
      background-color: ${active ? 'rgba(80,130,255,0.7)' : 'transparent'};
      border-radius: 4px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: ${active ? 'bold' : 'normal'};
    `;
  }

  private _actionBtnStyle(): string {
    return `
      color: rgba(255,255,255,0.8);
      background-color: rgba(255,255,255,0.1);
      border-radius: 4px;
      padding: 3px 8px;
      font-size: 12px;
    `;
  }

  // ── Tool / Color Selection ─────────────────────────────────────

  private _selectTool(tool: ToolType): void {
    this._currentTool = tool;
    for (const [t, btn] of this._toolButtons) {
      btn.style = this._toolBtnStyle(t === tool);
    }
    // Dismiss text entry if switching away from text
    if (tool !== 'text' && this._textEntry) {
      this._finishTextEntry();
    }
    this._overlay?.grab_key_focus();
  }

  private _selectColor(index: number): void {
    this._colorIndex = index;
    this._currentColor = [...COLORS[index].rgba] as RGBA;
    for (const [i, btn] of this._colorButtons) {
      const c = COLORS[i];
      const cssColor = `rgba(${c.rgba[0] * 255},${c.rgba[1] * 255},${c.rgba[2] * 255},1)`;
      btn.style = `
        background-color: ${cssColor};
        min-width: 22px; min-height: 22px;
        border-radius: 11px;
        border: 2px solid ${i === index ? 'white' : 'rgba(255,255,255,0.2)'};
        margin: 2px;
      `;
    }
    this._overlay?.grab_key_focus();
  }

  // ── Input Handling ─────────────────────────────────────────────

  private _onButtonPress(event: Clutter.Event): boolean {
    const [x, y] = event.get_coords();

    // If text tool, place entry
    if (this._currentTool === 'text') {
      this._startTextEntry(x, y);
      return Clutter.EVENT_STOP;
    }

    this._isDrawing = true;

    switch (this._currentTool) {
      case 'pen':
        this._currentStroke = {
          tool: 'pen',
          points: [{ x, y }],
          color: [...this._currentColor] as RGBA,
          lineWidth: this._currentLineWidth,
        };
        break;
      case 'arrow':
        this._currentStroke = {
          tool: 'arrow',
          start: { x, y },
          end: { x, y },
          color: [...this._currentColor] as RGBA,
          lineWidth: this._currentLineWidth,
        };
        break;
      case 'rect':
        this._currentStroke = {
          tool: 'rect',
          start: { x, y },
          end: { x, y },
          color: [...this._currentColor] as RGBA,
          lineWidth: this._currentLineWidth,
        };
        break;
      case 'ellipse':
        this._currentStroke = {
          tool: 'ellipse',
          start: { x, y },
          end: { x, y },
          color: [...this._currentColor] as RGBA,
          lineWidth: this._currentLineWidth,
        };
        break;
    }

    return Clutter.EVENT_STOP;
  }

  private _onMotion(event: Clutter.Event): boolean {
    if (!this._isDrawing || !this._currentStroke) return Clutter.EVENT_PROPAGATE;

    const [x, y] = event.get_coords();

    switch (this._currentStroke.tool) {
      case 'pen':
        (this._currentStroke as PenStroke).points.push({ x, y });
        break;
      case 'arrow':
        (this._currentStroke as ArrowStroke).end = { x, y };
        break;
      case 'rect':
        (this._currentStroke as RectStroke).end = { x, y };
        break;
      case 'ellipse':
        (this._currentStroke as EllipseStroke).end = { x, y };
        break;
    }

    this._canvas?.queue_repaint();
    return Clutter.EVENT_STOP;
  }

  private _onButtonRelease(_event: Clutter.Event): boolean {
    if (!this._isDrawing || !this._currentStroke) return Clutter.EVENT_PROPAGATE;

    this._isDrawing = false;

    // Only add strokes that have meaningful content
    if (this._currentStroke.tool === 'pen') {
      if ((this._currentStroke as PenStroke).points.length > 1) {
        this._strokes.push(this._currentStroke);
      }
    } else {
      this._strokes.push(this._currentStroke);
    }

    this._currentStroke = null;
    this._canvas?.queue_repaint();
    return Clutter.EVENT_STOP;
  }

  private _onKeyPress(event: Clutter.Event): boolean {
    const sym = event.get_key_symbol();
    const state = event.get_state();
    const ctrl = (state & Clutter.ModifierType.CONTROL_MASK) !== 0;

    // If text entry is active, let it handle keys (except Escape)
    if (this._textEntry && sym !== Clutter.KEY_Escape) {
      return Clutter.EVENT_PROPAGATE;
    }

    // Escape — close overlay
    if (sym === Clutter.KEY_Escape) {
      if (this._textEntry) {
        this._finishTextEntry();
      } else {
        this._deactivate();
      }
      return Clutter.EVENT_STOP;
    }

    // Ctrl+Z — undo
    if (ctrl && (sym === Clutter.KEY_z || sym === Clutter.KEY_Z)) {
      this._undo();
      return Clutter.EVENT_STOP;
    }

    // Tool shortcuts
    if (sym === Clutter.KEY_p) { this._selectTool('pen');     return Clutter.EVENT_STOP; }
    if (sym === Clutter.KEY_a) { this._selectTool('arrow');   return Clutter.EVENT_STOP; }
    if (sym === Clutter.KEY_r) { this._selectTool('rect');    return Clutter.EVENT_STOP; }
    if (sym === Clutter.KEY_e) { this._selectTool('ellipse'); return Clutter.EVENT_STOP; }
    if (sym === Clutter.KEY_t) { this._selectTool('text');    return Clutter.EVENT_STOP; }

    // Clear
    if (sym === Clutter.KEY_c && !ctrl) { this._clearAll(); return Clutter.EVENT_STOP; }

    // Color shortcuts: 1-5
    if (sym >= Clutter.KEY_1 && sym <= Clutter.KEY_5) {
      this._selectColor(sym - Clutter.KEY_1);
      return Clutter.EVENT_STOP;
    }

    // Zoom
    if (sym === Clutter.KEY_plus || sym === Clutter.KEY_equal) {
      this._zoomIn();
      return Clutter.EVENT_STOP;
    }
    if (sym === Clutter.KEY_minus) {
      this._zoomOut();
      return Clutter.EVENT_STOP;
    }

    return Clutter.EVENT_PROPAGATE;
  }

  // ── Text Entry ─────────────────────────────────────────────────

  private _startTextEntry(x: number, y: number): void {
    // Remove existing entry if any
    if (this._textEntry) {
      this._finishTextEntry();
    }

    const [cr, cg, cb] = this._currentColor;
    const cssColor = `rgb(${cr * 255}, ${cg * 255}, ${cb * 255})`;
    const fontSize = Math.max(16, this._currentLineWidth * 5);

    this._textEntry = new St.Entry({
      style: `
        color: ${cssColor};
        font-size: ${fontSize}px;
        font-weight: bold;
        background-color: rgba(0,0,0,0.3);
        border: 1px solid rgba(255,255,255,0.3);
        border-radius: 3px;
        padding: 2px 6px;
        min-width: 100px;
        caret-color: white;
      `,
      hint_text: 'Type and press Enter…',
      can_focus: true,
      reactive: true,
    });

    this._textEntry.set_position(x, y);
    this._overlay?.add_child(this._textEntry);
    this._textEntry.grab_key_focus();

    // Commit on Enter
    const clutterText = this._textEntry.get_clutter_text();
    clutterText.connect('activate', () => {
      this._finishTextEntry();
    });
  }

  private _finishTextEntry(): void {
    if (!this._textEntry) return;

    const text = this._textEntry.get_text().trim();
    if (text.length > 0) {
      const fontSize = Math.max(16, this._currentLineWidth * 5);
      const stroke: TextStroke = {
        tool: 'text',
        position: { x: this._textEntry.x, y: this._textEntry.y + fontSize },
        text,
        color: [...this._currentColor] as RGBA,
        lineWidth: this._currentLineWidth,
        fontSize,
      };
      this._strokes.push(stroke);
    }

    this._textEntry.destroy();
    this._textEntry = null;
    this._canvas?.queue_repaint();
    this._overlay?.grab_key_focus();
  }

  // ── Actions ────────────────────────────────────────────────────

  private _undo(): void {
    if (this._strokes.length > 0) {
      this._strokes.pop();
      this._canvas?.queue_repaint();
    }
  }

  private _clearAll(): void {
    this._strokes = [];
    this._currentStroke = null;
    this._canvas?.queue_repaint();
  }

  // ── Zoom (GNOME built-in magnifier) ────────────────────────────

  private _zoomIn(): void {
    if (!this._zoomSettings || !this._a11ySettings) return;
    this._zoomLevel = Math.min(this._zoomLevel + 0.5, 8.0);
    this._applyZoom();
  }

  private _zoomOut(): void {
    if (!this._zoomSettings || !this._a11ySettings) return;
    this._zoomLevel = Math.max(this._zoomLevel - 0.5, 1.0);
    this._applyZoom();
  }

  private _applyZoom(): void {
    if (!this._zoomSettings || !this._a11ySettings) return;
    if (this._zoomLevel > 1.0) {
      this._a11ySettings.set_boolean('screen-magnifier-enabled', true);
      this._zoomSettings.set_double('mag-factor', this._zoomLevel);
    } else {
      this._a11ySettings.set_boolean('screen-magnifier-enabled', false);
    }
    if (this._statusLabel) {
      this._statusLabel.set_text(
        this._zoomLevel > 1 ? `Zoom: ${this._zoomLevel.toFixed(1)}×` : 'Esc to close'
      );
    }
  }

  private _resetZoom(): void {
    if (this._a11ySettings) {
      try {
        this._a11ySettings.set_boolean('screen-magnifier-enabled', false);
      } catch {}
    }
    this._zoomLevel = 1.0;
  }

  // ── Cairo Drawing ──────────────────────────────────────────────

  private _onRepaint(area: St.DrawingArea): void {
    const cr = area.get_context() as unknown as CairoContext.Context;

    // Clear canvas to fully transparent
    cr.setOperator(CAIRO_OPERATOR_CLEAR);
    cr.paint();
    cr.setOperator(CAIRO_OPERATOR_OVER);

    // Draw all committed strokes
    for (const stroke of this._strokes) {
      this._drawStroke(cr, stroke);
    }

    // Draw in-progress stroke
    if (this._currentStroke) {
      this._drawStroke(cr, this._currentStroke);
    }

    // Note: do not call cr.$dispose() — GJS GC handles it, and
    // explicit disposal can corrupt the DrawingArea on GNOME 49.
  }

  private _drawStroke(cr: CairoContext.Context, stroke: Stroke): void {
    const [r, g, b, a] = stroke.color;
    cr.setSourceRGBA(r, g, b, a);
    cr.setLineWidth(stroke.lineWidth);
    cr.setLineCap(CAIRO_LINE_CAP_ROUND);
    cr.setLineJoin(CAIRO_LINE_JOIN_ROUND);

    switch (stroke.tool) {
      case 'pen':
        this._drawPen(cr, stroke);
        break;
      case 'arrow':
        this._drawArrow(cr, stroke);
        break;
      case 'rect':
        this._drawRect(cr, stroke);
        break;
      case 'ellipse':
        this._drawEllipse(cr, stroke);
        break;
      case 'text':
        this._drawText(cr, stroke);
        break;
    }
  }

  private _drawPen(cr: CairoContext.Context, stroke: PenStroke): void {
    const pts = stroke.points;
    if (pts.length < 2) return;

    cr.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) {
      cr.lineTo(pts[i].x, pts[i].y);
    }
    cr.stroke();
  }

  private _drawArrow(cr: CairoContext.Context, stroke: ArrowStroke): void {
    const { start, end } = stroke;
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len < 1) return;

    // Shaft
    cr.moveTo(start.x, start.y);
    cr.lineTo(end.x, end.y);
    cr.stroke();

    // Arrowhead
    const headLen = Math.min(20, len * 0.3);
    const headAngle = Math.PI / 7;
    const angle = Math.atan2(dy, dx);

    cr.moveTo(end.x, end.y);
    cr.lineTo(
      end.x - headLen * Math.cos(angle - headAngle),
      end.y - headLen * Math.sin(angle - headAngle)
    );
    cr.moveTo(end.x, end.y);
    cr.lineTo(
      end.x - headLen * Math.cos(angle + headAngle),
      end.y - headLen * Math.sin(angle + headAngle)
    );
    cr.stroke();
  }

  private _drawRect(cr: CairoContext.Context, stroke: RectStroke): void {
    const x = Math.min(stroke.start.x, stroke.end.x);
    const y = Math.min(stroke.start.y, stroke.end.y);
    const w = Math.abs(stroke.end.x - stroke.start.x);
    const h = Math.abs(stroke.end.y - stroke.start.y);
    cr.rectangle(x, y, w, h);
    cr.stroke();
  }

  private _drawEllipse(cr: CairoContext.Context, stroke: EllipseStroke): void {
    const cx = (stroke.start.x + stroke.end.x) / 2;
    const cy = (stroke.start.y + stroke.end.y) / 2;
    const rx = Math.abs(stroke.end.x - stroke.start.x) / 2;
    const ry = Math.abs(stroke.end.y - stroke.start.y) / 2;
    if (rx < 1 || ry < 1) return;

    cr.save();
    cr.translate(cx, cy);
    cr.scale(rx, ry);
    cr.arc(0, 0, 1, 0, 2 * Math.PI);
    cr.restore();
    cr.stroke();
  }

  private _drawText(cr: CairoContext.Context, stroke: TextStroke): void {
    cr.selectFontFace('Sans', CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    cr.setFontSize(stroke.fontSize);
    cr.moveTo(stroke.position.x, stroke.position.y);
    cr.showText(stroke.text);
  }
}
