// Component tests for the HelpTip question-mark badge.
//
// HelpTip is a small affordance: a "?" button that toggles a popover with
// explanatory text. It can be driven by click, hover or focus, collapses on
// Escape / focus-out, and (with the disabled prop) refuses to open at all.

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import HelpTip from "../../src/components/HelpTip.svelte";

describe("HelpTip", () => {
  it("renders a badge with the default label and shows text on click", () => {
    render(HelpTip, { text: "This explains the mode." });

    // The badge is a button carrying the label as its accessible name.
    const badge = screen.getByRole("button", { name: "Help" });
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("aria-expanded", "false");

    // The popover is hidden until opened.
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    // fireEvent.click dispatches only the click (no hover sequence), so the
    // badge toggles straight from closed to open.
    fireEvent.click(badge);
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("This explains the mode.");
    expect(badge).toHaveAttribute("aria-expanded", "true");
  });

  it("uses the label prop as the accessible name", () => {
    render(HelpTip, { text: "Hint", label: "About this switch mode" });
    expect(screen.getByRole("button", { name: "About this switch mode" })).toBeInTheDocument();
  });

  it("toggles the tooltip open and closed on repeated clicks", () => {
    render(HelpTip, { text: "Toggle me" });
    const badge = screen.getByRole("button", { name: "Help" });

    fireEvent.click(badge);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Toggle me");

    fireEvent.click(badge);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("opens on hover and collapses on mouse leave", async () => {
    const user = userEvent.setup();
    render(HelpTip, { text: "Hover hint" });
    const badge = screen.getByRole("button", { name: "Help" });

    await user.hover(badge);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Hover hint");

    await user.unhover(badge);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("collapses on Escape", () => {
    render(HelpTip, { text: "Esc me" });
    const badge = screen.getByRole("button", { name: "Help" });

    fireEvent.click(badge);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();

    fireEvent.keyDown(badge, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("respects the disabled state: never opens, and the badge is inert", async () => {
    const user = userEvent.setup();
    render(HelpTip, { text: "Nope", disabled: true });
    const badge = screen.getByRole("button", { name: "Help" });

    expect(badge).toBeDisabled();

    // Click, hover and focus must all fail to surface the tooltip.
    await user.click(badge);
    await user.hover(badge);
    fireEvent.focus(badge);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
