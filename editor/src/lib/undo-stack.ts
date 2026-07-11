/**
 * A generic bounded undo/redo history.
 *
 * Snapshots are deep-cloned with `structuredClone` on the way in
 * (constructor / push / reset) and on the way out (current / undo / redo),
 * so callers can freely mutate what they pass in and what they get back
 * without corrupting the retained history.
 *
 * The history is a linear stack of states with a cursor pointing at the
 * "present". `push` after one or more `undo`s discards the redo tail
 * (standard linear undo). `limit` bounds the total number of retained
 * states; when exceeded, the oldest state is dropped.
 */
export class History<T> {
  private states: T[] = [];
  private index = 0;
  private readonly limit: number;

  constructor(initial: T, limit = 50) {
    this.limit = Math.max(1, Math.floor(limit));
    this.states = [structuredClone(initial)];
    this.index = 0;
  }

  /** The present state (a deep clone the caller may freely mutate). */
  current(): T {
    return structuredClone(this.states[this.index]);
  }

  /**
   * Commit a new state as the present.
   * Truncates any redo tail and enforces the size limit by dropping the
   * oldest retained state(s).
   */
  push(state: T): void {
    // Drop the redo tail (anything after the current cursor).
    this.states.splice(this.index + 1);
    this.states.push(structuredClone(state));
    this.index = this.states.length - 1;

    // Enforce the size limit by dropping the oldest state(s).
    while (this.states.length > this.limit) {
      this.states.shift();
      this.index -= 1;
    }
  }

  /** Move back one state; returns the new current, or null if none. */
  undo(): T | null {
    if (!this.canUndo()) return null;
    this.index -= 1;
    return this.current();
  }

  /** Move forward one state; returns the new current, or null if none. */
  redo(): T | null {
    if (!this.canRedo()) return null;
    this.index += 1;
    return this.current();
  }

  canUndo(): boolean {
    return this.index > 0;
  }

  canRedo(): boolean {
    return this.index < this.states.length - 1;
  }

  /** Clear all history and start fresh at the given state. */
  reset(state: T): void {
    this.states = [structuredClone(state)];
    this.index = 0;
  }
}
