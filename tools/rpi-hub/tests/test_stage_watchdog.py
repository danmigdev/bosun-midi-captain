"""The Stage page's heartbeat and the watchdog that restarts a dead kiosk."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import signal
import sys
import urllib.error
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bosun_hub import server  # noqa: E402

SPEC = importlib.util.spec_from_file_location("bosun_stage_watchdog", ROOT / "kiosk/bosun-stage-watchdog.py")
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)


# -- hub endpoint ----------------------------------------------------------


@pytest.fixture
def hub(tmp_path):
    (tmp_path / "index.html").write_text("Stage")
    httpd = server._start_http(tmp_path, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def request(url, method="GET"):
    with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as response:
        return response.status, response.read()


def test_hub_reports_the_age_of_the_last_beat(hub):
    assert json.loads(request(hub + "/kiosk-alive")[1]) == {"age": None}
    assert request(hub + "/kiosk-alive", "POST") == (204, b"")
    age = json.loads(request(hub + "/kiosk-alive")[1])["age"]
    assert 0 <= age < 5
    # The static bundle is served as before.
    assert request(hub + "/")[1] == b"Stage"


def test_hub_accepts_no_other_post(hub):
    with pytest.raises(urllib.error.HTTPError) as refused:
        request(hub + "/index.html", "POST")
    assert refused.value.code == 404


def test_a_browser_on_the_network_cannot_answer_for_the_kiosk():
    class Handler(server._QuietHandler):
        def __init__(self, address):  # no request to handle
            self.path, self.client_address = "/kiosk-alive", (address, 50000)
            self.kiosk = server.KioskHeartbeat()

    assert Handler("127.0.0.1")._kiosk_request()
    assert not Handler("192.168.1.20")._kiosk_request()


# -- decision --------------------------------------------------------------


def run(health, pid, start, stop, age=lambda now: None, step=5):
    """First time the watchdog would restart the kiosk, or None."""
    now = start
    while now <= stop:
        if health.observe(pid, age(now), now):
            return now
        now += step
    return None


def beating(_now):
    return 2.0


def test_a_beating_page_is_never_restarted():
    assert run(watchdog.PageHealth(), 100, 0, 3600, beating) is None


def test_a_dead_page_is_restarted_after_four_missed_beats():
    health = watchdog.PageHealth()
    assert run(health, 100, 0, 600, beating) is None
    died = 598
    assert run(health, 100, 605, 900, lambda now: now - died) == died + 22  # next check after 20 s
    assert health.alive


def test_a_page_that_never_loads_is_restarted_after_the_slow_start_allowance():
    health = watchdog.PageHealth()
    assert run(health, 100, 0, 600) == watchdog.LOAD_SECONDS
    assert not health.alive


def test_the_previous_page_does_not_shorten_the_start_of_the_next():
    health = watchdog.PageHealth()
    assert run(health, 100, 0, 60, beating) is None
    # cage restarted: the hub still knows the old page's beat for a while.
    last = 58
    assert run(health, 200, 65, 400, lambda now: now - last) == 65 + watchdog.LOAD_SECONDS


def test_a_kiosk_that_never_shows_stage_is_retried_ever_less_often():
    health, start, waits = watchdog.PageHealth(), 0, []
    for pid in range(100, 106):
        at = run(health, pid, start, start + 2000)
        waits.append(at - start)
        health.restarting()
        start = at + 5
    assert waits == [120, 240, 480, 900, 900, 900]
    # One sign of life makes the next failure urgent again.
    assert run(health, 200, start, start + 100, beating) is None
    assert health.load_seconds == watchdog.LOAD_SECONDS


def test_a_page_that_died_does_not_slow_the_next_start():
    health = watchdog.PageHealth()
    run(health, 100, 0, 60, beating)
    assert run(health, 100, 65, 200, lambda now: now - 58) is not None
    health.restarting()
    assert health.load_seconds == watchdog.LOAD_SECONDS


def test_a_kiosk_stopped_on_purpose_stays_stopped():
    health = watchdog.PageHealth()
    run(health, 100, 0, 60, beating)
    assert run(health, 0, 65, 4000) is None


def test_a_hub_restart_is_not_held_against_the_page():
    health = watchdog.PageHealth()
    run(health, 100, 0, 60, beating)
    for now in range(65, 1000, 5):
        health.hub_silent(100, now)
    # The new hub has no beat yet; the page sends one within seconds.
    assert run(health, 100, 1000, 1010) is None
    assert run(health, 100, 1015, 1300, beating) is None


def test_a_page_that_died_while_the_hub_was_away_is_still_found():
    health = watchdog.PageHealth()
    run(health, 100, 0, 60, beating)
    for now in range(65, 1000, 5):
        health.hub_silent(100, now)
    assert run(health, 100, 1000, 1300) == 1015


def test_a_kiosk_that_started_without_the_hub_is_found_soon_after_it_returns():
    health = watchdog.PageHealth()
    for now in range(0, 300, 5):
        health.hub_silent(100, now)
    # Chromium is on its error page and offline: no beat will come.
    assert run(health, 100, 300, 600) == 315


# -- action ----------------------------------------------------------------


def test_restart_ends_cage_and_leaves_the_rest_to_systemd():
    sent = []
    watchdog.restart_kiosk(321, kill=lambda pid, number: sent.append((pid, number)),
                           alive=lambda pid: False, pause=lambda _: None)
    assert sent == [(321, signal.SIGTERM)]


def test_a_cage_that_ignores_the_request_is_killed(monkeypatch):
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)  # absent on Windows
    sent, waited = [], []
    watchdog.restart_kiosk(321, kill=lambda pid, number: sent.append(number),
                           alive=lambda pid: True, pause=waited.append)
    assert sent == [signal.SIGTERM, 9]
    assert sum(waited) == watchdog.STOP_SECONDS


def test_a_cage_that_is_already_gone_is_not_an_error():
    def kill(_pid, _number):
        raise ProcessLookupError

    watchdog.restart_kiosk(321, kill=kill, pause=lambda _: None)


class Loop(Exception):
    """Ends the watchdog's endless loop."""


def drive(monkeypatch, pages, checks):
    """Run main() for a number of checks; returns the kiosk pids it restarted."""
    clock, restarted = [0.0], []

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] > checks * watchdog.CHECK_SECONDS:
            raise Loop

    monkeypatch.setattr(watchdog.time, "sleep", sleep)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(watchdog, "kiosk_pid", lambda: 100 + len(restarted))
    monkeypatch.setattr(watchdog, "fetch", lambda path: pages(path, clock[0]))
    monkeypatch.setattr(watchdog, "restart_kiosk", restarted.append)
    with pytest.raises(Loop):
        watchdog.main()
    return restarted


def test_loop_restarts_a_kiosk_without_beats(monkeypatch):
    def pages(path, _now):
        return b'<meta name="bosun-kiosk-heartbeat" />' if path == "/" else b'{"age": null}'

    assert drive(monkeypatch, pages, 80) == [100, 101]  # after 120 s, then 240 s


def test_loop_ignores_a_stage_without_heartbeat(monkeypatch):
    def pages(path, _now):
        assert path == "/", "an older hub answers 404 here"
        return b"<title>Bosun Stage</title>"

    assert drive(monkeypatch, pages, 400) == []


def test_loop_never_restarts_while_the_hub_is_away(monkeypatch):
    def pages(_path, _now):
        raise urllib.error.URLError("Connection refused")

    assert drive(monkeypatch, pages, 400) == []


# -- installation ----------------------------------------------------------


def test_unit_outlives_the_kiosk_restarts_and_runs_as_the_kiosk_account():
    unit = (ROOT / "systemd/bosun-stage-watchdog.service").read_text(encoding="utf-8").splitlines()
    assert "User=bosun" in unit and "Restart=always" in unit
    assert not any(line.startswith(("PartOf=", "BindsTo=", "Requires=")) for line in unit)


def test_marker_matches_the_stage_page():
    page = (ROOT.parents[1] / "editor/stage-kiosk.html").read_bytes()
    assert watchdog.MARKER in page
