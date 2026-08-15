// Component tests for the schematic PedalMap (10 footswitches, 2 rows of 5).

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import PedalMap from "../../src/components/PedalMap.svelte";
import type { Binding } from "../../src/lib/protocol";

/** The ten switches in DOM order (matches DEFAULT_LAYOUT row-major). */
const SWITCH_ORDER = ["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"];

/** Default LED color per switch (mirrors switch-colors.ts). */
const DEFAULT_LED: Record<string, string> = {
  "1": "#3a8eff", "2": "#f5dc34", "3": "#e54848", "4": "#3ecb6e",
  up: "#00bcd4", down: "#c08aff",
  A: "#ff8a00", B: "#00e5ff", C: "#ff4081", D: "#76ff03",
};

function binding(sw: string, overrides: Partial<Binding> = {}): Binding {
  return { switch: sw, mode: "tap", actions: {}, ...overrides };
}

function ledsOf(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll(".led")) as HTMLElement[];
}

/** The --led CSS custom property jsdom parses out of the style. */
function ledColorOf(el: HTMLElement): string {
  return el.style.getPropertyValue("--led");
}

describe("PedalMap", () => {
  it("renders the 10 footswitches in the schematic layout", () => {
    const { container } = render(PedalMap, { bindings: [] });

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(10);
    // Unbound switches read as "Switch X (empty)".
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual(
      SWITCH_ORDER.map((sw) => `Switch ${sw} (empty)`),
    );
    // One LED dot per switch, in the same order.
    const leds = ledsOf(container);
    expect(leds).toHaveLength(10);
    expect(screen.getByRole("group", { name: "Pedal switch map" })).toBeInTheDocument();
  });

  it("shows the binding label on bound switches", () => {
    render(PedalMap, {
      bindings: [binding("1", { label: "Lead" }), binding("A", { label: "Delay" })],
    });
    expect(screen.getByRole("button", { name: "Switch 1 - Lead" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch A - Delay" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch 2 (empty)" })).toBeInTheDocument();
  });

  it("emits the switch name when a stomp is clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(PedalMap, { bindings: [], onSelect });

    await user.click(screen.getByRole("button", { name: "Switch up (empty)" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("up");

    await user.click(screen.getByRole("button", { name: "Switch D (empty)" }));
    expect(onSelect).toHaveBeenCalledWith("D");
  });

  it("marks the selected switch", () => {
    render(PedalMap, { bindings: [], selected: "B" });
    expect(screen.getByRole("button", { name: "Switch B (empty)" })).toHaveAttribute(
      "aria-pressed", "true",
    );
    expect(screen.getByRole("button", { name: "Switch A (empty)" })).toHaveAttribute(
      "aria-pressed", "false",
    );
  });

  it("uses the per-switch default LED color when nothing is bound", () => {
    const { container } = render(PedalMap, { bindings: [] });
    const leds = ledsOf(container);
    leds.forEach((led, i) => {
      expect(ledColorOf(led)).toBe(DEFAULT_LED[SWITCH_ORDER[i]]);
    });
  });

  it("uses the binding's led.on color when set", () => {
    const { container } = render(PedalMap, {
      bindings: [
        binding("1", { led: { on: "#ff0000" } }),
        binding("A", { led: { on: "#00ff00" } }),
      ],
    });
    const leds = ledsOf(container);
    expect(ledColorOf(leds[0])).toBe("#ff0000"); // switch 1
    expect(ledColorOf(leds[5])).toBe("#00ff00"); // switch A
    // Unbound switches still fall back to their default.
    expect(ledColorOf(leds[1])).toBe(DEFAULT_LED["2"]);
  });

  it("prefers the colorFor override over bindings and defaults", () => {
    const { container } = render(PedalMap, {
      bindings: [binding("1", { led: { on: "#ff0000" } })],
      colorFor: (sw) => (sw === "1" ? "#123456" : "#000000"),
    });
    const leds = ledsOf(container);
    expect(ledColorOf(leds[0])).toBe("#123456");
    expect(ledColorOf(leds[1])).toBe("#000000");
  });

  it("routes pointer down/up to onPress/onRelease for the simulator", () => {
    const onPress = vi.fn();
    const onRelease = vi.fn();
    render(PedalMap, { bindings: [], onPress, onRelease });

    const stomp = screen.getByRole("button", { name: "Switch 3 (empty)" });
    fireEvent.pointerDown(stomp);
    expect(onPress).toHaveBeenCalledWith("3");
    fireEvent.pointerUp(stomp);
    expect(onRelease).toHaveBeenCalledWith("3");
  });
});
