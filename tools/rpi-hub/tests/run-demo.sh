#!/usr/bin/env bash
# Run the Stage kiosk against a lively fake pedal on the Pi, so the panel
# (or a browser at http://<pi>:8080/) shows a realistic Stage View with
# no real hardware. Stops the normal bosun-hub.service while it runs.
#
#   ./run-demo.sh start   |   ./run-demo.sh stop
set -u

SRC=/home/bosun/bosun-hub
OPT=/opt/bosun-hub

start() {
    sudo systemctl stop bosun-hub.service 2>/dev/null || true
    pkill -f stage_demo_pedal.py 2>/dev/null || true
    pkill -f 'bosun_hub --target' 2>/dev/null || true
    sleep 1
    setsid python3 "$SRC/tests/stage_demo_pedal.py" >/tmp/demo-pedal.log 2>&1 &
    sleep 2
    # demo pedal holds :9876, so serve the editor-TCP endpoint on :9899
    ( cd "$OPT" && setsid python3 -m bosun_hub --target tcp://127.0.0.1:9876 \
        --tcp-port 9899 --stage-dir "$OPT/stage" >/tmp/hub-demo.log 2>&1 & )
    sleep 4
    echo "listening:"
    sudo ss -tlnp | grep -E ':9876|:8080|:8081' | awk '{print "  "$4}'
    echo "hub:"
    grep -E 'link|Stage|protocol' /tmp/hub-demo.log | sed 's/^/  /'
    echo "http: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/)"
    echo
    echo "open  http://192.168.1.91:8080/  in a browser"
}

stop() {
    pkill -f stage_demo_pedal.py 2>/dev/null || true
    pkill -f 'bosun_hub --target' 2>/dev/null || true
    sleep 1
    sudo systemctl start bosun-hub.service
    echo "demo stopped, bosun-hub.service back up"
}

case "${1:-}" in
    start) start ;;
    stop)  stop ;;
    *) echo "usage: $0 {start|stop}" ; exit 1 ;;
esac
