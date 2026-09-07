"""Check the actual fixed 1920x440 Stage iframe in the HDMI display preview.

Serve editor/dist-stage/ on localhost before running. The default test runs
the production kiosk against a small local WebSocket firmware fixture; no
hardware connection is made. Explicit --live permits only the configured Pi
and only passive opening/closing of BANK and appearance dialogs. It checks
the complete WebSocket send trace for device mutations in both modes.

For --live runs, set BOSUN_STAGE_HOST to the expected Pi hostname or IP.
The default trusted host is bosun-hub; the URL must still meet all live safeguards.
"""

import os
import argparse
import asyncio
import base64
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from websockets.asyncio.server import serve

from browser_stage_bank_picker import GEOMETRY as BANK_GEOMETRY
from browser_stage_bank_picker import capture, mouse, verify_mode_geometry, wait_for
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


VIEWPORTS = ((1920, 1080), (1366, 768), (375, 667))
READ_COMMANDS = {'GET_DEVICE_INFO', 'GET_PATCH', 'GET_CONTEXT', 'GET_GLOBAL', 'LIST_PATCHES'}

GEOMETRY = r"""(() => {
  const frame = document.querySelector('#stage-frame');
  const workspace = document.querySelector('#display-workspace');
  const rect = el => {
    const r = el.getBoundingClientRect();
    return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};
  };
  const child = frame.contentWindow;
  return {
    viewport:{width:innerWidth,height:innerHeight}, frame:rect(frame),
    shell:rect(document.querySelector('#display-shell')),
    workspace:{...rect(workspace),clientWidth:workspace.clientWidth,clientHeight:workspace.clientHeight,
      scrollWidth:workspace.scrollWidth,scrollHeight:workspace.scrollHeight,
      scrollLeft:workspace.scrollLeft,scrollTop:workspace.scrollTop},
    inner:{width:child.innerWidth,height:child.innerHeight,timeOrigin:child.performance.timeOrigin,
      identity:child.__hdmiCheckIdentity,href:child.location.href,
      scrollWidth:child.document.documentElement.scrollWidth,
      scrollHeight:child.document.documentElement.scrollHeight},
    fit:document.querySelector('#fit-button').getAttribute('aria-pressed'),
    native:document.querySelector('#native-button').getAttribute('aria-pressed'),
    status:document.querySelector('#scale-status').textContent,
    documentWidth:document.documentElement.scrollWidth,
    documentHeight:document.documentElement.scrollHeight,
  };
})()"""


class PreviewSession(CdpSession):
    """Retain every outgoing type independently of the bounded diagnostics ring."""

    def __init__(self, websocket):
        super().__init__(websocket)
        self.sent = []

    def observe(self, message):
        super().observe(message)
        if message.get('method') == 'Network.webSocketFrameSent':
            payload = message['params']['response'].get('payloadData', '')
            for line in payload.splitlines():
                try:
                    self.sent.append(json.loads(line))
                except json.JSONDecodeError:
                    self.sent.append({'type':'INVALID_JSON', 'payload':line})


class InnerFrame:
    """Evaluate existing Stage checks in the iframe's own JS realm."""

    def __init__(self, cdp):
        self.cdp = cdp

    async def evaluate(self, expression, await_promise=False):
        return await self.cdp.evaluate(
            'document.querySelector("#stage-frame").contentWindow.eval(' + json.dumps(expression) + ')',
            await_promise=await_promise,
        )


class Fixture:
    """A real local WebSocket, restricted to answering read requests."""

    def __init__(self):
        self.connections = 0
        self.sent = []

    async def handle(self, ws):
        self.connections += 1
        await ws.send(json.dumps({'type':'HUB', 'link':'up'}))
        async for raw in ws:
            command = json.loads(raw)
            self.sent.append(command)
            kind = command['type']
            reply = {'id':command.get('id')}
            if kind == 'GET_DEVICE_INFO':
                reply.update(type='DEVICE_INFO', fw='0.6.5-native', device='midi_captain_10',
                             current={'bank':1,'slot':2}, profile='hdmi-fixture', stage_input=True,
                             preset_navigation={'switches':{'A':1,'B':2,'C':3,'D':4,'down':5}})
            elif kind == 'LIST_PATCHES':
                names = ['ACOUSTIC', 'CLEAN', 'CRUNCH', 'HEAVY', 'LEAD']
                reply.update(type='PATCH_LIST', profile='hdmi-fixture', patches=[
                    {'bank':bank,'slot':slot,'name':name}
                    for bank in range(1, 100) for slot, name in enumerate(names, 1)
                ])
            elif kind == 'GET_PATCH':
                colors = ['#ef4444','#10b981','#3b82f6','#f59e0b','#a855f7']
                reply.update(type='PATCH', bank=command.get('bank', 1), slot=command.get('slot', 2),
                             patch={'name':'CLEAN HDMI FIXTURE', 'bindings':[
                                 {'switch':switch,'mode':'latched','label':label,'actions':{},'led':{'on':color}}
                                 for switch, label, color in zip(
                                     ['1','2','3','4','up'], ['DRIVE','CHORUS','FLANG','DELAY','BOOST'], colors)
                             ]})
            elif kind == 'GET_CONTEXT':
                reply.update(type='CONTEXT', context={'bank':1,'slot':2,
                    'kemper_rig_name':'CLEAN HDMI FIXTURE', 'expression_mode':'WAH', 'kemper_tuner':'off'})
            elif kind == 'GET_GLOBAL':
                reply.update(type='GLOBAL', device={})
            else:
                reply.update(type='ERROR', error='HDMI preview fixture permits reads only')
            await ws.send(json.dumps(reply))


async def inner_mouse(cdp, selector):
    point = await cdp.evaluate("""(() => {
      const frame = document.querySelector('#stage-frame');
      const target = frame.contentDocument.querySelector(SELECTOR);
      if (!target) throw new Error('Missing iframe target: ' + SELECTOR);
      const outer = frame.getBoundingClientRect();
      const inner = target.getBoundingClientRect();
      return {x:outer.x+(inner.x+inner.width/2)*outer.width/frame.contentWindow.innerWidth,
        y:outer.y+(inner.y+inner.height/2)*outer.height/frame.contentWindow.innerHeight};
    })()""".replace('SELECTOR', json.dumps(selector)))
    viewport = await cdp.evaluate('({width:innerWidth,height:innerHeight})')
    assert 0 <= point['x'] <= viewport['width'] and 0 <= point['y'] <= viewport['height'], point
    for kind in ('mousePressed', 'mouseReleased'):
        await cdp.command('Input.dispatchMouseEvent', {
            'type':kind, **point, 'button':'left', 'clickCount':1,
        })
    await asyncio.sleep(.08)


def verify_fixed(state, identity):
    assert state['inner']['width'] == 1920 and state['inner']['height'] == 440, state
    assert state['inner']['identity'] == identity, ('Stage iframe reloaded', state)
    assert state['inner']['scrollWidth'] <= 1920 and state['inner']['scrollHeight'] <= 440, state
    assert abs(state['frame']['width'] / 1920 - state['frame']['height'] / 440) < .001, state
    assert state['documentWidth'] <= state['viewport']['width'] + 1, state
    assert state['documentHeight'] <= state['viewport']['height'] + 1, state


def verify_fit(state, identity):
    verify_fixed(state, identity)
    assert state['fit'] == 'true' and state['native'] == 'false', state
    assert 0 < state['frame']['width'] <= 1921, state
    for axis in ('x', 'y'):
        assert state['shell'][axis] >= state['workspace'][axis] - 1, state
    for axis in ('right', 'bottom'):
        assert state['shell'][axis] <= state['workspace'][axis] + 1, state
    assert state['workspace']['scrollWidth'] <= state['workspace']['clientWidth'] + 1, state
    assert state['workspace']['scrollHeight'] <= state['workspace']['clientHeight'] + 1, state


async def dialogs(cdp, inner):
    # A real pointer action transfers browser input focus into the iframe;
    # its modal then restores focus to BANK for native keyboard activation.
    await inner_mouse(cdp, '.stage__bank-readout')
    await wait_for(inner, "document.querySelectorAll('.bank-picker__bank:not(:disabled)').length > 0", 'BANK pointer open')
    await inner_mouse(cdp, '.bank-picker__close')
    await wait_for(inner, "!document.querySelector('.bank-picker')", 'BANK pointer close')
    assert await inner.evaluate("document.activeElement === document.querySelector('.stage__bank-readout')"), await inner.evaluate('document.activeElement.outerHTML')
    assert await cdp.evaluate('document.activeElement === document.querySelector("#stage-frame")'), await cdp.evaluate('document.activeElement.outerHTML')
    await cdp.command('Input.dispatchKeyEvent', {
        'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,
        'text':'\r','unmodifiedText':'\r',
    })
    await cdp.command('Input.dispatchKeyEvent', {
        'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,
    })
    await wait_for(inner, "document.querySelectorAll('.bank-picker__bank:not(:disabled)').length > 0", 'BANK keyboard open')
    state = await inner.evaluate(BANK_GEOMETRY)
    assert state['modal'], state
    assert state['dialog'] == {'x':0,'y':0,'width':1920,'height':440,'right':1920,'bottom':440}, state
    verify_mode_geometry(state)
    await inner_mouse(cdp, '.bank-picker__close')
    await wait_for(inner, "!document.querySelector('.bank-picker')", 'BANK close inside scaled iframe')
    assert await inner.evaluate("document.activeElement === document.querySelector('.stage__bank-readout')")
    await inner_mouse(cdp, '.stage__icon-btn[aria-label="Stage appearance"]')
    await wait_for(inner, "document.querySelectorAll('.theme-panel [role=combobox]').length === 7", 'Appearance open')
    await inner_mouse(cdp, '.theme-panel__close')
    await wait_for(inner, "!document.querySelector('.theme-panel')", 'Appearance close')


async def run(args):
    parsed = urlparse(args.page)
    if parsed.scheme != 'http' or parsed.path != '/display-preview.html':
        raise ValueError('Only HTTP /display-preview.html is supported')
    if args.live:
        if parsed.hostname != os.environ.get("BOSUN_STAGE_HOST", "bosun-hub") or parsed.port != 8080 or 'ws' in dict(parse_qsl(parsed.query)):
            raise ValueError('--live permits only the configured Pi on HTTP 8080 without a ws override')
    elif parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('Without --live only localhost is permitted')

    fixture = Fixture()
    server = None
    if not args.live:
        server = await serve(fixture.handle, '127.0.0.1', 0)
        ws_port = server.sockets[0].getsockname()[1]
        params = dict(parse_qsl(parsed.query))
        params['ws'] = f'ws://127.0.0.1:{ws_port}/'
        parsed = parsed._replace(query=urlencode(params))
    page = urlunparse(parsed)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-hdmi-preview-cdp-')
    browser = subprocess.Popen([
        args.edge, '--headless=new', '--disable-gpu', '--no-first-run',
        f'--remote-debugging-port={port}', '--user-data-dir=' + profile.name, 'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = PreviewSession(await cdp_socket(port, page))
        await cdp.command('Network.enable')
        await cdp.command('Page.navigate', {'url':page})
        inner = InnerFrame(cdp)
        await wait_for(cdp, '!!document.querySelector("#stage-frame")?.contentDocument?.querySelector(".stage__bank-readout")', 'Stage iframe loaded')
        if args.live:
            # Real profiles can leave navigation slots empty; the current rig
            # and connected BANK control establish readiness without imposing
            # the synthetic fixture's five named rigs on the user's profile.
            await wait_for(inner, """(() => {
              const bank = document.querySelector('.stage__bank-readout');
              const title = document.querySelector('.stage__rig-name')?.textContent.trim();
              return bank && !bank.disabled && title && !/^[-—]+$/.test(title)
                && document.querySelectorAll('.stage__switch').length === 10;
            })()""", 'Connected current live rig')
        else:
            await wait_for(inner, "document.querySelectorAll('.stage__pedal-row:last-child .stage__switch-label').length === 5 && [...document.querySelectorAll('.stage__pedal-row:last-child .stage__switch-label')].every(el => el.textContent.trim() && el.textContent.trim() !== '-')", 'Fixture rig inventory')
        identity = await inner.evaluate('window.__hdmiCheckIdentity = String(performance.timeOrigin) + Math.random()')
        preferences = await inner.evaluate('JSON.stringify({...localStorage})')
        initial_sockets = dict(cdp.sockets)
        assert len(initial_sockets) == 1, initial_sockets

        for width, height in VIEWPORTS:
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width,'height':height,'deviceScaleFactor':1,'mobile':False,
            })
            await asyncio.sleep(.12)
            state = await cdp.evaluate(GEOMETRY)
            verify_fit(state, identity)
            await dialogs(cdp, inner)
            if args.screenshot:
                target = Path(args.screenshot)
                await capture(cdp, target.with_name(f'{target.stem}-{width}x{height}{target.suffix}'))
            print(f'PASS {"LIVE " if args.live else ""}outer {width}x{height}: inner viewport 1920x440; fit {state["frame"]["width"]/1920:.3f}; BANK header/X geometry and scaled mouse/keyboard input', flush=True)

        await mouse(cdp, '#native-button')
        await asyncio.sleep(.1)
        state = await cdp.evaluate(GEOMETRY)
        verify_fixed(state, identity)
        assert state['native'] == 'true' and state['fit'] == 'false', state
        assert abs(state['frame']['width'] - 1920) <= 1 and abs(state['frame']['height'] - 440) <= 1, state
        assert state['workspace']['scrollWidth'] > state['workspace']['clientWidth'], state
        assert state['workspace']['scrollHeight'] > state['workspace']['clientHeight'], state
        workspace = state['workspace']
        # Aim at the outer canvas padding, not the iframe or the native
        # horizontal scrollbar (which can consume a wheel without scrolling).
        wheel_point = {'x':workspace['x'] + 3,
                       'y':workspace['y'] + workspace['clientHeight']/2}
        await cdp.command('Input.dispatchMouseEvent', {'type':'mouseMoved', **wheel_point})
        await cdp.command('Input.dispatchMouseEvent', {
            'type':'mouseWheel', **wheel_point, 'deltaX':1000,'deltaY':300,
        })
        await wait_for(cdp, "document.querySelector('#display-workspace').scrollLeft > 0 && document.querySelector('#display-workspace').scrollTop > 0", 'Native wheel scroll in both axes')
        scrolled = await cdp.evaluate(GEOMETRY)
        assert scrolled['workspace']['scrollLeft'] > 0 and scrolled['workspace']['scrollTop'] > 0, scrolled
        await mouse(cdp, '#fit-button')
        await asyncio.sleep(.1)
        state = await cdp.evaluate(GEOMETRY)
        verify_fit(state, identity)
        assert state['workspace']['scrollLeft'] == 0 and state['workspace']['scrollTop'] == 0, state
        print('PASS native 1:1 pixels, real horizontal/vertical wheel scroll, return to fit without reloading Stage', flush=True)

        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':2048,'height':760,'deviceScaleFactor':1,'mobile':False,
        })
        await asyncio.sleep(.1)
        await mouse(cdp, '#native-button')
        await asyncio.sleep(.1)
        state = await cdp.evaluate(GEOMETRY)
        verify_fixed(state, identity)
        if args.screenshot:
            target = Path(args.screenshot)
            await capture(cdp, target)
            screen = state['frame']
            shot = await cdp.command('Page.captureScreenshot', {'format':'png',
                'clip':{'x':screen['x'],'y':screen['y'],'width':1920,'height':440,'scale':1},
                'captureBeyondViewport':False})
            target.with_name(target.stem + '-panel-1920x440' + target.suffix).write_bytes(base64.b64decode(shot['data']))
        await mouse(cdp, '#fit-button')
        if await cdp.evaluate('!document.querySelector("#fullscreen-button").hidden'):
            await mouse(cdp, '#fullscreen-button')
            await wait_for(cdp, 'document.fullscreenElement === document.documentElement', 'Outer preview fullscreen')
            await asyncio.sleep(.1)
            verify_fit(await cdp.evaluate(GEOMETRY), identity)
            await mouse(cdp, '#fullscreen-button')
            await wait_for(cdp, '!document.fullscreenElement', 'Exit outer preview fullscreen')
            print('PASS fullscreen belongs to outer preview; iframe retains its fixed 1920x440 viewport', flush=True)

        await cdp.evaluate('true')  # Drain final CDP network observations.
        assert cdp.sockets == initial_sockets, ('Extra/reopened Stage socket', cdp.sockets)
        assert await inner.evaluate('JSON.stringify({...localStorage})') == preferences, 'Preferences changed'
        forbidden = [command for command in cdp.sent if command.get('type') not in READ_COMMANDS]
        assert not forbidden, forbidden
        assert {'GET_DEVICE_INFO', 'LIST_PATCHES'} <= {command.get('type') for command in cdp.sent}, cdp.sent
        if not args.live:
            assert fixture.connections == 1, fixture.connections
            assert not [command for command in fixture.sent if command.get('type') not in READ_COMMANDS], fixture.sent
        print(f'PASS one Stage WebSocket, unchanged iframe identity/preferences and zero device mutations ({len(cdp.sent)} read requests)', flush=True)
    except BaseException as exc:
        error = exc
        if cdp:
            print('FAIL diagnostics: ' + json.dumps(await cdp.evaluate("""(() => {
              const frame = document.querySelector('#stage-frame');
              return {url:frame?.src,stage:frame?.contentDocument?.body?.innerText,sockets:SOCKETS,sent:SENT};
            })()""".replace('SOCKETS', json.dumps(cdp.sockets)).replace('SENT', json.dumps(cdp.sent))), ensure_ascii=False), flush=True)
            if args.screenshot:
                target = Path(args.screenshot)
                await capture(cdp, target.with_name(target.stem + '-failure' + target.suffix))
        raise
    finally:
        if cdp:
            await cdp.close()
        if server:
            server.close()
            await server.wait_closed()
        browser.terminate()
        try:
            browser.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser.kill()
            browser.wait(timeout=5)
        await cleanup_browser_profile(profile, primary_error=error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page', default='http://127.0.0.1:4734/display-preview.html')
    parser.add_argument('--edge', default=EDGE)
    parser.add_argument('--screenshot', help='Optional PNG prefix; also saves outer viewports and a native 1920x440 panel')
    parser.add_argument('--live', action='store_true', help='Passive configured-Pi inspection only; no bank/rig/setting changes')
    asyncio.run(run(parser.parse_args()))
