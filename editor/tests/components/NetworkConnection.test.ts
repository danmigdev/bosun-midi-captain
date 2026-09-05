import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import NetworkConnection from "../../src/components/NetworkConnection.svelte";
import type { DiscoveredHub } from "../../src/lib/protocol";

const { discoverHubs } = vi.hoisted(() => ({ discoverHubs: vi.fn() }));
vi.mock("../../src/lib/protocol", () => ({ discoverHubs }));

const studio = { name: "Studio Raspberry Pi", host: "192.168.1.72", tcp_port: 9876 };
const stage = { name: "Stage Raspberry Pi", host: "192.168.1.73", tcp_port: 9000 };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  discoverHubs.mockReset().mockResolvedValue([]);
});

describe("NetworkConnection", () => {
  it("keeps USB as the default and discovers once on each visit to network mode", async () => {
    render(NetworkConnection);
    expect(screen.getByRole("button", { name: "USB", exact: true })).toHaveAttribute("aria-pressed", "true");
    expect(discoverHubs).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("IP address or hostname")).not.toBeInTheDocument();

    await fireEvent.click(screen.getByRole("button", { name: "Raspberry Pi (network)" }));
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledOnce());
    await fireEvent.input(screen.getByLabelText("IP address or hostname"), { target: { value: "bosun-hub.local" } });
    await fireEvent.click(screen.getByRole("button", { name: "Raspberry Pi (network)" }));
    expect(discoverHubs).toHaveBeenCalledOnce();

    await fireEvent.click(screen.getByRole("button", { name: "USB", exact: true }));
    await fireEvent.click(screen.getByRole("button", { name: "Raspberry Pi (network)" }));
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledTimes(2));
    expect(discoverHubs).toHaveBeenLastCalledWith("bosun-hub.local");
    expect(screen.getByLabelText("IP address or hostname")).toHaveValue("bosun-hub.local");
  });

  it("uses the saved address as a hint and selects a discovered hub without connecting", async () => {
    discoverHubs.mockResolvedValue([studio, stage]);
    render(NetworkConnection, { mode: "network", host: "  previous.local  ", port: "1234" });
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledWith("previous.local"));
    const hub = await screen.findByRole("button", { name: "Stage Raspberry Pi 192.168.1.73:9000" });
    expect(screen.getByLabelText("IP address or hostname")).toHaveValue("  previous.local  ");
    await fireEvent.click(hub);
    expect(screen.getByLabelText("IP address or hostname")).toHaveValue(stage.host);
    expect(screen.getByLabelText("Port")).toHaveValue("9000");
    expect(hub).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Studio Raspberry Pi 192.168.1.72:9876" })).toHaveAttribute("aria-pressed", "false");
    expect(hub).toHaveAttribute("type", "button");
  });

  it("allows manual input during discovery and never overwrites it when results arrive", async () => {
    const pending = deferred<DiscoveredHub[]>();
    discoverHubs.mockReturnValue(pending.promise);
    render(NetworkConnection, { mode: "network" });
    expect(await screen.findByRole("button", { name: "Searching…" })).toBeDisabled();
    const host = screen.getByLabelText("IP address or hostname");
    const port = screen.getByLabelText("Port");
    expect(host).toBeEnabled();
    expect(port).toBeEnabled();
    await fireEvent.input(host, { target: { value: "192.168.1.99" } });
    await fireEvent.input(port, { target: { value: "9999" } });
    pending.resolve([studio]);
    await screen.findByRole("button", { name: "Studio Raspberry Pi 192.168.1.72:9876" });
    expect(host).toHaveValue("192.168.1.99");
    expect(port).toHaveValue("9999");
    expect(discoverHubs).toHaveBeenCalledOnce();
  });

  it("keeps manual entry available after no results and allows a discovery retry", async () => {
    render(NetworkConnection, { mode: "network" });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("No Raspberry Pi found. Enter its address manually"));
    expect(screen.getByLabelText("IP address or hostname")).toBeEnabled();
    discoverHubs.mockResolvedValue([studio]);
    await fireEvent.click(screen.getByRole("button", { name: "Find Raspberry Pi" }));
    await screen.findByRole("button", { name: "Studio Raspberry Pi 192.168.1.72:9876" });
    expect(discoverHubs).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toHaveTextContent("1 Raspberry Pi found");
  });

  it("shows search errors with manual fallback, and clears the error on retry", async () => {
    discoverHubs.mockRejectedValue(new Error("Network unavailable"));
    render(NetworkConnection, { mode: "network" });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Search unavailable: Error: Network unavailable"));
    expect(screen.getByRole("status")).toHaveTextContent("Enter the Raspberry Pi address manually");
    expect(screen.getByLabelText("IP address or hostname")).toBeEnabled();
    discoverHubs.mockResolvedValue([studio]);
    await fireEvent.click(screen.getByRole("button", { name: "Find Raspberry Pi" }));
    await screen.findByRole("button", { name: "Studio Raspberry Pi 192.168.1.72:9876" });
    expect(screen.getByRole("status")).not.toHaveTextContent("unavailable");
  });

  it.each(["resolve", "reject"] as const)("ignores a stale search %s after leaving and returning to network mode", async (finish) => {
    const oldSearch = deferred<DiscoveredHub[]>();
    const newSearch = deferred<DiscoveredHub[]>();
    discoverHubs.mockReturnValueOnce(oldSearch.promise).mockReturnValueOnce(newSearch.promise);
    render(NetworkConnection, { mode: "network" });
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledOnce());
    await fireEvent.click(screen.getByRole("button", { name: "USB", exact: true }));
    await fireEvent.click(screen.getByRole("button", { name: "Raspberry Pi (network)" }));
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledTimes(2));
    if (finish === "resolve") oldSearch.resolve([studio]);
    else oldSearch.reject(new Error("Old failure"));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Searching the local network"));
    expect(screen.queryByRole("button", { name: "Studio Raspberry Pi 192.168.1.72:9876" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveTextContent("Old failure");
    newSearch.resolve([stage]);
    await screen.findByRole("button", { name: "Stage Raspberry Pi 192.168.1.73:9000" });
    expect(screen.queryByText("Studio Raspberry Pi")).not.toBeInTheDocument();
  });

  it("does not start discovery while connecting and starts once when available", async () => {
    const view = render(NetworkConnection, { mode: "network", busy: true });
    expect(discoverHubs).not.toHaveBeenCalled();
    for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
    expect(screen.getByLabelText("IP address or hostname")).toBeDisabled();
    expect(screen.getByLabelText("Port")).toBeDisabled();
    await view.rerender({ busy: false });
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledOnce());
    await view.rerender({ busy: true });
    await view.rerender({ busy: false });
    expect(discoverHubs).toHaveBeenCalledOnce();
  });

  it("disables discovered hub selection while connecting", async () => {
    discoverHubs.mockResolvedValue([studio]);
    const view = render(NetworkConnection, { mode: "network" });
    const hub = await screen.findByRole("button", { name: "Studio Raspberry Pi 192.168.1.72:9876" });
    await view.rerender({ busy: true });
    expect(hub).toBeDisabled();
    expect(screen.getByRole("button", { name: "Find Raspberry Pi" })).toBeDisabled();
  });

  it("ignores a reply after unmount without leaking results into a new component", async () => {
    const pending = deferred<DiscoveredHub[]>();
    discoverHubs.mockReturnValueOnce(pending.promise).mockResolvedValueOnce([stage]);
    const oldView = render(NetworkConnection, { mode: "network" });
    await waitFor(() => expect(discoverHubs).toHaveBeenCalledOnce());
    oldView.unmount();
    render(NetworkConnection, { mode: "network" });
    pending.resolve([studio]);
    await screen.findByRole("button", { name: "Stage Raspberry Pi 192.168.1.73:9000" });
    expect(screen.queryByText("Studio Raspberry Pi")).not.toBeInTheDocument();
  });
});
