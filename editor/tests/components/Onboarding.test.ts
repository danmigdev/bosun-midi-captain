import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import Onboarding from "../../src/components/Onboarding.svelte";

const created = vi.hoisted(() => ({ createConfiguredProfile: vi.fn(async () => {}) }));
vi.mock("../../src/lib/create-profile", async (original) => ({
  ...await original<typeof import("../../src/lib/create-profile")>(), ...created,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); });

async function openProfileStep() {
  render(Onboarding, { connected: true, hasActiveProfile: false, manifest: null, onClose: vi.fn() });
  await fireEvent.click(screen.getByRole("button", { name: "Get started" }));
  return {
    name: screen.getByPlaceholderText("e.g. Live rig") as HTMLInputElement,
    id: screen.getByPlaceholderText("auto from name") as HTMLInputElement,
  };
}

describe("Onboarding profile form", () => {
  it("derives the id from the whole name as it is typed", async () => {
    const { name, id } = await openProfileStep();
    for (const value of ["L", "Li", "Live", "Live ", "Live r", "Live rig"]) {
      await fireEvent.input(name, { target: { value } });
    }
    expect(id.value).toBe("live-rig");
    await fireEvent.input(name, { target: { value: "Live" } });
    expect(id.value).toBe("live");
  });

  it("keeps an id the user typed and creates the profile with it", async () => {
    const { name, id } = await openProfileStep();
    await fireEvent.input(name, { target: { value: "Live" } });
    await fireEvent.input(id, { target: { value: "stage-a" } });
    await fireEvent.input(name, { target: { value: "Live rig" } });
    expect(id.value).toBe("stage-a");
    await fireEvent.click(screen.getByRole("button", { name: /Create/ }));
    await waitFor(() => expect(created.createConfiguredProfile).toHaveBeenCalled());
    expect(created.createConfiguredProfile).toHaveBeenCalledWith(
      "stage-a", "Live rig", expect.any(String), expect.any(String), expect.anything());
  });
});
