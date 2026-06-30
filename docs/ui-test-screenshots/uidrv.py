#!/usr/bin/env python3
"""Rich CDP driver for the Bosun editor WebView2.

Runs a batch of steps over a single websocket connection. Read steps as JSON
from a file (--steps file.json) or a literal string (--json '[...]').

Step kinds:
  {"do":"eval","expr":"..."}              -> evaluate JS, print result
  {"do":"click","sel":"..."}              -> document.querySelector(sel).click()
  {"do":"type","sel":"...","text":"..."}  -> set input value + fire input/change
  {"do":"key","key":"Enter","ctrl":true} -> dispatch keydown/keyup on window
  {"do":"wait","ms":500}                  -> sleep
  {"do":"shot","path":"name.png"}         -> screenshot (saved under script dir)
  {"do":"state"}                          -> dump high-signal app state (__app + dom)

Every step prints a one-line "[i] ok ..." trace. eval/state print their JSON.
"""
import argparse, base64, json, sys, time, urllib.request
from pathlib import Path
import websocket

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST, PORT = "localhost", 9222
OUT = Path(__file__).parent

def find_page():
    with urllib.request.urlopen(f"http://{HOST}:{PORT}/json", timeout=3) as r:
        targets = json.loads(r.read().decode())
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        sys.exit("no debuggable page target")
    return pages[0]

class CDP:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=10, max_size=None)
        self.seq = 0
    def send(self, method, params=None):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.ws.settimeout(2); raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            obj = json.loads(raw)
            if obj.get("id") == self.seq:
                if "error" in obj: raise RuntimeError(obj["error"])
                return obj.get("result", {})
        raise RuntimeError(f"timeout {method}")
    def ev(self, expr):
        # Wrap bare `return ...` snippets in an IIFE; leave complete
        # expressions (already an IIFE starting with "(") untouched.
        wrap = ("return" in expr) and not expr.strip().startswith("(")
        r = self.send("Runtime.evaluate",
                      {"expression": f"(()=>{{ {expr} }})()" if wrap else expr,
                       "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if res.get("subtype") == "error":
            return {"__error__": res.get("description")}
        return res.get("value", res.get("description"))
    def close(self):
        try: self.ws.close()
        except Exception: pass

STATE_EXPR = r"""
(() => {
  const a = (window.__app)||{};
  const navs = [...document.querySelectorAll('.navitem')].map(e=>({
    lbl:(e.querySelector('.lbl')||{}).textContent, active:e.classList.contains('active')}));
  const pill = document.querySelector('.connpill');
  const heads = [...document.querySelectorAll('.pageHead h2, h1')].map(e=>e.textContent.trim());
  const errs = [...document.querySelectorAll('.err,.toast')].map(e=>e.textContent.trim()).filter(Boolean);
  const modals = {
    onboarding: !!document.querySelector('.onboard, [class*=onboard]'),
    installer: !!document.querySelector('[class*=installer], .modal'),
  };
  return {
    page:a.page, connected:a.connected, port:a.connectedPortName,
    deviceInfo:a.deviceInfo, patches:(a.patches||[]).length,
    dirty:(a.dirtyIds||[]).length, learning:a.learning, error:a.error,
    hasManifest: !!a.manifest, currentPatch: a.currentPatch ? (a.currentPatch.bank+'/'+a.currentPatch.slot) : null,
    pill: pill ? pill.textContent.trim() : null,
    nav: navs, heads, errs, modals,
  };
})()
"""

def run(cdp, steps):
    for i, s in enumerate(steps):
        do = s.get("do")
        if do == "eval":
            print(f"[{i}] eval:", json.dumps(cdp.ev(s["expr"]), ensure_ascii=False)[:2000])
        elif do == "state":
            print(f"[{i}] state:", json.dumps(cdp.ev(STATE_EXPR), ensure_ascii=False, indent=2))
        elif do == "click":
            sel = s["sel"]
            r = cdp.ev(f"""const e=document.querySelector({json.dumps(sel)});
                if(!e) return {{ok:false,reason:'not found'}};
                e.scrollIntoView({{block:'center'}}); e.click();
                return {{ok:true,tag:e.tagName,txt:(e.textContent||'').trim().slice(0,50)}};""")
            print(f"[{i}] click {sel}:", json.dumps(r, ensure_ascii=False))
        elif do == "clickText":
            txt = s["text"]; tag = s.get("tag","button")
            r = cdp.ev(f"""const els=[...document.querySelectorAll({json.dumps(tag)})];
                const e=els.find(x=>(x.textContent||'').trim().includes({json.dumps(txt)}));
                if(!e) return {{ok:false,reason:'not found'}};
                e.scrollIntoView({{block:'center'}}); e.click();
                return {{ok:true,txt:(e.textContent||'').trim().slice(0,50)}};""")
            print(f"[{i}] clickText '{txt}':", json.dumps(r, ensure_ascii=False))
        elif do == "type":
            sel, text = s["sel"], s["text"]
            r = cdp.ev(f"""const e=document.querySelector({json.dumps(sel)});
                if(!e) return {{ok:false,reason:'not found'}};
                const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
                const setter=Object.getOwnPropertyDescriptor(proto,'value').set;
                setter.call(e,{json.dumps(text)});
                e.dispatchEvent(new Event('input',{{bubbles:true}}));
                e.dispatchEvent(new Event('change',{{bubbles:true}}));
                return {{ok:true,val:e.value}};""")
            print(f"[{i}] type {sel}:", json.dumps(r, ensure_ascii=False))
        elif do == "key":
            k = s["key"]
            r = cdp.ev(f"""const o={{key:{json.dumps(k)},ctrlKey:{str(s.get('ctrl',False)).lower()},
                metaKey:{str(s.get('meta',False)).lower()},bubbles:true}};
                window.dispatchEvent(new KeyboardEvent('keydown',o));
                window.dispatchEvent(new KeyboardEvent('keyup',o));
                return {{ok:true}};""")
            print(f"[{i}] key {k}:", json.dumps(r, ensure_ascii=False))
        elif do == "wait":
            time.sleep(s.get("ms", 500) / 1000.0)
            print(f"[{i}] wait {s.get('ms',500)}ms")
        elif do == "shot":
            res = cdp.send("Page.captureScreenshot", {"format": "png"})
            p = OUT / s["path"]
            p.write_bytes(base64.b64decode(res["data"]))
            print(f"[{i}] shot {p.name} ({p.stat().st_size} B)")
        else:
            print(f"[{i}] UNKNOWN step {s}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps"); ap.add_argument("--json")
    a = ap.parse_args()
    if a.steps: steps = json.loads(Path(a.steps).read_text(encoding="utf-8"))
    elif a.json: steps = json.loads(a.json)
    else: sys.exit("need --steps or --json")
    cdp = CDP(find_page()["webSocketDebuggerUrl"])
    try: run(cdp, steps)
    finally: cdp.close()

if __name__ == "__main__":
    main()
