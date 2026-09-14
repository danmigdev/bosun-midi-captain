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

    def stop(self):
        self.policy_running = False

    def start(self):
        self.policy_running = True
        self.hdmi_enabled = self.present
        self.virtual_enabled = not self.present

    def connected(self):
        return {"HDMI-A-1"} if self.present else set()

    def query(self, *args):
        if args == ("--json",):
            return [{"name": "HDMI-A-1"}, {"name": "HEADLESS-1"}]
        assert not self.policy_running, "kanshi must not race the deliberate off interval"
        self.commands.append(args)
        if args[1] == "HEADLESS-1":
            self.virtual_enabled, self.hdmi_enabled = True, False
        else:
            if self.fail_enable:
                raise subprocess.CalledProcessError(1, "wlr-randr")
            self.hdmi_enabled, self.virtual_enabled = True, False

    def off_interval(self, seconds):
        assert seconds >= 2
        assert self.virtual_enabled and not self.hdmi_enabled
        assert not self.policy_running


def recover(display, **kwargs):
    return hdmi.rearm_hdmi(display, query=display.query, connected=display.connected,
                           pause=kwargs.get("pause", display.off_interval))


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
