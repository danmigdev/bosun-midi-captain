"""Recovery races observed with a separately powered HDMI panel on the Pi."""

import importlib.util
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "kiosk/bosun-hdmi-recovery.py"
SPEC = importlib.util.spec_from_file_location("bosun_hdmi_recovery", SCRIPT)
hdmi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hdmi)


def test_boot_recovery_waits_for_panel_then_leaves_healthy_display_alone():
    schedule = hdmi.RecoverySchedule()
    schedule.observe({"HDMI-A-1"}, False, 0)
    assert not schedule.ready(2)
    assert schedule.ready(3)
    schedule.complete(True, 5)
    for now in (6, 30, 3600):
        schedule.observe({"HDMI-A-1"}, False, now)
        assert not schedule.ready(now)


def test_display_can_arrive_late_and_disconnect_during_settle():
    schedule = hdmi.RecoverySchedule()
    schedule.observe(set(), False, 0)
    assert not schedule.ready(100)
    schedule.observe({"HDMI-A-1"}, True, 100)
    schedule.observe(set(), True, 101)
    assert not schedule.ready(110)
    schedule.observe({"HDMI-A-1"}, True, 200)
    assert not schedule.ready(202)
    assert schedule.ready(203)


def test_fast_power_cycle_is_detected_even_when_connector_snapshot_is_unchanged():
    schedule = hdmi.RecoverySchedule()
    schedule.observe({"HDMI-A-1"}, False, 0)
    schedule.complete(True, 5)
    schedule.observe({"HDMI-A-1"}, True, 20)
    schedule.observe({"HDMI-A-1"}, True, 21)
    assert not schedule.ready(23)
    assert schedule.ready(24)


def test_failures_have_bounded_retries_and_new_hotplug_gets_a_fresh_attempt():
    schedule = hdmi.RecoverySchedule()
    schedule.observe({"HDMI-A-1"}, False, 0)
    for now in (3, 8, 13):
        assert schedule.ready(now)
        schedule.complete(False, now)
    schedule.observe({"HDMI-A-1"}, False, 100)
    assert not schedule.ready(100)
    schedule.observe({"HDMI-A-1"}, True, 101)
    assert schedule.ready(104)
    assert schedule.attempts == 0


class Display:
    """Model output ownership, so a racing kanshi would undo HDMI-off early."""

    def __init__(self):
        self.policy_running = True
        self.present = True
        self.hdmi_enabled = True
        self.virtual_enabled = False
        self.fail_enable = False
        self.commands = []
        self.mirror_running = True

    def stop(self):
        self.policy_running = False

    def start(self):
        assert not self.mirror_running, "wayvnc and kanshi must agree on the virtual mode"
        self.policy_running = True
        self.hdmi_enabled = self.present
        self.virtual_enabled = not self.present

    def connected(self):
        return {"HDMI-A-1"} if self.present else set()

    def stop_mirror(self):
        self.mirror_running = False

    def query(self, *args):
        if args == ("--json",):
            return [{"name": "HDMI-A-1"}, {"name": "HEADLESS-1"}]
        assert not self.policy_running, "kanshi must not race the deliberate off interval"
        self.commands.append(args)
        if args[1] == "HEADLESS-1":
            assert not self.mirror_running, "cage aborts with a wayvnc that holds the old mode"
            self.virtual_enabled, self.hdmi_enabled = True, False
        else:
            if self.fail_enable:
                raise subprocess.CalledProcessError(1, "wlr-randr")
            self.hdmi_enabled, self.virtual_enabled = True, False

    def off_interval(self, seconds):
        assert seconds >= 2
        assert self.virtual_enabled and not self.hdmi_enabled
        assert not self.policy_running
        self.mirror_running = True  # systemd has restarted wayvnc meanwhile


def recover(display, **kwargs):
    return hdmi.rearm_hdmi(display, query=display.query, connected=display.connected,
                           pause=kwargs.get("pause", display.off_interval),
                           mirror=display.stop_mirror)


def test_recovery_keeps_virtual_stage_during_off_interval_and_restores_policy():
    display = Display()
    assert recover(display)
    assert display.hdmi_enabled and not display.virtual_enabled
    assert display.policy_running


def test_unplug_during_recovery_keeps_fallback_and_does_not_enable_missing_hdmi():
    display = Display()

    def unplug(seconds):
        display.off_interval(seconds)
        display.present = False

    assert recover(display, pause=unplug)
    assert display.policy_running and display.virtual_enabled
    assert not display.hdmi_enabled
    assert len(display.commands) == 1


def test_failed_modeset_returns_control_to_policy_instead_of_leaving_hdmi_off():
    display = Display()
    display.fail_enable = True
    with pytest.raises(subprocess.CalledProcessError):
        recover(display)
    assert display.policy_running and display.hdmi_enabled


def test_cage_has_not_published_new_connector_yet_retries_without_changing_outputs():
    display = Display()
    assert not hdmi.rearm_hdmi(display, query=lambda *_: [{"name": "HEADLESS-1"}],
                               connected=display.connected)
    assert display.policy_running and not display.commands


def test_recovery_does_not_disable_only_output_if_virtual_fallback_is_missing():
    display = Display()
    assert not hdmi.rearm_hdmi(display, query=lambda *_: [{"name": "HDMI-A-1"}],
                               connected=display.connected)
    assert display.policy_running and display.hdmi_enabled


def mirror_service(monkeypatch, main_pid, alive_checks):
    """Fake bosun-wayvnc.service; returns the signals sent to its process."""
    sent, left = [], list(range(alive_checks))

    def kill(pid, number):
        if number == 0:
            if not left:
                raise ProcessLookupError
            left.pop()
            return
        sent.append((pid, number))

    def run(arguments, **_options):
        assert arguments[-1] == "bosun-wayvnc.service", "another wayvnc must stay alone"
        return subprocess.CompletedProcess(arguments, 0, stdout=main_pid)

    monkeypatch.setattr(hdmi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(hdmi.os, "kill", kill)
    monkeypatch.setattr(hdmi.subprocess, "run", run)
    return sent, left


def test_mirror_stop_ends_only_the_service_process_and_waits_for_it(monkeypatch):
    sent, left = mirror_service(monkeypatch, "4711\n", 3)
    hdmi.stop_mirror()
    assert sent == [(4711, hdmi.signal.SIGTERM)] and not left


@pytest.mark.parametrize("main_pid", ["0\n", "\n"])
def test_mirror_stop_ignores_a_stopped_mirror(monkeypatch, main_pid):
    sent, _ = mirror_service(monkeypatch, main_pid, 0)
    hdmi.stop_mirror()
    assert sent == []


def test_mirror_stop_never_fails_recovery(monkeypatch):
    mirror_service(monkeypatch, "4711\n", 1000)  # never ends
    hdmi.stop_mirror()

    def missing(*_arguments, **_options):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(hdmi.subprocess, "run", missing)
    hdmi.stop_mirror()


class Stop(Exception):
    """Ends the supervisor's endless loop."""


def supervise(monkeypatch, tmp_path, connected, results, seconds):
    """Run main() on a fake clock; returns (second, event) pairs."""
    clock, events, marker = [0.0], [], tmp_path / "outputs-settled"
    results = list(results)

    class Kanshi:
        process = type("Process", (), {"poll": staticmethod(lambda: None)})

        def __init__(self, _config):
            pass

        def start(self):
            pass

        stop = start

    def sleep(delay):
        if marker.exists() and not any(event == "settled" for _, event in events):
            events.append((clock[0], "settled"))
        clock[0] += delay
        if clock[0] > seconds:
            raise Stop

    def rearm(_policy):
        events.append((clock[0], "rearm"))
        assert not marker.exists(), "Chromium must not start before the outputs are stable"
        return results.pop(0)

    def no_netlink(*_arguments):
        raise OSError

    monkeypatch.setattr(hdmi, "SETTLED", marker)
    monkeypatch.setattr(hdmi, "Policy", Kanshi)
    monkeypatch.setattr(hdmi, "connected_hdmi", lambda: set(connected))
    monkeypatch.setattr(hdmi, "rearm_hdmi", rearm)
    monkeypatch.setattr(hdmi.signal, "signal", lambda *_arguments: None)
    monkeypatch.setattr(hdmi.socket, "AF_NETLINK", 16, raising=False)  # absent on Windows
    monkeypatch.setattr(hdmi.socket, "socket", no_netlink)
    monkeypatch.setattr(hdmi.time, "sleep", sleep)
    monkeypatch.setattr(hdmi.time, "monotonic", lambda: clock[0])
    with pytest.raises(Stop):
        hdmi.main("config")
    return events


def test_chromium_may_start_once_the_startup_recovery_is_over(monkeypatch, tmp_path):
    assert supervise(monkeypatch, tmp_path, {"HDMI-A-1"}, [True], 30) == [(3.0, "rearm"), (3.0, "settled")]


def test_chromium_may_start_at_once_without_a_panel(monkeypatch, tmp_path):
    assert supervise(monkeypatch, tmp_path, set(), [], 30) == [(0.0, "settled")]


def test_chromium_is_not_held_back_by_a_recovery_that_keeps_failing(monkeypatch, tmp_path):
    events = supervise(monkeypatch, tmp_path, {"HDMI-A-1"}, [False, False, False], 60)
    assert [event for _, event in events] == ["rearm", "rearm", "rearm", "settled"]
    assert events[-1][0] == 13.0


def test_hotplug_monitor_ignores_other_devices_and_userspace_packets():
    class Monitor:
        packets = iter([
            (b"SUBSYSTEM=usb\0HOTPLUG=1\0", (0, 1)),
            (b"SUBSYSTEM=drm\0HOTPLUG=1\0", (999, 1)),
            (b"SUBSYSTEM=drm\0", (0, 1)),
        ])

        def recvfrom(self, _size):
            try:
                return next(self.packets)
            except StopIteration:
                raise BlockingIOError

    monitor = Monitor()
    assert not hdmi.drain_hotplug(monitor)
    monitor.packets = iter([(b"SUBSYSTEM=drm\0HOTPLUG=1\0", (0, 1))])
    assert hdmi.drain_hotplug(monitor)
