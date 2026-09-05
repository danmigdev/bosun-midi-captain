"""Real Chromium geometry regression, no MIDI/hardware commands.

Run the dev-only preview with `npm run stage:preview` in editor/, then:
    python tools/rpi-hub/tests/browser_stage_layout.py
`--page http://bosun-hub:8080/ --live` verifies deployed geometry passively.
"""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import socket
import subprocess
import tempfile

from browser_stage_transition import (
    EDGE, CdpSession, cdp_socket, cleanup_browser_profile,
)


GEOMETRY = r"""(() => {
  const rect = el => {
    const r = el.getBoundingClientRect();
    return {left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
  };
  const header = document.querySelector('.stage__header');
  const stage = header.closest('.stage');
  const separator = document.querySelector('.stage__separator');
  const expression = document.querySelector('.stage__expression');
  return {
    width:innerWidth, height:innerHeight, stage:rect(stage), header:rect(header),
    headerPaddingRight:parseFloat(getComputedStyle(header).paddingRight),
    separator:separator ? rect(separator) : null,
    rig:rect(document.querySelector('.stage__rig-name')),
    rigText:document.querySelector('.stage__rig-name')?.textContent.trim(),
    meta:rect(document.querySelector('.stage__meta')),
    bank:document.querySelector('.stage__bank') ? rect(document.querySelector('.stage__bank')) : null,
    bankText:document.querySelector('.stage__bank')?.textContent.trim(),
    bpm:document.querySelector('.stage__bpm') ? rect(document.querySelector('.stage__bpm')) : null,
    bpmText:document.querySelector('.stage__bpm')?.textContent.trim(),
    expression:expression ? rect(expression) : null,
    expressionText:expression?.textContent.trim(),
    cards:[...document.querySelectorAll('.stage__switch')].map(rect),
    textFrames:[...document.querySelectorAll(
      '.stage__rig-name,.stage__bank,.stage__switch-label')].map(el => ({
        text:el.textContent.trim(), width:el.clientWidth,
        textWidth:el.firstElementChild.scrollWidth,
        scrolling:el.firstElementChild.classList.contains('stage__marquee-active'),
      })),
  };
})()"""

VIEWPORTS = ((1045, 399), (800, 480), (640, 360), (568, 320),
             (480, 800), (375, 667), (320, 568), (834, 1112), (1920, 1080))


async def fixture(cdp, mode, *, title='CLEAN', bpm=None):
    # Use the preview's normal firmware-message bus; no fake DOM/CSS geometry.
    await cdp.evaluate("""(async () => {
      document.querySelector('#controls')?.setAttribute('hidden', '');
      document.querySelector('#toggleBtn')?.setAttribute('hidden', '');
      const push = async msg => {
        window.__stageInbox = [JSON.stringify(msg)];
        window.__stageDoorbell();
        await new Promise(resolve => setTimeout(resolve, 30));
      };
      const labels = ['-', '-', 'FLANG', '-', 'BOOST',
                      'ACOUSTIC', 'CLEAN', 'CRUNCH', 'HEAVY', 'LEAD'];
      await push({type:'PATCH', bank:1, slot:1, patch:{name:'CLEAN', bindings:
        ['1','2','3','4','up','A','B','C','D','down'].map((switchId, index) => ({
          switch:switchId, mode:'latched', label:labels[index], actions:{}, led:{on:'#3b82f6'}
        }))}});
      await push({type:'CONTEXT', context:{bank:1, slot:1, kemper_rig_name:TITLE,
        kemper_bpm:BPM, expression_mode:MODE}});
      for (const sw of ['3', 'B', 'down'])
        await push({type:'EVENT', event:'binding_fired', switch:sw, action:'toggle_on'});
      return true;
    })()""".replace("MODE", json.dumps(mode))
            .replace("TITLE", json.dumps(title)).replace("BPM", json.dumps(bpm)),
        await_promise=True)


async def wait_live_bootstrap(cdp, timeout=12):
    deadline = asyncio.get_running_loop().time() + timeout
    labels = []
    while asyncio.get_running_loop().time() < deadline:
        labels = await cdp.evaluate("""(() => {
          const lower = [...document.querySelectorAll('.stage__pedal-row')].at(-1);
          return lower ? [...lower.querySelectorAll('.stage__switch-label')]
            .map(el => el.textContent.trim()) : [];
        })()""")
        if len(labels) == 5 and all(label and label not in ('-', '---') for label in labels):
            return labels
        await asyncio.sleep(.1)
    raise AssertionError('Live Stage bootstrap incomplete after %ss: %r' % (timeout, labels))


async def animation_phase(cdp, phase, *, expect_active=None):
    animations = await cdp.evaluate("""(() => {
      const active = [...document.querySelectorAll('.stage__switch--active')];
      const pulses = document.getAnimations().filter(a =>
        a.effect?.target?.matches('.stage__switch--active') &&
        (a.animationName || '').includes('stage-switch-pulse'));
      for (const a of pulses) { a.pause(); a.currentTime=PHASE; }
      return {active:active.length, count:pulses.length, names:pulses.map(a => a.animationName)};
    })()""".replace('PHASE', str(phase)))
    # Svelte prefixes @keyframes names. A test which silently matched zero
    # animations never exercised either extreme of the active border pulse.
    assert animations['count'] == animations['active'], animations
    if expect_active is not None:
        assert animations['active'] == expect_active, animations
    return animations


def assert_geometry(state, *, expect_mode=None, expect_title=None, expect_bpm=None):
    width, height = state['width'], state['height']
    assert abs(state['stage']['height'] - height) < 1, (
        'Stage overflows viewport: ' + json.dumps(state))
    assert len(state['cards']) == 10, state
    for card in state['cards']:
        assert card['left'] >= 1 and card['right'] <= width - 1, state
        assert card['bottom'] <= height - 1, ('Bottom border clipped: ' + json.dumps(state))
        assert card['top'] >= state['header']['bottom'] + 1, state
        assert card['height'] > 30, state
    separator = state['separator']
    assert separator is not None, 'Missing title separator'
    assert separator['height'] >= 2, ('Title separator too thin: ' + json.dumps(state))
    assert abs(separator['bottom'] - state['header']['bottom']) <= 1, state
    assert separator['left'] >= state['header']['left'] - .5, state
    assert separator['right'] <= state['header']['right'] + .5, state
    assert separator['width'] >= state['header']['width'] * .95, state
    badge = state['expression']
    assert badge is not None, 'Missing expression pedal indicator'
    assert badge['bottom'] <= state['header']['bottom'], state
    assert abs(state['header']['right'] - badge['right'] - state['headerPaddingRight']) <= 1, state
    header = state['header']
    elements = [state['rig'], state['meta'], badge]
    for element in elements:
        assert element['left'] >= header['left'] - .5, state
        assert element['right'] <= header['right'] + .5, state
        assert element['top'] >= header['top'] - .5, state
        assert element['bottom'] <= header['bottom'] + .5, state
    for index, left in enumerate(elements):
        for right in elements[index + 1:]:
            overlap_x = min(left['right'], right['right']) - max(left['left'], right['left'])
            overlap_y = min(left['bottom'], right['bottom']) - max(left['top'], right['top'])
            assert overlap_x <= .5 or overlap_y <= .5, ('Header elements overlap: ' + json.dumps(state))
    if state['bpm'] is not None:
        assert state['bpm']['left'] >= state['meta']['left'] - .5, state
        assert state['bpm']['right'] <= state['meta']['right'] + .5, state
    if state['bank'] is not None:
        assert state['bank']['width'] >= 30, ('Bank/rig marquee collapsed: ' + json.dumps(state))
        assert state['bank']['left'] >= state['meta']['left'] - .5, state
        assert state['bank']['right'] <= state['meta']['right'] + .5, state
    if expect_mode is not None:
        assert state['expressionText'] == expect_mode, state
    if expect_title is not None:
        assert state['rigText'] == expect_title, state
    if expect_bpm is not None:
        assert state['bpmText'] == '%s BPM' % expect_bpm, state


async def run(args):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-layout-cdp-')
    browser = subprocess.Popen([
        args.edge, '--headless=new', '--disable-gpu', '--no-first-run',
        '--remote-debugging-port=%d' % port, '--user-data-dir=' + profile.name,
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = CdpSession(await cdp_socket(port, args.page))
        await cdp.command('Page.navigate', {'url': args.page})
        for _ in range(150):
            if await cdp.evaluate("!!document.querySelector('.stage__switch')"):
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError('Stage did not mount')
        await cdp.command('Emulation.setEmulatedMedia', {'features':[
            {'name':'prefers-reduced-motion', 'value':'no-preference'}]})
        if args.live:
            labels = await wait_live_bootstrap(cdp)
            print('PASS live bootstrap ' + json.dumps(labels), flush=True)
            cases = [('live', None, None)]
        else:
            cases = [('short', 'CLEAN', None),
                     ('long-title', 'Vintage Deluxe Reverb Ultra Mega Long Rig Name', 120),
                     ('long-title-high-bpm', 'CRUNCH BOOST DELAY REVERB LEAD', 250)]
        for case, title, bpm in cases:
            if not args.live:
                await fixture(cdp, 'VOL', title=title, bpm=bpm)
            for width, height in VIEWPORTS:
                await cdp.command('Emulation.setDeviceMetricsOverride', {
                    'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
                })
                await asyncio.sleep(.1)
                # Check the active animation at its extremes, including an
                # active bottom-right card: its border must never be cropped.
                for phase in (0, 1300):
                    await animation_phase(cdp, phase, expect_active=None if args.live else 3)
                    state = await cdp.evaluate(GEOMETRY)
                    assert_geometry(state, expect_mode=None if args.live else 'VOL',
                                    expect_title=title, expect_bpm=bpm)
                print('PASS geometry %s %dx%d (both animation extremes)' % (case,width,height), flush=True)
        if not args.live:
            # The native editor may use 150% UI text with a nearly square
            # landscape viewport. Height-only type sizes made even CLEAN and
            # BANK/RIG scroll. Measure real glyph widths, not just clip boxes.
            await fixture(cdp, 'VOL')
            await cdp.evaluate("document.documentElement.style.fontSize = '24px'")
            for width, height in ((1759, 1408), (1899, 1520), (1920, 1080), (800, 480)):
                await cdp.command('Emulation.setDeviceMetricsOverride', {
                    'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
                })
                await asyncio.sleep(.1)
                state = await cdp.evaluate(GEOMETRY)
                assert_geometry(state, expect_mode='VOL', expect_title='CLEAN')
                for frame in state['textFrames']:
                    assert frame['textWidth'] <= frame['width'] + 2, (
                        'Ordinary saved name clipped at enlarged UI scale: ' + json.dumps(state))
                    assert not frame['scrolling'], frame
                if args.screenshot and width == 1759:
                    shot = await cdp.command('Page.captureScreenshot', {'format':'png'})
                    path = Path(args.screenshot)
                    path.with_name(path.stem + '-large-ui' + path.suffix).write_bytes(
                        base64.b64decode(shot['data']))
                print('PASS readable names at 150%% UI scale %dx%d' % (width, height), flush=True)
            await cdp.evaluate("document.documentElement.style.removeProperty('font-size')")
            for mode, expected in [('WAH','WAH'), ('','---')]:
                await fixture(cdp, mode)
                assert_geometry(await cdp.evaluate(GEOMETRY), expect_mode=expected)
                print('PASS expression ' + expected, flush=True)
        if args.screenshot:
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':1045, 'height':399, 'deviceScaleFactor':1, 'mobile':False,
            })
            await asyncio.sleep(.1)
            shot = await cdp.command('Page.captureScreenshot', {'format':'png'})
            Path(args.screenshot).write_bytes(base64.b64decode(shot['data']))
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
    parser.add_argument('--edge', default=EDGE)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--screenshot')
    asyncio.run(run(parser.parse_args()))
