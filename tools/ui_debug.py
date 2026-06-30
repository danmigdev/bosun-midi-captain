#!/usr/bin/env python3
"""Drive the Tauri editor's WebView2 via Chrome DevTools Protocol.

Launch the editor with the WEBVIEW2 debug port enabled first:

    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9222"
    npm run tauri dev

Then this script connects to ws://localhost:9222 and exposes:

    eval         run JavaScript in the editor, print return value (JSON)
    state        dump key Svelte state (page, connected, currentPatch, etc.)
    dom          dump the visible DOM tree of the main content area
    screenshot   save a PNG of the window
    click        click an element matched by CSS selector

Examples:

    python tools/ui_debug.py state
    python tools/ui_debug.py eval "globalDevice"
    python tools/ui_debug.py click ".navitem:nth-child(4)"
    python tools/ui_debug.py screenshot ui.png
"""
import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket


CDP_HOST = "localhost"
CDP_PORT = 9222


def get_targets() -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        sys.exit(f"cannot reach WebView2 debugger at {CDP_HOST}:{CDP_PORT} - is the editor running with --remote-debugging-port=9222?\n  {e}")


def find_page() -> dict:
    targets = get_targets()
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        sys.exit(f"no page targets among {len(targets)} debug entries")
    # Prefer the localhost:1420 page (vite dev), fallback to any.
    for p in pages:
        if "1420" in p.get("url", ""):
            return p
    return pages[0]


class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=5)
        self._seq = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._seq += 1
        msg = {"id": self._seq, "method": method, "params": params or {}}
        self.ws.send(json.dumps(msg))
        # Drain until we see our id (skip events / other replies).
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.ws.settimeout(2)
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            obj = json.loads(raw)
            if obj.get("id") == self._seq:
                if "error" in obj:
                    raise RuntimeError(f"CDP error: {obj['error']}")
                return obj.get("result", {})
        raise RuntimeError(f"timed out waiting for response to {method}")

    def close(self):
        try: self.ws.close()
        except Exception: pass


def js_eval(cdp: CDP, expr: str) -> object:
    result = cdp.send("Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": True,
    })
    r = result.get("result", {})
    if r.get("subtype") == "error":
        return {"error": r.get("description")}
    return r.get("value", r.get("description"))


# ---------------- commands ----------------

def cmd_eval(cdp: CDP, args):
    print(json.dumps(js_eval(cdp, args.expr), indent=2, ensure_ascii=False))


def cmd_state(cdp: CDP, args):
    """Snapshot the high-signal Svelte reactive vars by reading them from the DOM
    + a synthetic peek via known data attributes."""
    expr = """
    (() => {
      const get = (sel) => {
        const e = document.querySelector(sel);
        return e ? (e.textContent ?? '').trim() : null;
      };
      const all = (sel) => [...document.querySelectorAll(sel)].map(e => (e.textContent ?? '').trim());
      const active = document.querySelector('.navitem.active');
      const errs = [...document.querySelectorAll('.err, .ferr')].map(e => e.textContent.trim()).filter(Boolean);
      return {
        title: document.title,
        page_label: active ? active.querySelector('.lbl')?.textContent : null,
        connected: !!document.querySelector('.shell'),
        offline_card: !!document.querySelector('.welcome'),
        footer: get('.status'),
        page_head: get('.pageHead h2'),
        patches_visible: all('ul.patchlist .pname'),
        errors: errs,
        section_h3s: all('h3'),
        loading_msg: get('.muted'),
      };
    })()
    """
    state = js_eval(cdp, expr)
    print(json.dumps(state, indent=2, ensure_ascii=False))


def cmd_dom(cdp: CDP, args):
    expr = """
    (() => {
      const main = document.querySelector('.content') || document.body;
      return main.outerHTML.slice(0, 4000);
    })()
    """
    print(js_eval(cdp, expr))


def cmd_click(cdp: CDP, args):
    expr = f"""
    (() => {{
      const e = document.querySelector({json.dumps(args.selector)});
      if (!e) return {{clicked: false, reason: 'not found'}};
      e.click();
      return {{clicked: true, tag: e.tagName, text: (e.textContent||'').trim().slice(0,60)}};
    }})()
    """
    print(json.dumps(js_eval(cdp, expr), indent=2, ensure_ascii=False))


def cmd_screenshot(cdp: CDP, args):
    result = cdp.send("Page.captureScreenshot", {"format": "png"})
    data = base64.b64decode(result["data"])
    Path(args.path).write_bytes(data)
    print(f"screenshot saved: {args.path} ({len(data)} bytes)")


def cmd_listen(cdp: CDP, args):
    """Subscribe to console messages and print them as they fire."""
    cdp.send("Runtime.enable")
    print("listening for console messages (Ctrl-C to stop)")
    while True:
        try:
            cdp.ws.settimeout(60)
            raw = cdp.ws.recv()
        except KeyboardInterrupt:
            break
        except websocket.WebSocketTimeoutException:
            continue
        obj = json.loads(raw)
        if obj.get("method") == "Runtime.consoleAPICalled":
            args_ = obj["params"]["args"]
            txt = " ".join(str(a.get("value", a.get("description", ""))) for a in args_)
            print(f"[{obj['params'].get('type','log')}] {txt}")


# ---------------- entry ----------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state")
    sub.add_parser("dom")
    sub.add_parser("listen")

    se = sub.add_parser("eval"); se.add_argument("expr")
    sc = sub.add_parser("click"); sc.add_argument("selector")
    ss = sub.add_parser("screenshot"); ss.add_argument("path")

    args = p.parse_args()

    page = find_page()
    cdp = CDP(page["webSocketDebuggerUrl"])
    try:
        cdp.send("Runtime.enable")
        {
            "eval":       cmd_eval,
            "state":      cmd_state,
            "dom":        cmd_dom,
            "click":      cmd_click,
            "screenshot": cmd_screenshot,
            "listen":     cmd_listen,
        }[args.cmd](cdp, args)
    finally:
        cdp.close()


if __name__ == "__main__":
    main()
