import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import App from "../src/App.svelte";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  detectPedal: vi.fn(),
  lifecycle: vi.fn(),
  onFirmwareMessage: vi.fn(),
  onDisconnected: vi.fn(),
  readNetworkBootstrap: vi.fn(),
  cmd: {
    getDeviceInfo: vi.fn(), getManifest: vi.fn(), getManifestAwait: vi.fn(),
    listProfiles: vi.fn(), listPatches: vi.fn(), getDirty: vi.fn(),
    getMidiLearn: vi.fn(), getGlobal: vi.fn(), getStats: vi.fn(),
  },
}));

// Keep the actual protocol transport functions so the assertions cover the
// App -> tcpConnect -> Tauri command path. Only firmware data and subscriptions
// are stubbed; this suite renders App and NetworkConnection themselves.
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("../src/lib/network-bootstrap", () => ({ readNetworkBootstrap: mocks.readNetworkBootstrap }));
vi.mock("../src/lib/protocol", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/protocol")>(),
  cmd: mocks.cmd,
  onFirmwareMessage: mocks.onFirmwareMessage,
  onFirmwareRawLine: vi.fn(async () => () => {}),
  onDisconnected: mocks.onDisconnected,
  onReconnecting: vi.fn(async () => () => {}),
  onReconnected: vi.fn(async () => () => {}),
}));
vi.mock("../src/lib/installer", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/installer")>(),
  detectPedal: mocks.detectPedal,
}));
vi.mock("../src/lib/firmware-update", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/firmware-update")>(),
  fetchBundledVersion: vi.fn(async () => "0.6.3"),
  fetchLatestRelease: vi.fn(async () => null),
}));
vi.mock("../src/lib/android-lifecycle", () => ({
  onLifecycleChange: mocks.lifecycle,
  onBackButton: vi.fn(() => () => {}),
  saveSessionState: vi.fn(),
  restoreSessionState: vi.fn(() => null),
}));

let backendConnected = false;

beforeEach(() => {
  backendConnected = false;
  localStorage.setItem("BOSUN_ONBOARDED", "1");
  localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "", port: "9876" }));
  mocks.invoke.mockReset().mockImplementation(async (command: string) => {
    if (command === "is_connected") return backendConnected;
    if (command === "tcp_connect") { backendConnected = true; return; }
    if (command === "disconnect") { backendConnected = false; return; }
    if (command === "discover_hubs" || command === "list_ports" || command === "tcp_list_ports") return [];
    if (command === "auto_connect") throw new Error("No USB pedal connected");
    if (command === "midi_bridge_status") return { active: false, kemper_port: null, pedal_port: null };
    throw new Error(`Unexpected IPC: ${command}`);
  });
  mocks.detectPedal.mockReset().mockResolvedValue({ kind: "none" });
  mocks.lifecycle.mockReset().mockImplementation(() => () => {});
  mocks.onFirmwareMessage.mockReset().mockImplementation(async () => () => {});
  mocks.onDisconnected.mockReset().mockImplementation(async () => () => {});
  mocks.readNetworkBootstrap.mockReset().mockResolvedValue({ profiles: [], active: null });
  for (const command of Object.values(mocks.cmd)) command.mockReset().mockResolvedValue({});
  mocks.cmd.listProfiles.mockResolvedValue({ profiles: [] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

async function ready() {
  const view = render(App);
  // This registration is the end of App's async startup path.
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
  return view;
}

async function submitAddress(host: string, port: string) {
  const input = screen.getByLabelText("IP address or hostname");
  await fireEvent.input(input, { target: { value: host } });
  await fireEvent.input(screen.getByLabelText("Port"), { target: { value: port } });
  await fireEvent.submit(input.closest("form")!);
}

describe("App network connection", () => {
  it("recovers one dropped TCP session and bootstraps again despite duplicate loss events", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    backendConnected = false;
    mocks.onDisconnected.mock.calls[0][0]();
    mocks.onDisconnected.mock.calls[0][0]();
    window.dispatchEvent(new CustomEvent("rust-disconnected"));
    await vi.advanceTimersByTimeAsync(2500);
    expect(screen.getByTitle("Connected on tcp://bosun-hub.local:9876")).toBeInTheDocument();
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(2);
    expect(mocks.readNetworkBootstrap).toHaveBeenCalledTimes(2);
    expect(mocks.invoke.mock.calls.some(([name]) => name === "auto_connect")).toBe(false);
  });

  it("stops automatic network recovery after bounded failed attempts", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    const implementation = mocks.invoke.getMockImplementation()!;
    mocks.invoke.mockImplementation(async (name: string, args?: unknown) => {
      if (name === "tcp_connect") throw new Error("Hub offline");
      return implementation(name, args);
    });
    backendConnected = false;
    mocks.onDisconnected.mock.calls[0][0]();
    await vi.advanceTimersByTimeAsync(25_000);
    expect(screen.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Lost connection to the Raspberry Pi");
    const attempts = mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect").length;
    expect(attempts).toBeGreaterThan(2);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(attempts);
  });

  it("ignores a delayed duplicate loss event while the recovered session bootstraps", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    let finishBootstrap!: () => void;
    mocks.readNetworkBootstrap.mockImplementationOnce(() => new Promise(resolve => {
      finishBootstrap = () => resolve({ profiles: [], active: null });
    }));
    backendConnected = false;
    mocks.onDisconnected.mock.calls[0][0]();
    await vi.advanceTimersByTimeAsync(2500);
    expect(backendConnected).toBe(true);
    expect(mocks.readNetworkBootstrap).toHaveBeenCalledTimes(2);
    const disconnects = mocks.invoke.mock.calls.filter(([name]) => name === "disconnect").length;
    mocks.onDisconnected.mock.calls[0][0]();
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "disconnect")).toHaveLength(disconnects);
    expect(backendConnected).toBe(true);
    finishBootstrap();
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByTitle("Connected on tcp://bosun-hub.local:9876")).toBeInTheDocument();
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(2);
  });

  it.each(["handshake", "settling"])("releases an owned reconnect completed after unmount during %s", async (phase) => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    const view = render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    const implementation = mocks.invoke.getMockImplementation()!;
    let finishConnect!: () => void;
    mocks.invoke.mockImplementation(async (name: string, args?: unknown) => {
      if (name === "tcp_connect") {
        await new Promise<void>(resolve => { finishConnect = resolve; });
        backendConnected = true;
        return;
      }
      return implementation(name, args);
    });
    backendConnected = false;
    mocks.onDisconnected.mock.calls[0][0]();
    await vi.advanceTimersByTimeAsync(2100);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(2);
    if (phase === "settling") {
      finishConnect();
      await vi.advanceTimersByTimeAsync(0);
      expect(backendConnected).toBe(true);
    }
    view.unmount();
    if (phase === "handshake") finishConnect();
    await vi.advanceTimersByTimeAsync(500);
    expect(backendConnected).toBe(false);
    expect(mocks.readNetworkBootstrap).toHaveBeenCalledOnce();
    // A callback already queued by the old subscription must stay inert too.
    const disconnects = mocks.invoke.mock.calls.filter(([name]) => name === "disconnect").length;
    mocks.onDisconnected.mock.calls[0][0]();
    await vi.advanceTimersByTimeAsync(25_000);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "disconnect")).toHaveLength(disconnects);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(2);
  });

  it("keeps the manual Disconnect button disconnected when the backend notifies link loss", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    const implementation = mocks.invoke.getMockImplementation()!;
    mocks.invoke.mockImplementation(async (name: string, args?: unknown) => {
      if (name === "disconnect" && backendConnected) {
        backendConnected = false;
        mocks.onDisconnected.mock.calls[0][0]();
      }
      return implementation(name, args);
    });
    await fireEvent.click(screen.getByRole("button", { name: "Menu", exact: true }));
    await fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: /Disconnect$/ }));
    await vi.advanceTimersByTimeAsync(25_000);
    expect(screen.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(1);
  });

  it("does not reconnect after explicit Disconnect or after the app closes during backoff", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    const view = render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: "manual" }));
    await vi.advanceTimersByTimeAsync(25_000);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(1);
    await submitAddress("bosun-hub.local", "9876");
    await vi.waitFor(() => expect(mocks.readNetworkBootstrap).toHaveBeenCalledTimes(2));
    backendConnected = false;
    mocks.onDisconnected.mock.calls[0][0]();
    view.unmount();
    await vi.advanceTimersByTimeAsync(25_000);
    expect(mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect")).toHaveLength(2);
  });

  it("submits the editable address to tcp_connect and persists both fields", async () => {
    await ready();
    await submitAddress("  192.168.1.72  ", "9000");
    await waitFor(() => expect(mocks.invoke).toHaveBeenCalledWith("tcp_connect", { addr: "192.168.1.72:9000" }));
    await screen.findByTitle("Connected on tcp://192.168.1.72:9000");
    expect(screen.queryByRole("button", { name: /^Bridge (OFF|ON)$/ })).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("BOSUN_CONNECTION")!)).toEqual({
      mode: "network", host: "  192.168.1.72  ", port: "9000",
    });
    expect(mocks.readNetworkBootstrap).toHaveBeenCalledOnce();
    expect(mocks.invoke.mock.calls.some(([name]) => name === "auto_connect")).toBe(false);
  });

  it.each([
    ["", "9876", "Enter the Raspberry Pi IP address or hostname"],
    ["192.168.1.999", "9876", "Enter a valid IP address"],
    ["bosun-hub.local", "65536", "Enter a port between 1 and 65535"],
    ["tcp://192.168.1.72:9876", "9876", "Enter an IP address or hostname only"],
  ])("rejects invalid endpoint %s / %s before opening a socket", async (host, port, error) => {
    await ready();
    await submitAddress(host, port);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(error));
    expect(mocks.invoke.mock.calls.some(([name]) => name === "tcp_connect")).toBe(false);
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect", exact: true })).toBeEnabled());
  });

  it("restores the network target on startup and keeps USB discovery and MIDI bridging idle", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    expect(screen.getByTitle("Connected on tcp://bosun-hub.local:9876")).toBeInTheDocument();
    expect(mocks.invoke).toHaveBeenCalledWith("tcp_connect", { addr: "bosun-hub.local:9876" });
    await vi.advanceTimersByTimeAsync(11_000);
    expect(mocks.detectPedal).not.toHaveBeenCalled();
    const commands = mocks.invoke.mock.calls.map(([name]) => name);
    expect(commands).not.toContain("auto_connect");
    expect(commands).not.toContain("list_ports");
    expect(commands).not.toContain("midi_bridge_start");
    expect(commands).not.toContain("midi_bridge_status");
  });

  it("does not poll for an unflashed USB pedal while waiting for a network address", async () => {
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(7_500);
    expect(screen.getByLabelText("IP address or hostname")).toBeEnabled();
    expect(mocks.detectPedal).not.toHaveBeenCalled();
    const commands = mocks.invoke.mock.calls.map(([name]) => name);
    expect(commands).not.toContain("auto_connect");
    expect(commands).not.toContain("list_ports");
    expect(commands).not.toContain("tcp_connect");
    expect(screen.queryByRole("button", { name: /Install firmware/ })).not.toBeInTheDocument();
  });

  it("waits for network bootstrap before mounting profile and dashboard queries", async () => {
    vi.useFakeTimers();
    render(App);
    await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    let finishBootstrap!: (value: { profiles: []; active: null }) => void;
    const pendingBootstrap = new Promise<{ profiles: []; active: null }>((resolve) => { finishBootstrap = resolve; });
    mocks.readNetworkBootstrap.mockReturnValueOnce(pendingBootstrap);
    await submitAddress("192.168.1.72", "9876");
    await vi.waitFor(() => expect(mocks.readNetworkBootstrap).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(11_000);
    expect(screen.getByLabelText("IP address or hostname")).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "Welcome back" })).not.toBeInTheDocument();
    expect(mocks.cmd.listProfiles).not.toHaveBeenCalled();
    expect(mocks.cmd.getStats).not.toHaveBeenCalled();
    expect(mocks.cmd.getManifest).not.toHaveBeenCalled();
    expect(mocks.cmd.getManifestAwait).not.toHaveBeenCalled();

    finishBootstrap({ profiles: [], active: null });
    await vi.waitFor(() => expect(screen.getByTitle("Connected on tcp://192.168.1.72:9876")).toBeInTheDocument());
    await vi.waitFor(() => expect(mocks.cmd.getStats).toHaveBeenCalled());
    expect(mocks.cmd.listProfiles).toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Welcome back" })).toBeInTheDocument();
  });

  it("closes the network link and offers Connect again when bootstrap fails", async () => {
    await ready();
    mocks.readNetworkBootstrap.mockRejectedValueOnce(new Error("Captain response timed out"));
    await submitAddress("192.168.1.72", "9876");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not load the Captain over the network: Error: Captain response timed out",
    ));
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect", exact: true })).toBeEnabled());
    expect(screen.getByLabelText("IP address or hostname")).toHaveValue("192.168.1.72");
    expect(backendConnected).toBe(false);
    expect(mocks.cmd.listProfiles).not.toHaveBeenCalled();
    expect(mocks.cmd.getStats).not.toHaveBeenCalled();
    const commands = mocks.invoke.mock.calls.map(([name]) => name);
    expect(commands.lastIndexOf("disconnect")).toBeGreaterThan(commands.lastIndexOf("tcp_connect"));
  });

  it("shows Raspberry Pi routing in MIDI Learn without offering a local bridge", async () => {
    await ready();
    await submitAddress("192.168.1.72", "9876");
    await screen.findByTitle("Connected on tcp://192.168.1.72:9876");
    mocks.onFirmwareMessage.mock.calls[0][0]({
      type: "DEVICE_INFO", fw: "0.6.3", device: "MIDI Captain",
      profile: "kemper", current: { bank: 1, slot: 1 },
    });
    await fireEvent.click(screen.getByRole("button", { name: "Menu", exact: true }));
    await fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: /MIDI Learn$/ }));
    expect(await screen.findByText("MIDI routing is managed by the Raspberry Pi.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Start|Stop) bridge$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Bridge (OFF|ON)$/ })).not.toBeInTheDocument();
    expect(screen.queryByText("through this PC")).not.toBeInTheDocument();
    const commands = mocks.invoke.mock.calls.map(([name]) => name);
    expect(commands).not.toContain("midi_bridge_start");
    expect(commands).not.toContain("midi_bridge_status");
  });

  it("reuses the saved network endpoint on foreground recovery instead of auto-detecting USB", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "192.168.1.72", port: "9000" }));
    // Fail the startup TCP attempt while still answering initial connectivity.
    const implementation = mocks.invoke.getMockImplementation()!;
    let firstConnect = true;
    mocks.invoke.mockReset().mockImplementation(async (name: string, args?: unknown) => {
      if (name === "tcp_connect" && firstConnect) { firstConnect = false; throw new Error("Hub offline"); }
      return implementation(name, args);
    });
    await ready();
    expect(screen.getByLabelText("IP address or hostname")).toHaveValue("192.168.1.72");
    const onLifecycle = mocks.lifecycle.mock.calls[0][0];
    onLifecycle("active");
    await screen.findByTitle("Connected on tcp://192.168.1.72:9000");
    const attempts = mocks.invoke.mock.calls.filter(([name]) => name === "tcp_connect");
    expect(attempts).toEqual([
      ["tcp_connect", { addr: "192.168.1.72:9000" }],
      ["tcp_connect", { addr: "192.168.1.72:9000" }],
    ]);
    expect(mocks.invoke.mock.calls.some(([name]) => name === "auto_connect")).toBe(false);
  });
});
