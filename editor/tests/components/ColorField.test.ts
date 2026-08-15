// Component tests for the ColorField swatch row + native picker.

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import ColorField from "../../src/components/ColorField.svelte";

// The ten base colors, in render order (mirrors BASE_COLORS in the component).
const BASE_COLORS = [
  "#ffffff", "#ff0000", "#ff7f00", "#ffff00", "#00ff00",
  "#00ffff", "#0000ff", "#ff00ff", "#8000ff", "#000000",
];

describe("ColorField", () => {
  it("renders all 10 color swatches plus the native picker", () => {
    render(ColorField);

    const swatches = screen.getAllByRole("button");
    expect(swatches).toHaveLength(10);
    // Each swatch carries its hex as the accessible name, in palette order.
    expect(swatches.map((s) => s.getAttribute("aria-label"))).toEqual(BASE_COLORS);
    // All swatches sit inside a labelled group.
    const group = screen.getByRole("group", { name: "Base colors" });
    expect(group).toBeInTheDocument();
  });

  it("shows the native OS picker button", () => {
    render(ColorField);
    const picker = screen.getByLabelText("Custom color") as HTMLInputElement;
    expect(picker).toBeInTheDocument();
    expect(picker.type).toBe("color");
  });

  it("marks the current value's swatch as pressed", () => {
    render(ColorField, { value: "#ff0000" });
    expect(screen.getByRole("button", { name: "#ff0000" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "#ffffff" })).toHaveAttribute("aria-pressed", "false");
  });

  it("emits the correct color when a swatch is clicked", async () => {
    const user = userEvent.setup();
    const onchange = vi.fn();
    render(ColorField, { value: "#ffffff", onchange });

    await user.click(screen.getByRole("button", { name: "#00ff00" }));
    expect(onchange).toHaveBeenCalledTimes(1);
    expect(onchange).toHaveBeenCalledWith("#00ff00");

    await user.click(screen.getByRole("button", { name: "#000000" }));
    expect(onchange).toHaveBeenCalledTimes(2);
    expect(onchange).toHaveBeenCalledWith("#000000");
  });

  it("emits the color picked from the native input", () => {
    const onchange = vi.fn();
    render(ColorField, { onchange });

    const picker = screen.getByLabelText("Custom color") as HTMLInputElement;
    // A real picker writes the whole hex at once; fire the input event the
    // component listens to (userEvent typing is not applicable to color inputs).
    fireEvent.input(picker, { target: { value: "#123456" } });
    expect(onchange).toHaveBeenCalledWith("#123456");
  });
});
