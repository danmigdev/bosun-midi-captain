"""Exercise the dedicated tuner with local feedback fixtures and real browser input."""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

from browser_stage_bank_picker import escape, mouse, wait_for
from browser_stage_control_styles import MAIN_CLOSE, probe as control_probe, same_close
from browser_stage_layout import fixture
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


async def context(cdp, **fields):
    fields = {"kemper_tuner": "on", "kemper_tuner_note": "F#",
              "kemper_tuner_deviance": 8192, **fields}
    await cdp.evaluate("""(() => {
      window.__stageInbox = [JSON.stringify({type:'CONTEXT', context:FIELDS})];
      window.__stageDoorbell();
    })()""".replace("FIELDS", json.dumps(fields)))
    await asyncio.sleep(.1)


GEOMETRY = r"""(() => {
  const d = document.querySelector('.tuner-screen');
  const rect = el => {
    const r = el.getBoundingClientRect();
    return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height};
  };
  return {modal:d.matches(':modal'), viewport:[innerWidth,innerHeight], dialog:rect(d),
    controls:['.tuner-screen__note','.tuner-screen__meter','.tuner-screen__flat',
      '.tuner-screen__sharp','.tuner-screen__close'].map(s => ({selector:s,...rect(d.querySelector(s))})),
    note:d.querySelector('.tuner-screen__note').textContent.trim(),
    direction:d.querySelector('.tuner-screen__instrument').dataset.direction,
    needle:d.querySelector('.tuner-screen__needle')?.getAttribute('transform') ?? null,
    text:d.textContent.replace(/\s/g, ''),
    noteSize:parseFloat(getComputedStyle(d.querySelector('.tuner-screen__note')).fontSize),
    closeLabel:d.querySelector('.tuner-screen__close').getAttribute('aria-label'),
    animations:document.getAnimations().filter(a => d.contains(a.effect?.target)).length};
})()"""


async def verify_close(cdp):
    main = await control_probe(cdp, MAIN_CLOSE)
    close = await control_probe(cdp, '.tuner-screen__close')
    same_close(close, main, 'Tuner X matches Stage X')
    for axis in ('x', 'y'):
        assert abs(close['box'][axis] - main['box'][axis]) <= 1, ('Tuner X position differs', close, main)


async def run(args):
    url = urlparse(args.page)
    if url.hostname not in ('127.0.0.1', 'localhost') or url.path != '/stage-preview.html':
        raise ValueError('Tuner fixture requires the isolated local Stage preview')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-tuner-cdp-')
    browser = subprocess.Popen([EDGE, '--headless=new', '--disable-gpu', '--no-first-run',
        f'--remote-debugging-port={port}', '--user-data-dir=' + profile.name, 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = CdpSession(await cdp_socket(port, args.page))
        await cdp.command('Page.navigate', {'url':args.page})
        await wait_for(cdp, "!!document.querySelector('.stage__switch')", 'Stage mount')
        await fixture(cdp, 'WAH', tuner=False)
        await cdp.evaluate("window.__tunerCommands = []; window.__stageCommand = m => { window.__tunerCommands.push(m); }")
        for width, height in ((1920,440), (800,480), (568,320), (375,667), (320,568)):
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
            await context(cdp, kemper_tuner='off')
            sizes = await cdp.evaluate("""['.stage__bank','.stage__rig','.stage__expression-label']
              .map(s => parseFloat(getComputedStyle(document.querySelector(s)).fontSize))""")
            assert max(sizes) - min(sizes) < .01, ('Header font sizes differ', sizes)
            await context(cdp, kemper_tuner='on', kemper_tuner_note='F#', kemper_tuner_deviance=8192)
            await wait_for(cdp, "!!document.querySelector('.tuner-screen[open]')", 'Automatic tuner')
            assert not await cdp.evaluate("!!document.querySelector('.stage__tuner')"), 'Compact tuner must not appear in Stage'
            await verify_close(cdp)
            assert await cdp.evaluate("document.activeElement === document.querySelector('.tuner-screen')"), 'Automatic opening must focus the dialog'
            assert not await cdp.evaluate("document.querySelector('.tuner-screen__close').matches(':focus-visible')"), 'Automatic opening highlighted the X'
            state = await cdp.evaluate(GEOMETRY)
            assert state['modal'] and state['direction'] == 'center', state
            assert state['dialog']['width'] == width and state['dialog']['height'] == height, state
            for item in state['controls']:
                assert item['width'] > 0 and item['height'] > 0, state
                assert item['x'] >= 0 and item['right'] <= width + 1, state
                assert item['y'] >= 0 and item['bottom'] <= height + 1, state
            note, meter = state['controls'][:2]
            assert min(note['right'], meter['right']) <= max(note['x'], meter['x']) + 1 or \
                   min(note['bottom'], meter['bottom']) <= max(note['y'], meter['y']) + 1, state
            flat, sharp, close = state['controls'][2:]
            assert flat['right'] <= meter['x'] and sharp['x'] >= meter['right'], state
            assert abs((flat['y'] + flat['bottom']) / 2 - (meter['y'] + meter['bottom']) / 2) < 1, state
            assert abs((sharp['y'] + sharp['bottom']) / 2 - (meter['y'] + meter['bottom']) / 2) < 1, state
            assert state['text'] == 'F♯♭♯' and state['closeLabel'] == 'Close tuner', state
            assert state['noteSize'] >= (300 if width == 1920 else 140), state
            assert min(close['right'], meter['right']) <= max(close['x'], meter['x']) + 1 or \
                   min(close['bottom'], meter['bottom']) <= max(close['y'], meter['y']) + 1, state
            assert min(close['right'], note['right']) <= max(close['x'], note['x']) + 1 or \
                   min(close['bottom'], note['bottom']) <= max(close['y'], note['y']) + 1, state
            assert state['animations'] == 0, 'Tuner must be quiet on the Pi when pitch is steady'
            if args.screenshots:
                target = Path(args.screenshots) / f'tuner-{width}x{height}.png'
                target.parent.mkdir(parents=True, exist_ok=True)
                shot = await cdp.command('Page.captureScreenshot', {'format':'png'})
                target.write_bytes(base64.b64decode(shot['data']))
            print(f'PASS fullscreen tuner {width}x{height}; X style/position matches Stage; equal BANK/RIG/WAH font sizes', flush=True)

        for width, height in ((1920,440), (375,667)):
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width,'height':height,'deviceScaleFactor':1,'mobile':False})
            for corners in (0, 2, .75):
                await cdp.evaluate("document.querySelector('.stage').style.setProperty('--stage-corner-scale', " + json.dumps(str(corners)) + ")")
                await asyncio.sleep(.1)
                await verify_close(cdp)
            await cdp.evaluate("document.documentElement.style.fontSize = '24px'")
            await asyncio.sleep(.1)
            await verify_close(cdp)
            await cdp.evaluate("document.documentElement.style.fontSize = '16px'")
        for kind in ('keyDown', 'keyUp'):
            await cdp.command('Input.dispatchKeyEvent', {'type':kind,'key':'Tab','code':'Tab','windowsVirtualKeyCode':9})
        assert await cdp.evaluate("document.querySelector('.tuner-screen__close').matches(':focus-visible')"), 'Tab must visibly focus the X'
        print('PASS X follows changed corners and header size; keyboard focus stays visible', flush=True)

        for value, direction in ((0,'flat'), (7000,'flat'), (8192,'center'), (9000,'sharp'), (16383,'sharp')):
            await context(cdp, kemper_tuner_deviance=value)
            state = await cdp.evaluate(GEOMETRY)
            assert state['direction'] == direction and state['needle'], state
        await context(cdp, kemper_tuner_deviance=None, kemper_tuner_note=None)
        state = await cdp.evaluate(GEOMETRY)
        assert state['direction'] == 'waiting' and state['needle'] is None, state

        await escape(cdp)
        await wait_for(cdp, "!document.querySelector('.tuner-screen')", 'Back to Stage')
        await context(cdp, kemper_tuner_note='A', kemper_tuner_deviance=8192)
        assert not await cdp.evaluate("!!document.querySelector('.tuner-screen')"), 'Dismissed tuner reopened on feedback'
        assert not await cdp.evaluate("!!document.querySelector('.stage__tuner')"), 'Compact tuner appeared after dismissal'
        await context(cdp, kemper_tuner='off')
        await context(cdp, kemper_tuner='on')
        await wait_for(cdp, "!!document.querySelector('.tuner-screen[open]')", 'Reopen on next activation')
        await mouse(cdp, '.tuner-screen__close')
        await wait_for(cdp, "!document.querySelector('.tuner-screen')", 'Close tuner with X')
        await context(cdp, kemper_tuner_note='G')
        assert not await cdp.evaluate("!!document.querySelector('.tuner-screen, .stage__tuner')"), 'Tuner appeared after X'
        await context(cdp, kemper_tuner='off')
        await context(cdp, kemper_tuner='on')
        await wait_for(cdp, "!!document.querySelector('.tuner-screen[open]')", 'Next activation after X')
        await context(cdp, kemper_tuner='off')
        await wait_for(cdp, "!document.querySelector('.tuner-screen')", 'Automatic Stage return')
        await context(cdp, kemper_tuner='on')
        await wait_for(cdp, "!!document.querySelector('.tuner-screen[open]')", 'Next tuner activation')
        commands = await cdp.evaluate('window.__tunerCommands')
        assert all(c['type'].startswith('GET_') or c['type'] == 'LIST_PATCHES' for c in commands), commands
        print('PASS live pitch directions, absent feedback, Escape, X, reopen and automatic return; no device mutations', flush=True)
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
    parser.add_argument('--page', default='http://127.0.0.1:4732/stage-preview.html')
    parser.add_argument('--screenshots')
    asyncio.run(run(parser.parse_args()))
