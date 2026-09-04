#!/usr/bin/env bash
# Run the Stage kiosk against a lively fake pedal on the Pi, so the panel
# (or a browser at http://<pi>:8080/) shows a realistic Stage View with
# no real hardware. Stops the normal bosun-hub.service while it runs.
#
#   ./run-demo.sh start | stop | status
set -u

SRC=/home/bosun/bosun-hub
OPT=/opt/bosun-hub

spawn() {  # spawn <logfile> <cmd...>
    local log="$1"; shift
    setsid --fork "$@" </dev/null >"$log" 2>&1
}

wait_gone() {  # wait_gone <port>
    for _ in $(seq 1 20); do
        ss -tln | grep -q ":$1 " || return 0
        sleep 0.25
    done
}

start() {
    sudo systemctl stop bosun-hub.service 2>/dev/null || true
    pkill -f stage_demo_pedal.py 2>/dev/null || true
    pkill -f 'bosun_hub --target' 2>/dev/null || true
    wait_gone 9876
    spawn /tmp/demo-pedal.log python3 "$SRC/tests/stage_demo_pedal.py"
    sleep 1
    ( cd "$OPT" && spawn /tmp/hub-demo.log python3 -m bosun_hub \
        --target tcp://127.0.0.1:9876 --tcp-port 9899 --stage-dir "$OPT/stage" )
    sleep 3
    status
}

stop() {
    pkill -f stage_demo_pedal.py 2>/dev/null || true
    pkill -f 'bosun_hub --target' 2>/dev/null || true
    wait_gone 9876
    sudo systemctl start bosun-hub.service
    echo "demo stopped, bosun-hub.service back up"
}

status() {
    echo "procs:"
    pgrep -af 'stage_demo_pedal|bosun_hub --target' | sed 's/^/  /' || echo "  (none)"
    echo "listening: $(ss -tln | grep -oE ':(9876|8080|8081|9899)' | sort -u | tr '\n' ' ')"
    echo "kiosk http: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || echo down)"
    echo "-> open http://192.168.1.91:8080/"
}

case "${1:-}" in
    start) start ;;
    stop)  stop ;;
    status) status ;;
    *) echo "usage: $0 {start|stop|status}"; exit 1 ;;
esac
