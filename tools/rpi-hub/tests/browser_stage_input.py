"""Exercise real browser mouse, touch and keyboard input against the local preview.

Uses its dev-only transport seam; this script refuses a remote/device URL.
Run the Stage preview first, then pass --page with its localhost URL.
"""

import argparse
import asyncio
import json
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

from browser_stage_layout import fixture
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


async def push(cdp, message):
    await cdp.evaluate("""(async () => {
      window.__stageInbox = [JSON.stringify(MESSAGE)];
      window.__stageDoorbell();
      await new Promise(resolve => setTimeout(resolve, 30));
    })()""".replace('MESSAGE', json.dumps(message)), await_promise=True)


async def state(cdp):
    return await cdp.evaluate("""(() => ({
      sent:window.__stageCommands.filter(m => m.type === 'ACTIVATE_SWITCH'),
      cards:[...document.querySelectorAll('.stage__switch')].map(el => {
        const rect = el.getBoundingClientRect();
        return {id:el.querySelector('.stage__switch-id').textContent.trim(),
          tag:el.tagName, disabled:el.disabled, active:el.getAttribute('aria-pressed'),
          busy:el.getAttribute('aria-busy'), x:rect.x+rect.width/2, y:rect.y+rect.height/2};
      }),
      error:document.querySelector('[role=alert]')?.textContent,
    }))()""")


async def mouse(cdp, card):
    for kind in ('mousePressed', 'mouseReleased'):
        await cdp.command('Input.dispatchMouseEvent', {
            'type':kind, 'x':card['x'], 'y':card['y'], 'button':'left', 'clickCount':1,
        })
    await asyncio.sleep(.04)


async def keyboard(cdp, index, key, code, virtual):
    await cdp.evaluate("document.querySelectorAll('.stage__switch')[%d].focus()" % index)
    for kind in ('keyDown', 'keyUp'):
        await cdp.command('Input.dispatchKeyEvent', {
            'type':kind, 'key':key, 'code':code, 'windowsVirtualKeyCode':virtual,
            **({'text':'\r' if key == 'Enter' else key} if kind == 'keyDown' else {}),
        })
    await asyncio.sleep(.04)


async def run(args):
    parsed = urlparse(args.page)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/stage-preview.html':
        raise ValueError('Only the local stage-preview.html fixture is allowed')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-input-cdp-')
    browser = subprocess.Popen([
        args.edge, '--headless=new', '--disable-gpu', '--no-first-run',
        '--remote-debugging-port=%d' % port, '--user-data-dir=' + profile.name,
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = CdpSession(await cdp_socket(port, args.page))
        await cdp.command('Page.navigate', {'url':args.page})
        for _ in range(100):
            if await cdp.evaluate("!!document.querySelector('.stage__switch')"):
                break
            await asyncio.sleep(.1)
        assert await cdp.evaluate("typeof window.__stageDoorbell === 'function'")
        await cdp.evaluate("window.__stageCommands=[]; window.__stageCommand=m=>{window.__stageCommands.push(m)}")
        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':1045, 'height':399, 'deviceScaleFactor':1, 'mobile':False,
        })
        await fixture(cdp, 'VOL')
        before = await state(cdp)
        assert all(c['tag'] == 'BUTTON' and not c['disabled'] for c in before['cards']), before
        target = before['cards'][1]
        assert target['active'] == 'false'
        await mouse(cdp, target)
        pending = await state(cdp)
        assert len(pending['sent']) == 1 and pending['sent'][0]['switch'] == '2', pending
        assert pending['sent'][0]['bank'] == 1 and pending['sent'][0]['slot'] == 1, pending
        assert pending['cards'][1]['busy'] == 'true' and pending['cards'][1]['active'] == 'false', pending
        assert all(c['disabled'] for c in pending['cards']), pending
        await mouse(cdp, target)
        assert len((await state(cdp))['sent']) == 1, 'Repeated click dispatched a second tap'
        await push(cdp, {'type':'EVENT', 'event':'binding_fired', 'switch':'2', 'action':'toggle_on'})
        assert (await state(cdp))['cards'][1]['active'] == 'true'
        await push(cdp, {'type':'ACK', 'id':pending['sent'][0]['id']})
        assert not (await state(cdp))['cards'][1]['disabled']
        print('PASS real mouse dispatch, pending guard and authoritative feedback', flush=True)

        for index, key, code, virtual in ((3, 'Enter', 'Enter', 13), (4, ' ', 'Space', 32)):
            count = len((await state(cdp))['sent'])
            await keyboard(cdp, index, key, code, virtual)
            current = await state(cdp)
            assert len(current['sent']) == count + 1, current
            expected = ('4', 'up')[index - 3]
            assert current['sent'][-1]['switch'] == expected, current
            await push(cdp, {'type':'ACK', 'id':current['sent'][-1]['id']})
        print('PASS Enter/Space native button activation and exact switch IDs', flush=True)

        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':375, 'height':667, 'deviceScaleFactor':1, 'mobile':False,
        })
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':True, 'maxTouchPoints':1})
        before = await state(cdp)
        target = before['cards'][0]
        await cdp.command('Input.dispatchTouchEvent', {
            'type':'touchStart', 'touchPoints':[{'x':target['x'], 'y':target['y']}],
        })
        await cdp.command('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
        await asyncio.sleep(.1)
        current = await state(cdp)
        assert len(current['sent']) == len(before['sent']) + 1, current
        assert current['sent'][-1]['switch'] == '1' and current['cards'][0]['active'] == 'false', current
        await push(cdp, {'type':'ERROR', 'id':current['sent'][-1]['id'], 'error':'busy'})
        await asyncio.sleep(.2)
        rejected = await state(cdp)
        assert rejected['error'] == 'Switch action not confirmed.', rejected
        assert len(rejected['sent']) == len(current['sent']) and not rejected['cards'][0]['disabled'], rejected
        assert rejected['cards'][0]['active'] == 'false', rejected
        print('PASS touch emits one tap; rejection neither repaints nor retries', flush=True)
    except BaseException as exc:
        error = exc
        raise
    finally:
        if cdp:
            await cdp.close()
        browser.terminate()
        try:
            browser.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser.kill()
            browser.wait(timeout=5)
        await cleanup_browser_profile(profile, primary_error=error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page', default='http://127.0.0.1:4738/stage-preview.html')
    parser.add_argument('--edge', default=EDGE)
    asyncio.run(run(parser.parse_args()))
