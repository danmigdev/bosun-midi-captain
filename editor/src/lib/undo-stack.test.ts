import { describe, it, expect } from "vitest";

import { History } from "./undo-stack";

type Doc = { name: string; nested: { count: number; tags: string[] } };

function makeDoc(name: string, count: number): Doc {
  return { name, nested: { count, tags: [] } };
}

describe("History", () => {
  it("current() equals the constructor value but is a distinct object", () => {
    const initial = makeDoc("a", 1);
    const h = new History<Doc>(initial);
    const c = h.current();
    expect(c).toEqual(initial);
    expect(c).not.toBe(initial);
    expect(c.nested).not.toBe(initial.nested);
  });

  it("push then undo returns the previous state", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    h.push(makeDoc("b", 2));
    expect(h.current().name).toBe("b");
    const back = h.undo();
    expect(back?.name).toBe("a");
    expect(h.current().name).toBe("a");
  });

  it("redo returns the pushed state", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    h.push(makeDoc("b", 2));
    h.undo();
    const fwd = h.redo();
    expect(fwd?.name).toBe("b");
    expect(h.current().name).toBe("b");
  });

  it("push after undo truncates the redo tail", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    h.push(makeDoc("b", 2));
    h.push(makeDoc("c", 3));
    h.undo(); // back to b
    expect(h.canRedo()).toBe(true);
    h.push(makeDoc("d", 4)); // truncates c
    expect(h.canRedo()).toBe(false);
    expect(h.current().name).toBe("d");
    expect(h.undo()?.name).toBe("b");
  });

  it("canUndo/canRedo at both boundaries", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    expect(h.canUndo()).toBe(false);
    expect(h.canRedo()).toBe(false);
    expect(h.undo()).toBeNull();
    expect(h.redo()).toBeNull();

    h.push(makeDoc("b", 2));
    expect(h.canUndo()).toBe(true);
    expect(h.canRedo()).toBe(false);

    h.undo();
    expect(h.canUndo()).toBe(false);
    expect(h.canRedo()).toBe(true);
    expect(h.undo()).toBeNull();
  });

  it("limit drops the oldest states beyond the retained window", () => {
    const h = new History<Doc>(makeDoc("s0", 0), 3);
    h.push(makeDoc("s1", 1));
    h.push(makeDoc("s2", 2));
    h.push(makeDoc("s3", 3)); // window now holds s1, s2, s3 (s0 dropped)

    expect(h.current().name).toBe("s3");
    expect(h.undo()?.name).toBe("s2");
    expect(h.undo()?.name).toBe("s1");
    // s0 was dropped, so we cannot go past the retained window.
    expect(h.canUndo()).toBe(false);
    expect(h.undo()).toBeNull();
  });

  it("reset clears undo and redo", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    h.push(makeDoc("b", 2));
    h.push(makeDoc("c", 3));
    h.undo();

    h.reset(makeDoc("z", 9));
    expect(h.current().name).toBe("z");
    expect(h.canUndo()).toBe(false);
    expect(h.canRedo()).toBe(false);
    expect(h.undo()).toBeNull();
    expect(h.redo()).toBeNull();
  });

  it("deep-clones on the way in so later mutations don't corrupt history", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    const pushed = makeDoc("b", 2);
    h.push(pushed);
    // Mutate the object we handed to push().
    pushed.name = "MUTATED";
    pushed.nested.count = 999;
    pushed.nested.tags.push("x");
    expect(h.current().name).toBe("b");
    expect(h.current().nested.count).toBe(2);
    expect(h.current().nested.tags).toEqual([]);
  });

  it("deep-clones on the way out so mutating a returned object is isolated", () => {
    const h = new History<Doc>(makeDoc("a", 1));
    h.push(makeDoc("b", 2));
    const first = h.current();
    first.name = "MUTATED";
    first.nested.count = 999;
    first.nested.tags.push("y");
    const second = h.current();
    expect(second.name).toBe("b");
    expect(second.nested.count).toBe(2);
    expect(second.nested.tags).toEqual([]);
  });
});
