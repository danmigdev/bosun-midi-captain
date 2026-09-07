"""Inspect the real Pi kiosk pixels through a local SSH-forwarded noVNC viewer.

Requires the deployed noVNC service and an existing SSH tunnel. This never
creates a Stage fixture, opens the application WebSocket or sends pointer/key
input. Only the viewer is resized and reloaded; the Pi framebuffer stays fixed.
The server must independently enforce view-only and no-resize (-d -R).
"""

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse

from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


PAGE = 'http://localhost:6080/vnc.html?autoconnect=1&resize=scale&view_only=1&reconnect=1'
VIEWPORTS = ((2048, 768), (1366, 768), (375, 667))

PROBE = r"""(async () => {
  const UI = (await import('/app/ui.js')).default;
  const rfb = UI.rfb;
  const canvas = [...document.querySelectorAll('#noVNC_container canvas')]
    .sort((a,b) => b.width*b.height-a.width*a.height)[0];
  const state = {connected:UI.connected, viewOnly:rfb?.viewOnly,
    resizeSession:rfb?.resizeSession, scaleViewport:rfb?.scaleViewport,
    desktopName:rfb?._desktopName, iframes:document.querySelectorAll('iframe').length,
    stageElements:document.querySelectorAll('.stage, .stage__switch').length,
    viewport:{width:innerWidth,height:innerHeight}};
  if (!canvas) return state;
  const box=canvas.getBoundingClientRect();
  state.canvas={width:canvas.width,height:canvas.height,
    x:box.x,y:box.y,displayWidth:box.width,displayHeight:box.height};
  if (!canvas.width || !canvas.height) return state;
  const pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
  let sampled=0,nonBlack=0,bright=0,colored=0;
  const colors=new Set();
  for (let i=0;i<pixels.length;i+=16) {
    const r=pixels[i],g=pixels[i+1],b=pixels[i+2];
    sampled++;
    if (Math.max(r,g,b)>16) nonBlack++;
    if (Math.min(r,g,b)>180) bright++;
    if (Math.max(r,g,b)-Math.min(r,g,b)>45) colored++;
    colors.add((r>>4)*256+(g>>4)*16+(b>>4));
  }
  state.pixels={sampled,nonBlack:nonBlack/sampled,bright:bright/sampled,
    colored:colored/sampled,colors:colors.size};
  return state;
})()"""


class VncSession(CdpSession):
    """Keep binary RFB evidence without copying framebuffer payloads into logs."""

    def __init__(self, websocket):
        super().__init__(websocket)
        self.wire = {}

    def observe(self, message):
        method = message.get('method', '')
        params = message.get('params', {})
        if method == 'Network.webSocketCreated':
            super().observe(message)
            self.wire[params['requestId']] = {
                'url':params['url'], 'sentFrames':0, 'receivedFrames':0,
                'sentBytes':0, 'receivedBytes':0, 'binaryFrames':0,
                'receivedPrefix':b'', 'closed':False,
            }
        elif method == 'Network.webSocketClosed':
            if params['requestId'] in self.wire:
                self.wire[params['requestId']]['closed'] = True
        elif method in ('Network.webSocketFrameSent', 'Network.webSocketFrameReceived'):
            record = self.wire[params['requestId']]
            response = params['response']
            binary = response['opcode'] == 2
            raw = (base64.b64decode(response['payloadData']) if binary
                   else response['payloadData'].encode('utf-8'))
            direction = 'sent' if method.endswith('Sent') else 'received'
            record[direction + 'Frames'] += 1
            record[direction + 'Bytes'] += len(raw)
            record['binaryFrames'] += int(binary)
            if direction == 'received' and len(record['receivedPrefix']) < 12:
                record['receivedPrefix'] = (record['receivedPrefix'] + raw)[:12]

    def evidence(self):
        return {key:{**value, 'receivedPrefix':value['receivedPrefix'].decode('ascii', 'replace')}
                for key,value in self.wire.items()}


async def ready(cdp, description):
    deadline = time.monotonic() + 40
    state = None
    while time.monotonic() < deadline:
        try:
            state = await cdp.evaluate(PROBE, await_promise=True)
        except RuntimeError:
            # A real reload briefly destroys the old execution context/module.
            state = None
        if state and state.get('connected') and state.get('pixels', {}).get('colors', 0) > 16:
            return state
        await asyncio.sleep(.2)
    raise AssertionError((description, state, cdp.evidence()))


def verify(state):
    assert state['connected'] and state['viewOnly'], state
    assert state['scaleViewport'] and not state['resizeSession'], state
    assert state['iframes'] == 0 and state['stageElements'] == 0, state
    canvas, pixels = state['canvas'], state['pixels']
    assert (canvas['width'],canvas['height']) == (1920,440), state
    assert abs(canvas['displayWidth']/1920-canvas['displayHeight']/440) < .002, state
    assert 0 < canvas['displayWidth'] <= state['viewport']['width'] + 1, state
    assert 0 < canvas['displayHeight'] <= state['viewport']['height'] + 1, state
    assert pixels['colors'] > 16 and pixels['nonBlack'] > .05, state
    assert pixels['bright'] > .001 and pixels['colored'] > .001, state


async def screenshot(cdp, target, *, framebuffer=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    if framebuffer:
        encoded = await cdp.evaluate("""[...document.querySelectorAll('#noVNC_container canvas')]
          .sort((a,b) => b.width*b.height-a.width*a.height)[0].toDataURL('image/png').split(',')[1]""")
    else:
        encoded = (await cdp.command('Page.captureScreenshot', {'format':'png'}))['data']
    data = base64.b64decode(encoded)
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


async def run(args):
    parsed = urlparse(args.page)
    if (parsed.scheme != 'http' or parsed.hostname not in ('localhost','127.0.0.1')
            or parsed.port != 6080 or parsed.path != '/vnc.html'):
        raise ValueError('Only the SSH-forwarded localhost HTTP 6080 noVNC viewer is permitted')
    query = parse_qs(parsed.query)
    for key,value in {'autoconnect':'1','resize':'scale','view_only':'1','reconnect':'1'}.items():
        if query.get(key) != [value]:
            raise ValueError(f'Required noVNC viewer parameter: {key}={value}')
    if set(query) - {'autoconnect','resize','view_only','reconnect','reconnect_delay'}:
        raise ValueError('VNC target/path/auth overrides are not permitted')

    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-vnc-cdp-')
    browser = subprocess.Popen([args.edge,'--headless=new','--disable-gpu','--no-first-run',
        f'--remote-debugging-port={port}','--user-data-dir=' + profile.name,'about:blank'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    cdp, error = None, None
    report = {'page':args.page,'states':[],'screenshots':{}}
    try:
        cdp = VncSession(await cdp_socket(port,args.page))
        await cdp.command('Network.enable')
        await cdp.command('Page.enable')
        await cdp.command('Page.navigate', {'url':args.page})
        await ready(cdp, 'Initial VNC connection')
        initial_connections = set(cdp.wire)
        assert len(initial_connections) == 1, cdp.evidence()
        for width,height in VIEWPORTS:
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width,'height':height,'deviceScaleFactor':1,'mobile':False,
            })
            await asyncio.sleep(.3)
            state = await ready(cdp, f'Viewer {width}x{height}')
            verify(state)
            report['states'].append(state)
            if args.screenshots:
                target = Path(args.screenshots) / f'vnc-viewer-{width}x{height}.png'
                report['screenshots'][str(target)] = await screenshot(cdp,target)
                if width == 2048:
                    target = Path(args.screenshots) / 'vnc-framebuffer-1920x440.png'
                    report['screenshots'][str(target)] = await screenshot(cdp,target,framebuffer=True)
            print(f'PASS viewer {width}x{height}: actual 1920x440 RFB canvas, visible colored pixels, view-only; no iframe or local Stage DOM',flush=True)
        assert set(cdp.wire) == initial_connections, ('Resize reconnected VNC',cdp.evidence())

        old_origin = await cdp.evaluate('performance.timeOrigin')
        await cdp.command('Page.reload', {'ignoreCache':False})
        reload_deadline = time.monotonic() + 15
        while await cdp.evaluate('performance.timeOrigin') == old_origin:
            assert time.monotonic() < reload_deadline, 'Viewer document did not reload'
            await asyncio.sleep(.1)
        state = await ready(cdp,'VNC viewer reload')
        verify(state)
        await cdp.evaluate('true')
        assert len(cdp.wire) == 2, cdp.evidence()
        # Edge can omit webSocketClosed when navigation destroys a document.
        # The changed timeOrigin proves replacement; additionally ensure the
        # prior transport receives no further data after the new one is ready.
        previous_frames = {key:cdp.wire[key]['receivedFrames'] for key in initial_connections}
        await asyncio.sleep(.8)
        await cdp.evaluate('true')
        assert previous_frames == {key:cdp.wire[key]['receivedFrames'] for key in initial_connections}, cdp.evidence()
        print('PASS viewer reload: document replaced, one replacement VNC connection, prior transport inactive, framebuffer remains 1920x440',flush=True)

        for connection in cdp.wire.values():
            target = urlparse(connection['url'])
            assert target.scheme == 'ws' and target.hostname == parsed.hostname and target.port == 6080, connection
            assert connection['receivedPrefix'] == b'RFB 003.008\n', connection
            assert connection['binaryFrames'] > 0 and connection['receivedBytes'] > 1000, connection
        report['wire'] = cdp.evidence()
        print('PASS binary RFB 3.8 traffic through the SSH viewer; no application WebSocket or device input generated',flush=True)
    except BaseException as exc:
        error = exc
        if cdp:
            print('FAIL VNC diagnostics: ' + json.dumps(cdp.evidence()),flush=True)
            if args.screenshots:
                await screenshot(cdp,Path(args.screenshots) / 'vnc-failure.png')
        raise
    finally:
        if args.screenshots:
            target = Path(args.screenshots) / 'vnc-evidence.json'
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps(report,indent=2) + '\n',encoding='utf-8')
        if cdp:
            await cdp.close()
        browser.terminate()
        try:
            browser.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser.kill()
            browser.wait(timeout=5)
        await cleanup_browser_profile(profile,primary_error=error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page',default=PAGE)
    parser.add_argument('--edge',default=EDGE)
    parser.add_argument('--screenshots',help='Output directory for actual canvas/viewer PNGs and JSON evidence')
    asyncio.run(run(parser.parse_args()))
