import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import TftLayout from "../../src/components/TftLayout.svelte";
import type { Manifest } from "../../src/lib/protocol";

const commands = vi.hoisted(() => ({
  putGlobal: vi.fn(),
  getGlobal: vi.fn(),
  listFonts: vi.fn(),
}));
vi.mock("../../src/lib/protocol", () => ({ cmd: commands }));

const title = {
  field: "patch_name", halign: "left", valign: "top", x: 0, y: 0,
  size: 5, color: "#ffffff", font: "system",
};
const pedal = {
  field: "expression_mode", halign: "right", valign: "bottom", x: -6, y: -6,
  size: 2, color: "#ffffff", font: "system",
};

function deviceWith(layout: Array<Record<string, unknown>>) {
  return {
    version: 1,
    expression: [{ jack: 1, enabled: true }],
    kemper: { custom_setting: 42 },
    tft: { layout: structuredClone(layout), brightness: 87 },
  };
}

function kemperManifest(): Manifest {
  return {
    core_messages: {},
    plugins: {
      kemper_player: {
        label: "Kemper Player", version: "1", messages: {},
        tft_fields: { expression_mode: { label: "Expression pedal mode (VOL/WAH)", sample: "VOL" } },
        default_layout: [structuredClone(title), structuredClone(pedal)],
      },
    },
  };
}

beforeEach(() => {
  commands.putGlobal.mockReset().mockResolvedValue(undefined);
  commands.getGlobal.mockReset().mockResolvedValue(undefined);
  commands.listFonts.mockReset().mockResolvedValue({ fonts: ["custom.bdf"] });
});

describe("TftLayout expression indicator", () => {
  it("preserves existing user layouts and settings without implicitly adding a badge", async () => {
    const original = deviceWith([title, { text: "User label", x: 12, y: 80, size: 2, color: "#123456" }]);
    const before = structuredClone(original);
    const { container } = render(TftLayout, { device: original, manifest: kemperManifest(), activeKind: "kemper_player" });

    await waitFor(() => expect(container.querySelectorAll(".entry")).toHaveLength(2));
    expect(container.querySelector(".pedalIcon")).toBeNull();
    expect(commands.putGlobal).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledWith(before));
    expect(original).toEqual(before);
  });

  it("adds a bottom-right pedal row explicitly and edits its ordinary position and font", async () => {
    const original = deviceWith([title]);
    const before = structuredClone(original);
    const { container } = render(TftLayout, { device: original, manifest: null });
    await fireEvent.click(screen.getByRole("button", { name: "+ Add pedal indicator" }));

    const row = await waitFor(() => {
      const rows = container.querySelectorAll<HTMLElement>(".entry");
      expect(rows).toHaveLength(2);
      return within(rows[1]);
    });
    expect(row.getByLabelText("Field")).toHaveValue("expression_mode");
    expect(row.getByLabelText("H-align")).toHaveValue("right");
    expect(row.getByLabelText("V-align")).toHaveValue("bottom");
    expect(row.getByLabelText("X offset")).toHaveValue(-6);
    expect(row.getByLabelText("Y offset")).toHaveValue(-6);
    expect(row.getByLabelText("Size")).toHaveValue(2);
    const badge = container.querySelectorAll<HTMLElement>(".prevlabel")[1];
    expect(badge.style.left).toBe("234px");
    expect(badge.style.top).toBe("234px");
    expect(badge.style.height).toBe("28px");
    expect(badge.style.transform).toBe("translate(-100%, -100%)");
    expect(badge.querySelector("svg")).toHaveAttribute("width", "32");
    expect(badge.querySelector("svg")).toHaveAttribute("height", "24");
    await fireEvent.change(row.getByLabelText("H-align"), { target: { value: "left" } });
    await fireEvent.change(row.getByLabelText("V-align"), { target: { value: "top" } });
    await fireEvent.input(row.getByLabelText("X offset"), { target: { value: "17" } });
    await fireEvent.input(row.getByLabelText("Y offset"), { target: { value: "180" } });
    await fireEvent.change(row.getByLabelText("Font"), { target: { value: "custom.bdf" } });
    expect(commands.putGlobal).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledOnce());
    const saved = commands.putGlobal.mock.calls[0][0];
    expect(saved.tft.layout).toEqual([title, { ...pedal, halign: "left", valign: "top", x: 17, y: 180, font: "custom.bdf" }]);
    expect(saved.expression).toEqual(before.expression);
    expect(saved.kemper).toEqual(before.kemper);
    expect(saved.tft.brightness).toBe(87);
    expect(original).toEqual(before);
  });

  it("offers the field with an older manifest and previews VOL, WAH and unknown without saving", async () => {
    const { container } = render(TftLayout, { device: deviceWith([title]), manifest: null });
    const field = await screen.findByLabelText("Field");
    await fireEvent.change(field, { target: { value: "expression_mode" } });
    await waitFor(() => expect(container.querySelectorAll(".pedalIcon")).toHaveLength(1));
    expect(container.querySelector(".preview")?.textContent).toContain("VOL");
    expect(container.querySelector(".pedalIcon")).toHaveAttribute("data-mode", "VOL");
    const volumePath = container.querySelector(".pedalIcon path")?.getAttribute("d");
    const mode = screen.getByLabelText("Pedal preview");
    await fireEvent.change(mode, { target: { value: "WAH" } });
    expect(container.querySelector(".preview")?.textContent).toContain("WAH");
    expect(container.querySelector(".pedalIcon")).toHaveAttribute("data-mode", "WAH");
    expect(container.querySelector(".pedalIcon path")?.getAttribute("d")).not.toBe(volumePath);
    await fireEvent.change(mode, { target: { value: "" } });
    expect(container.querySelector(".preview")?.textContent).toContain("---");
    expect(container.querySelector(".preview")?.textContent).not.toContain("WAH");
    expect(container.querySelector(".pedalIcon")).toHaveStyle("visibility: hidden");
    expect(commands.putGlobal).not.toHaveBeenCalled();
  });

  it("uses plugin defaults only on reset and does not duplicate the expression field option", async () => {
    const original = deviceWith([{ text: "My layout", x: 1, y: 2, size: 1, color: "#fedcba" }]);
    const before = structuredClone(original);
    const { container } = render(TftLayout, { device: original, manifest: kemperManifest(), activeKind: "kemper_player" });
    await fireEvent.click(screen.getByRole("button", { name: "Reset to plugin default" }));
    await waitFor(() => expect(container.querySelectorAll(".entry")).toHaveLength(2));
    for (const field of screen.getAllByLabelText("Field")) {
      expect(field.querySelectorAll('option[value="expression_mode"]')).toHaveLength(1);
    }
    const previewTitle = container.querySelector<HTMLElement>(".prevlabel");
    expect(previewTitle?.textContent).toBe("Heavy");
    expect(previewTitle?.style.maxWidth).toBe("");
    expect(commands.putGlobal).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledOnce());
    expect(commands.putGlobal.mock.calls[0][0].tft.layout).toEqual([title, pedal]);
    expect(original).toEqual(before);
  });

  it("round-trips custom and multiple expression entries without dropping properties", async () => {
    const custom = { ...pedal, x: 11, y: -39, halign: "center", size: 3, color: "#abcdef", font: "custom.bdf", prefix: "EXP ", suffix: "!", future_display_option: 23 };
    const original = deviceWith([title, custom, { ...pedal, color: "#ff0000" }]);
    const { container } = render(TftLayout, { device: original, manifest: null });
    await waitFor(() => expect(container.querySelectorAll(".pedalIcon")).toHaveLength(2));
    expect(container.querySelector(".preview")?.textContent).toContain("EXP VOL!");
    await fireEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledWith(original));
  });

  it("preserves a field unknown to the current manifest and does not replace it with a core field", async () => {
    const original = deviceWith([{ field: "future_plugin_field", x: 3, y: 4, size: 1, color: "#abcdef" }]);
    render(TftLayout, { device: original, manifest: kemperManifest(), activeKind: "kemper_player" });
    expect(await screen.findByLabelText("Field")).toHaveValue("future_plugin_field");
    await fireEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledWith(original));
  });

  it("removes the independent badge without altering the title or other device settings", async () => {
    const original = deviceWith([title, pedal]);
    const { container } = render(TftLayout, { device: original, manifest: null });
    await waitFor(() => expect(container.querySelectorAll(".entry")).toHaveLength(2));
    const row = within(container.querySelectorAll<HTMLElement>(".entry")[1]);
    await fireEvent.click(row.getByTitle("Remove"));
    expect(container.querySelector(".pedalIcon")).toBeNull();
    expect(screen.queryByLabelText("Pedal preview")).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledWith(deviceWith([title])));
    expect(original.tft.layout).toEqual([title, pedal]);
  });
});
