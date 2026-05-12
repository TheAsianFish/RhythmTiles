// Keyboard input -> lane press/release events with millisecond timestamps.
// Pulls timestamps from performance.now(); the caller pairs them with the
// audio clock (see clock.ts).

export type Lane = 0 | 1 | 2 | 3;

export interface InputEvent {
  lane: Lane;
  kind: "press" | "release";
  perfMs: number;     // performance.now()
}

export interface KeyBindings {
  // Indexed by lane (0..3), values are KeyboardEvent.code strings.
  codes: [string, string, string, string];
}

export class InputCapture {
  private bindings: KeyBindings;
  private listener: ((e: KeyboardEvent) => void) | null = null;
  private upListener: ((e: KeyboardEvent) => void) | null = null;
  private pressed = new Set<Lane>(); // for held-key dedup
  private subscribers: Array<(e: InputEvent) => void> = [];

  constructor(bindings: KeyBindings) {
    this.bindings = bindings;
  }

  setBindings(b: KeyBindings) {
    this.bindings = b;
  }

  subscribe(fn: (e: InputEvent) => void): () => void {
    this.subscribers.push(fn);
    return () => {
      this.subscribers = this.subscribers.filter((s) => s !== fn);
    };
  }

  private laneFor(code: string): Lane | null {
    const i = this.bindings.codes.indexOf(code);
    return i >= 0 ? (i as Lane) : null;
  }

  private emit(e: InputEvent) {
    for (const fn of this.subscribers) fn(e);
  }

  // Inject a key event from outside the DOM (e.g. forwarded by the content
  // script when the parent page has focus). Dedups identically to the DOM
  // path so a key held down only fires a single press.
  injectKey(code: string, kind: "press" | "release", perfMs: number) {
    const lane = this.laneFor(code);
    if (lane === null) return;
    if (kind === "press") {
      if (this.pressed.has(lane)) return;
      this.pressed.add(lane);
      this.emit({ lane, kind: "press", perfMs });
    } else {
      if (!this.pressed.has(lane)) return;
      this.pressed.delete(lane);
      this.emit({ lane, kind: "release", perfMs });
    }
  }

  attach(target: Window | Document = window) {
    if (this.listener) return; // already attached

    this.listener = (ev) => {
      // ev.repeat dedups OS-level key repeat; we also guard with pressed set
      // in case some sources report no repeat flag.
      const lane = this.laneFor(ev.code);
      if (lane === null) return;
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.repeat || this.pressed.has(lane)) return;
      this.pressed.add(lane);
      this.emit({ lane, kind: "press", perfMs: performance.now() });
    };

    this.upListener = (ev) => {
      const lane = this.laneFor(ev.code);
      if (lane === null) return;
      if (!this.pressed.has(lane)) return;
      this.pressed.delete(lane);
      this.emit({ lane, kind: "release", perfMs: performance.now() });
    };

    target.addEventListener("keydown", this.listener as EventListener, { capture: true });
    target.addEventListener("keyup", this.upListener as EventListener, { capture: true });
  }

  detach(target: Window | Document = window) {
    if (this.listener)
      target.removeEventListener("keydown", this.listener as EventListener, { capture: true } as any);
    if (this.upListener)
      target.removeEventListener("keyup", this.upListener as EventListener, { capture: true } as any);
    this.listener = null;
    this.upListener = null;
    this.pressed.clear();
  }
}
