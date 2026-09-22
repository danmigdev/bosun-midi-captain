import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/svelte";
import SetupWizard from "../../src/components/SetupWizard.svelte";
import Installer from "../../src/components/Installer.svelte";
import { piInstallCommand } from "../../src/lib/setup-guide";
const mocks = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
beforeEach(() => {
  localStorage.clear(); vi.clearAllMocks();
  Object.defineProperty(HTMLDialogElement.prototype,"showModal",{ configurable:true,value() { this.setAttribute("open",""); } });
});
afterEach(cleanup);
it('shows shared PROFILER settings, DIN feedback and the HDMI display together', async () => {
  render(SetupWizard, { standalone: true });
  await fireEvent.change(screen.getByLabelText('Kemper model'), { target: { value: 'stage' } });
  await fireEvent.change(screen.getByLabelText('MIDI connection'), { target: { value: 'din' } });
  await fireEvent.change(screen.getByLabelText('Hardware generation'), { target: { value: 'MK2' } });
  await fireEvent.click(screen.getByRole('button', { name: /Raspberry Pi 3 \+ HDMI display/ }));
  expect(screen.getByRole('img')).toHaveAccessibleName(/Kemper Stage MIDI OUT to Captain MIDI IN.*Pi HDMI/);
  await fireEvent.click(screen.getByRole('button', { name: '4. Connect your devices' }));
  expect(screen.getByText(/Leave the Kemper USB cable disconnected/)).toBeInTheDocument();
  expect(screen.getByText(/HDMI does not power it/)).toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem('BOSUN_KEMPER_SETUP')!)).toMatchObject({ target: 'stage', generation: 'MK2', connection: 'din' });
});
it("shows six configurations, persists the choice and explains the direct connection", async () => {
  const install = vi.fn(); render(SetupWizard,{ standalone:true,onInstall:install });
  expect(screen.getByRole("button",{ name:"Next" })).toBeDisabled();
  expect(screen.getAllByRole("button",{ pressed:false })).toHaveLength(6);
  await fireEvent.click(screen.getByRole("button",{ name:/Captain \+ Kemper Player/ }));
  expect(localStorage.getItem("BOSUN_SETUP")).toBe("direct");
  expect(screen.getByRole("img")).toHaveAccessibleName("Captain USB-B connected to Kemper Player USB-A");
  await fireEvent.click(screen.getByRole("button",{ name:"4. Connect your devices" }));
  expect(screen.getByText(/does not provide an external Bosun Stage display/)).toBeInTheDocument();
  expect(install).not.toHaveBeenCalled(); expect(mocks.invoke).not.toHaveBeenCalled();
});
it("adapts Android instructions without offering a Pi card setup or flashing in the standalone guide", async () => {
  render(SetupWizard,{ standalone:true });
  await fireEvent.click(screen.getByRole("button",{ name:/Captain \+ Android/ }));
  await fireEvent.click(screen.getByRole("button",{ name:"3. Prepare the host" }));
  expect(screen.getByText(/Simultaneous charging/)).toBeInTheDocument();
  expect(screen.queryByText("1. Write the microSD card")).not.toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button",{ name:"2. Prepare the Captain" }));
  expect(screen.queryByRole("button",{ name:"Install native firmware" })).not.toBeInTheDocument();
});
it("exports a Pi package only on request and generates a quoted Windows command", async () => {
  mocks.invoke.mockResolvedValue("C:\\Users\\A B\\bosun-pi-setup.tar.gz");
  render(SetupWizard,{});
  await fireEvent.click(screen.getByRole("button",{ name:/Raspberry Pi 3 \+ HDMI display/ }));
  await fireEvent.click(screen.getByRole("button",{ name:"3. Prepare the host" }));
  expect(mocks.invoke).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button",{ name:"Save Raspberry Pi package" }));
  await fireEvent.input(screen.getByLabelText("Username chosen in Imager"),{target:{value:"musician"}});
  expect(await screen.findByText(/scp 'C:/)).toBeInTheDocument();
  expect(mocks.invoke).toHaveBeenCalledExactlyOnceWith("export_pi_setup");
});
it("requires device and model confirmation and never invokes a legacy firmware installer", async () => {
  mocks.invoke.mockResolvedValue({ devices:[{id:"selected",port:"COM5",label:"Captain COM5"}],version:"0.6.6-native",problem:null });
  const start=vi.fn(); render(Installer,{onClose:vi.fn(),onStart:start});
  await screen.findByText("Captain COM5");
  expect(screen.getByRole("button",{name:"Continue"})).toBeDisabled();
  expect(start).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("checkbox"));
  await fireEvent.click(screen.getByRole("button",{name:"Continue"}));
  expect(start).toHaveBeenCalledExactlyOnceWith("selected","0.6.6-native");
  expect(mocks.invoke).toHaveBeenCalledExactlyOnceWith("factory_install_discover");
});
it("blocks incomplete assets even when a Captain is connected",async () => {
  mocks.invoke.mockResolvedValue({devices:[{id:"selected",port:"COM5",label:"Captain COM5"}],version:null,problem:"Checksum mismatch"});
  render(Installer,{onClose:vi.fn(),onStart:vi.fn()});
  await screen.findByRole("alert"); await fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button",{name:"Continue"})).toBeDisabled();
});
it("rejects shell syntax in credentials and quotes literal paths", () => {
  expect(piInstallCommand("C:\\A'B\\$(echo no).tar.gz","musician","bosun.local")).toContain("'C:\\A''B\\$(echo no).tar.gz'");
  for (const host of ["-oProxyCommand=bad","pi;bad","pi$(bad)","pi\nno"]) expect(piInstallCommand("x","musician",host)).toBe("");
  expect(piInstallCommand("x","root;bad","pi")).toBe("");
});
