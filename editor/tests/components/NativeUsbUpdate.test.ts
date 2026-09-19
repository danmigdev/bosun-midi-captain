import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import NativeUsbUpdate from "../../src/components/NativeUsbUpdate.svelte";
import type { UsbUpdateJob } from "../../src/lib/usb-update";

const mocks = vi.hoisted(() => ({ start: vi.fn(), factory: vi.fn(), recover: vi.fn(), status: vi.fn() }));
vi.mock("../../src/lib/usb-update", async importOriginal => ({
  ...await importOriginal<typeof import("../../src/lib/usb-update")>(),
  startUsbUpdate: mocks.start, startFactoryInstall: mocks.factory, recoverUsbUpdate: mocks.recover, usbUpdateStatus: mocks.status,
}));
const job = (phase: UsbUpdateJob["phase"], id = "new"): UsbUpdateJob => ({
  id, phase, port: "COM9", previous_version: "0.6.5-native", version: "0.6.6-native", percent: 0,
  backup_path: "", message: "Operation status", identity: { serial: "0123456789ABCDEF", bus: "1", ports: [2] },
});
const props = () => ({ port: "COM9", installed: "0.6.5-native", version: "0.6.6-native", job: null as UsbUpdateJob | null,
  canStart: () => true, onPreparing: vi.fn(), onJob: vi.fn(), onClose: vi.fn() });
beforeEach(() => {
  vi.clearAllMocks();
  mocks.status.mockResolvedValue(null);
  mocks.start.mockResolvedValue(job("preflight"));
  Object.defineProperty(HTMLDialogElement.prototype,"showModal",{ configurable: true, value() { this.setAttribute("open", ""); } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
it("starts factory installation only after Install and keeps status polling read-only", async () => {
  mocks.factory.mockResolvedValue({...job("preflight"),mode:"install"});
  const options={...props(),candidateId:"pinned-device"}; render(NativeUsbUpdate,options);
  expect(mocks.factory).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button",{name:"Install",exact:true}));
  await waitFor(() => expect(mocks.factory).toHaveBeenCalledExactlyOnceWith("pinned-device"));
  expect(mocks.start).not.toHaveBeenCalled();
});

it("requires an explicit update click and sends the selected USB port once", async () => {
  const options = props(); render(NativeUsbUpdate, options);
  expect(mocks.start).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
  await waitFor(() => expect(options.onJob).toHaveBeenCalledWith(job("preflight")));
  expect(options.onPreparing).toHaveBeenCalledOnce();
  expect(mocks.start).toHaveBeenCalledExactlyOnceWith("COM9");
  expect(mocks.recover).not.toHaveBeenCalled();
});
it("does not report an older successful job after a preflight rejection", async () => {
  mocks.status.mockResolvedValue(job("done", "old"));
  mocks.start.mockRejectedValue(new Error("Unsupported USB device"));
  const options = props(); render(NativeUsbUpdate, options);
  await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
  await screen.findByText("Error: Unsupported USB device");
  expect(options.onJob).toHaveBeenLastCalledWith(null);
});
it("recovers status after a lost start acknowledgement without resending the write command", async () => {
  mocks.status.mockResolvedValueOnce(null).mockResolvedValue(job("writing"));
  mocks.start.mockRejectedValue(new Error("IPC response lost"));
  const options = props(); render(NativeUsbUpdate, options);
  await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
  await waitFor(() => expect(options.onJob).toHaveBeenCalledWith(job("writing")));
  expect(mocks.start).toHaveBeenCalledOnce();
  expect(mocks.recover).not.toHaveBeenCalled();
});
it("polls a recovered job without automatically flashing or restoring and prevents closing during writes", async () => {
  vi.useFakeTimers(); mocks.status.mockResolvedValue(job("writing"));
  const options = { ...props(), job: job("writing") }; render(NativeUsbUpdate, options);
  await vi.advanceTimersByTimeAsync(1600);
  expect(mocks.status).toHaveBeenCalledTimes(2);
  expect(mocks.start).not.toHaveBeenCalled(); expect(mocks.recover).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
  await fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
  expect(options.onClose).not.toHaveBeenCalled();
});
it("allows recovery while disconnected only through Restore backup", async () => {
  mocks.recover.mockResolvedValue(undefined); mocks.status.mockResolvedValue(job("restoring"));
  const options = { ...props(), job: job("recovery-required"), canStart: () => false }; render(NativeUsbUpdate, options);
  expect(mocks.recover).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole("button", { name: "Restore backup" }));
  await waitFor(() => expect(options.onJob).toHaveBeenCalledWith(job("restoring")));
  expect(mocks.recover).toHaveBeenCalledOnce(); expect(mocks.start).not.toHaveBeenCalled();
});
