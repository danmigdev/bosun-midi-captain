"""Real Chromium input/geometry checks for every Stage appearance font menu.

The default URL is the isolated local Stage preview. It exercises mouse,
keyboard and touch selection, browser preference persistence/reset, responsive
option height, scrolling and viewport placement without connecting hardware.
Explicit --live permits only the configured Pi: open, inspect and close menus
without selecting an option or changing appearance/device state.

For --live runs, set BOSUN_STAGE_HOST to the expected Pi hostname or IP.
The default trusted host is bosun-hub; the URL must still meet all live safeguards.
"""

import os
import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

from browser_stage_bank_picker import PEER, capture, escape, key, mouse, touch, wait_for
from browser_stage_layout import fixture
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


LABELS = ('Default font', 'Rig name font', 'Bank / Rig font', 'BPM font',
          'Tuner font', 'Switch label font', 'Switch ID font', 'VOL / WAH font')
VIEWPORTS = ((1045, 399), (320, 568), (568, 320), (375, 667))
SECTIONS = (('rigName', 'rig-name'), ('bank', 'bank'), ('bpm', 'bpm'),
            ('tuner', 'tuner'), ('switchLabel', 'switch-label'), ('switchId', 'switch-id'), ('expression', 'expression'))
STORAGE = 'BOSUN_STAGE_THEME'


def trigger(label):
    return '.theme-panel [role="combobox"][aria-label=' + json.dumps(label) + ']'


def option(value):
    return '.theme-panel [role="listbox"] [role="option"][data-value=' + json.dumps(value) + ']'


GEOMETRY = r"""(() => {
  const selected = document.querySelector(SELECTOR);
  const list = selected && document.getElementById(selected.getAttribute('aria-controls'));
  const rect = el => {
    const r = el.getBoundingClientRect();
    return {x:r.x, y:r.y, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
  };
  if (!selected) return null;
  const body = document.querySelector('.theme-panel__body');
  return {viewport:{width:innerWidth,height:innerHeight}, trigger:rect(selected),
    stageClose:rect(document.querySelector('.stage__icon-btn[aria-label="Exit Stage"]')),
    coarse:matchMedia('(pointer: coarse)').matches,
    font:{family:getComputedStyle(selected).fontFamily,size:getComputedStyle(selected).fontSize},
    value:selected.dataset.value, expanded:selected.getAttribute('aria-expanded'),
    body:{...rect(body),clientWidth:body.clientWidth,scrollWidth:body.scrollWidth},
    documentWidth:document.documentElement.scrollWidth,
    list:list ? {...rect(list),scrollHeight:list.scrollHeight,clientHeight:list.clientHeight,
      clientWidth:list.clientWidth,scrollWidth:list.scrollWidth,scrollTop:list.scrollTop,
      position:getComputedStyle(list).position} : null,
    options:list ? [...list.querySelectorAll('[role="option"]')].map(el => ({
      ...rect(el),value:el.dataset.value,text:el.textContent.trim(),
      selected:el.getAttribute('aria-selected'),scrollHeight:el.scrollHeight,
      clientHeight:el.clientHeight,
    })) : []};
})()"""


async def geometry(cdp, label):
    return await cdp.evaluate(GEOMETRY.replace('SELECTOR', json.dumps(trigger(label))))


async def open_appearance(cdp):
    await mouse(cdp, '.stage__icon-btn[aria-label="Stage appearance"]')
    await wait_for(cdp, f"document.querySelectorAll('.theme-panel [role=combobox]').length === {len(LABELS)}",
                   'All custom font menus')
    labels = await cdp.evaluate("[...document.querySelectorAll('.theme-panel [role=combobox]')].map(el => el.getAttribute('aria-label'))")
    assert labels == list(LABELS), labels
    assert await cdp.evaluate("document.querySelectorAll('.theme-panel select').length") == 0


async def reveal(cdp, selector, *, block='center'):
    # Position the scrolling settings sheet; pointer activation below remains
    # real browser input, including testing the popup's own scroll separately.
    await cdp.evaluate('document.querySelector(' + json.dumps(selector) + ').scrollIntoView({block:' + json.dumps(block) + ',inline:"nearest"})')
    await asyncio.sleep(.04)


async def open_menu(cdp, label, *, use_touch=False, block='center'):
    await reveal(cdp, trigger(label), block=block)
    await wait_for(cdp, """(() => {
      const height = document.querySelector('.stage__icon-btn[aria-label="Exit Stage"]').getBoundingClientRect().height;
      return height > 0 && [...document.querySelectorAll('.theme-panel [role="combobox"]')]
        .every(el => Math.abs(el.getBoundingClientRect().height - height) <= 1);
    })()""", 'All settings controls match the current Stage X height')
    await (touch if use_touch else mouse)(cdp, trigger(label))
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger(label)) + ').getAttribute("aria-expanded") === "true"',
                   'Expanded ' + label)
    return await geometry(cdp, label)


async def close_menu(cdp, label):
    await escape(cdp)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger(label)) + ').getAttribute("aria-expanded") === "false"',
                   'Escape closes only ' + label)
    assert await cdp.evaluate("!!document.querySelector('.theme-panel')")
    assert await cdp.evaluate('document.activeElement === document.querySelector(' + json.dumps(trigger(label)) + ')')


def verify_geometry(state):
    assert state and state['expanded'] == 'true' and state['list'], state
    control, popup, viewport = state['trigger'], state['list'], state['viewport']
    assert state['stageClose']['height'] >= (40 if state['coarse'] else 30), state
    assert abs(control['height'] - state['stageClose']['height']) <= 1, state
    assert control['width'] >= 48, state
    assert len(state['options']) == 6, state
    assert sum(item['selected'] == 'true' for item in state['options']) == 1, state
    assert popup['position'] == 'fixed', state
    assert popup['x'] >= -1 and popup['right'] <= viewport['width'] + 1, state
    assert popup['y'] >= -1 and popup['bottom'] <= viewport['height'] + 1, state
    assert popup['height'] > 0 and popup['width'] >= 48, state
    assert popup['scrollWidth'] <= popup['clientWidth'] + 1, state
    assert state['documentWidth'] <= viewport['width'] + 1, state
    assert state['body']['scrollWidth'] <= state['body']['clientWidth'] + 1, state
    assert popup['bottom'] <= control['y'] + 1 or popup['y'] >= control['bottom'] - 1, state
    for item in state['options']:
        assert abs(item['height'] - control['height']) <= 1, state
        assert abs(item['height'] - state['stageClose']['height']) <= 1, state
        assert item['width'] >= 48, item
        assert item['scrollHeight'] <= item['clientHeight'] + 1, item
    if sum(item['height'] for item in state['options']) > popup['height'] + 2:
        assert popup['scrollHeight'] > popup['clientHeight'], state


async def wheel_menu(cdp, label, direction):
    popup = (await geometry(cdp, label))['list']
    await cdp.command('Input.dispatchMouseEvent', {
        'type':'mouseWheel', 'x':popup['x'] + popup['width'] / 2,
        'y':popup['y'] + popup['height'] / 2, 'deltaX':0, 'deltaY':direction * 10000,
    })
    await asyncio.sleep(.12)
    return await geometry(cdp, label)


async def passive_geometry(cdp, args):
    initial_font = await cdp.evaluate('getComputedStyle(document.documentElement).fontSize')
    scrolled, flipped = False, False
    for scale in (1, 1.5) if not args.live else (1,):
        if not args.live:
            await cdp.evaluate('document.documentElement.style.fontSize = ' + json.dumps(str(float(initial_font[:-2]) * scale) + 'px'))
        for width, height in VIEWPORTS:
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width,'height':height,'deviceScaleFactor':1,'mobile':False,
            })
            await asyncio.sleep(.06)
            sizes = []
            for label in LABELS:
                # Deliberately place one field at the bottom of the panel to
                # exercise upward opening independently of header dimensions.
                state = await open_menu(cdp, label, block='end' if label == 'Switch ID font' else 'center')
                verify_geometry(state)
                sizes.append(state['trigger']['height'])
                flipped |= state['list']['bottom'] <= state['trigger']['y'] + 1
                if state['list']['scrollHeight'] > state['list']['clientHeight'] + 1:
                    bottom = await wheel_menu(cdp, label, 1)
                    assert bottom['list']['scrollTop'] > 0, bottom
                    assert abs(bottom['list']['scrollTop'] + bottom['list']['clientHeight'] - bottom['list']['scrollHeight']) <= 2, bottom
                    top = await wheel_menu(cdp, label, -1)
                    assert top['list']['scrollTop'] <= 1, top
                    scrolled = True
                if args.screenshot and label == 'Default font' and (width, height) == (320, 568):
                    path = Path(args.screenshot)
                    name = path.stem + ('-150pct' if scale > 1 else '') + path.suffix
                    await capture(cdp, path.with_name(name))
                await close_menu(cdp, label)
            assert all(abs(value - sizes[0]) <= 1 for value in sizes), sizes
            print(f'PASS {"LIVE " if args.live else ""}all {len(LABELS)} font menus {width}x{height}, root {scale:g}x: options/controls match Stage X at ' + ', '.join(f'{value:g}' for value in sizes) + 'px; viewport bounds and scroll', flush=True)
    assert scrolled, 'No overflow menu was exercised'
    assert flipped, 'No menu above its trigger was exercised'
    if not args.live:
        await cdp.evaluate('document.documentElement.style.fontSize = ""')
    print('PASS menus scroll at full item height and flip above lower controls without viewport overflow', flush=True)


async def theme(cdp):
    raw = await cdp.evaluate('localStorage.getItem(' + json.dumps(STORAGE) + ')')
    return json.loads(raw) if raw else {'version':2,'sections':{}}


async def no_actions(cdp, *, live):
    await cdp.evaluate('true')
    if live:
        messages = [entry['message'] for entry in cdp.frames_since(0) if entry['direction'] == 'SEND']
    else:
        messages = await cdp.evaluate('window.__bankPeer.commands')
    forbidden = [message for message in messages if message.get('type') in ('SWITCH_PATCH', 'ACTIVATE_SWITCH')]
    assert not forbidden, forbidden


async def choose_named(cdp, label, name, *, use_touch=False):
    state = await open_menu(cdp, label, use_touch=use_touch)
    match = next(item for item in state['options'] if item['text'] == name)
    await reveal(cdp, option(match['value']))
    await (touch if use_touch else mouse)(cdp, option(match['value']))
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger(label)) + ').dataset.value === ' + json.dumps(match['value']),
                   'Chosen ' + name + ' for ' + label)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger(label)) + ').getAttribute("aria-expanded")') == 'false'
    return match['value']


async def input_checks(cdp, args):
    assert await cdp.evaluate("""[...document.querySelectorAll('.theme-panel__sections input[type="range"]')]
      .map(el => ({value:el.value,min:el.min,max:el.max}))""") == [
        {'value':'1','min':'0.1','max':'5'} for _ in SECTIONS]
    print('PASS all font sizes default to 100% with a 10%-500% range and doubled base size', flush=True)
    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':1045,'height':399,'deviceScaleFactor':1,'mobile':False,
    })
    serif = await choose_named(cdp, 'Default font', 'Serif')
    assert (await theme(cdp))['fontFamily'] == serif
    assert serif in await cdp.evaluate('document.querySelector(".stage").style.getPropertyValue("--stage-font")')
    print('PASS real mouse font selection persists and applies to Stage', flush=True)

    await open_menu(cdp, 'Default font')
    await close_menu(cdp, 'Default font')
    await key(cdp, 'Enter', 13)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger('Default font')) + ').getAttribute("aria-expanded") === "true"', 'Keyboard opens font menu')
    before = await theme(cdp)
    for name, code in (('End', 35), ('Home', 36), ('ArrowDown', 40), ('End', 35)):
        await key(cdp, name, code)
        assert await theme(cdp) == before, 'Keyboard exploration saved before commit'
        assert await cdp.evaluate('!!document.getElementById(document.querySelector(' + json.dumps(trigger('Default font')) + ').getAttribute("aria-activedescendant"))')
    await key(cdp, 'Enter', 13)
    condensed = (await theme(cdp))['fontFamily']
    assert condensed != serif and 'Arial Narrow' in condensed
    print('PASS keyboard opens/navigates menu without early commit and Enter saves selected font', flush=True)

    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':375,'height':667,'deviceScaleFactor':1,'mobile':False,
    })
    await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':True, 'maxTouchPoints':1})
    mono = await choose_named(cdp, 'Rig name font', 'Monospace', use_touch=True)
    assert (await theme(cdp))['sections']['rigName']['fontFamily'] == mono
    assert (await theme(cdp))['fontFamily'] == condensed
    await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':False})
    print('PASS real touch chooses section font independently of default font', flush=True)

    # Section size sliders alter the Stage display. If the resulting header
    # geometry changes, every UI control follows its shared Stage X height.
    root_size = await cdp.evaluate('parseFloat(getComputedStyle(document.documentElement).fontSize)')
    await cdp.evaluate('document.documentElement.style.fontSize = ' + json.dumps(str(root_size * 1.5) + 'px'))
    await asyncio.sleep(.1)
    for label, (section, css_key) in zip(LABELS[1:], SECTIONS):
        size = '.theme-panel input[type="range"][aria-label=' + json.dumps(label[:-5] + ' size') + ']'
        await reveal(cdp, size)
        await mouse(cdp, size)
        await key(cdp, 'Home', 36)
        assert await cdp.evaluate('document.querySelector(' + json.dumps(size) + ').value') == '0.1'
        assert (await theme(cdp))['sections'][section]['scale'] == 0.1
        assert await cdp.evaluate('document.querySelector(".stage").style.getPropertyValue(' + json.dumps('--stage-' + css_key + '-scale') + ')') == '0.2'
        await key(cdp, 'End', 35)
        assert await cdp.evaluate('document.querySelector(' + json.dumps(size) + ').value') == '5'
        assert (await theme(cdp))['sections'][section]['scale'] == 5
        assert await cdp.evaluate('document.querySelector(".stage").style.getPropertyValue(' + json.dumps('--stage-' + css_key + '-scale') + ')') == '10'
        state = await open_menu(cdp, label)
        verify_geometry(state)
        common = await geometry(cdp, 'Default font')
        assert abs(state['trigger']['height'] - common['trigger']['height']) <= 1, state
        assert state['font'] == common['font'], state
        await close_menu(cdp, label)
    await cdp.evaluate('document.documentElement.style.fontSize = ""')
    print('PASS all section size sliders at 10% and 500% update saved display CSS; every control/option follows the same Stage X height and common typography at root 150% without clipping', flush=True)

    await mouse(cdp, '.theme-panel__close')
    await open_appearance(cdp)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger('Rig name font')) + ').dataset.value') == mono
    await no_actions(cdp, live=False)
    await cdp.command('Page.reload')
    await wait_for(cdp, "typeof window.__stageDoorbell === 'function' && !!document.querySelector('.stage__bank-readout')", 'Reloaded Stage preview')
    await fixture(cdp, 'WAH', title='SETTINGS PREVIEW')
    await cdp.evaluate(PEER)
    await open_appearance(cdp)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger('Default font')) + ').dataset.value') == condensed
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger('Rig name font')) + ').dataset.value') == mono
    assert await cdp.evaluate("[...document.querySelectorAll('.theme-panel__sections input[type=range]')].every(el => el.value === '5')")
    print('PASS default and section font preferences survive panel reopen and full page reload', flush=True)

    reset = '.theme-panel__row[aria-label="Rig name appearance"] .theme-panel__reset'
    await reveal(cdp, reset)
    await mouse(cdp, reset)
    assert 'rigName' not in (await theme(cdp))['sections']
    assert (await theme(cdp))['fontFamily'] == condensed
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger('Rig name font')) + ').dataset.value') == ''
    assert await cdp.evaluate('document.querySelector(".stage").style.getPropertyValue("--stage-rig-name-scale")') == '2'
    await mouse(cdp, '.theme-panel__reset-all')
    assert await theme(cdp) == {'version':2,'sections':{}}
    assert await cdp.evaluate("[...document.querySelectorAll('.theme-panel [role=combobox]')].every(el => el.dataset.value === '')")
    assert await cdp.evaluate("[...document.querySelectorAll('.theme-panel__sections input[type=range]')].every(el => el.value === '1')")
    await no_actions(cdp, live=False)
    print('PASS section reset preserves default font; Reset all restores defaults; zero device actions', flush=True)


async def corner_checks(cdp, args):
    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':1920,'height':440,'deviceScaleFactor':1,'mobile':False,
    })
    await asyncio.sleep(.1)
    assert await cdp.evaluate("""[...document.querySelectorAll('.theme-panel__corners input[type=range]')]
      .map(el => ({value:el.value,min:el.min,max:el.max}))""") == [
        {'value':'0.75','min':'0','max':'2'}]
    serif = await choose_named(cdp, 'Default font', 'Serif')

    async def radii():
        return await cdp.evaluate("""(() => {
          const radius = (el, pseudo = null) => {
            const s = getComputedStyle(el, pseudo);
            return [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomRightRadius, s.borderBottomLeftRadius];
          };
          const stage = document.querySelector('.stage');
          return {screen:radius(stage), inset:radius(stage, '::before'),
            rows:[...document.querySelectorAll('.stage__pedal-row')].map(row =>
              [...row.querySelectorAll('.stage__switch')].map(el => ({outer:radius(el),inner:radius(el,'::before')}))),
            controls:[...document.querySelectorAll('.stage__header button')].map(el => ({label:el.getAttribute('aria-label'),radius:radius(el)}))};
        })()""")

    async def set_endpoint(maximum):
        selector = '.theme-panel__corners input[aria-label="Corners"]'
        await reveal(cdp, selector)
        await mouse(cdp, selector)
        await key(cdp, 'End' if maximum else 'Home', 35 if maximum else 36)

    baseline = await radii()
    assert baseline['screen'] == ['42px'] * 4, baseline
    assert baseline['rows'][-1][0]['outer'] == ['4px', '4px', '4px', '27px'], baseline
    assert baseline['rows'][-1][-1]['outer'] == ['4px', '4px', '27px', '4px'], baseline
    assert next(item for item in baseline['controls'] if item['label'] == 'Exit Stage')['radius'][1] == '27px'
    for scale in (2, 0):
        await set_endpoint(scale == 2)
        expected = json.loads(json.dumps(baseline))
        expected['screen'] = [f'{56 * scale}px'] * 4
        expected['inset'] = [f'{max(0, 56 * scale - 3)}px'] * 4
        for tile, index in ((expected['rows'][-1][0], 3), (expected['rows'][-1][-1], 2)):
            tile['outer'][index] = f'{36 * scale}px'
            tile['inner'][index] = f'{max(0, 36 * scale - 5)}px'
        next(item for item in expected['controls'] if item['label'] == 'Exit Stage')['radius'][1] = f'{36 * scale}px'
        actual = await radii()
        assert actual == expected, {'scale':scale, 'actual':actual, 'expected':expected}
        assert (await theme(cdp))['corners'] == scale
    reset = '.theme-panel [aria-label="Reset corners"]'
    await reveal(cdp, reset)
    await mouse(cdp, reset)
    assert await radii() == baseline
    assert 'corners' not in await theme(cdp)
    assert (await theme(cdp))['fontFamily'] == serif
    assert await cdp.evaluate("document.querySelector('.theme-panel__corners input').value") == '0.75'
    print('PASS one Corners control defaults to 75%; 0% and 200% affect only the screen, two lower outer switch corners and top-right X; corner reset restores 75% and preserves fonts', flush=True)

    if args.screenshot:
        await reveal(cdp, '.theme-panel__corners')
        path = Path(args.screenshot)
        await capture(cdp, path.with_name(path.stem + '-corners' + path.suffix))
    await set_endpoint(False)
    squared = await radii()
    await no_actions(cdp, live=False)
    await cdp.command('Page.reload')
    await wait_for(cdp, "typeof window.__stageDoorbell === 'function' && !!document.querySelector('.stage__bank-readout')", 'Reloaded square Stage')
    await fixture(cdp, 'WAH', title='SQUARE DISPLAY')
    await cdp.evaluate(PEER)
    assert await radii() == squared
    assert (await theme(cdp))['corners'] == 0
    if args.screenshot:
        await capture(cdp, path.with_name(path.stem + '-square' + path.suffix))
    await mouse(cdp, '.stage__bank-readout')
    await wait_for(cdp, "!!document.querySelector('.bank-picker[open]')", 'Square bank picker')
    assert await cdp.evaluate("getComputedStyle(document.querySelector('.bank-picker')).borderRadius") == '0px'
    await escape(cdp)
    await open_appearance(cdp)
    assert await cdp.evaluate("[...document.querySelectorAll('.theme-panel__corners input')].every(el => el.value === '0')")
    await mouse(cdp, '.theme-panel__reset-all')
    assert await theme(cdp) == {'version':2,'sections':{}}
    assert await radii() == baseline
    await no_actions(cdp, live=False)
    assert await cdp.evaluate("document.querySelector('.theme-panel__corners input').value") == '0.75'
    print('PASS single corner value survives reload, including square bank picker frame; Reset all restores 75%; zero device actions', flush=True)


async def run(args):
    parsed = urlparse(args.page)
    if args.live:
        if parsed.scheme != 'http' or parsed.hostname != os.environ.get("BOSUN_STAGE_HOST", "bosun-hub") or parsed.port != 8080 or parsed.path != '/':
            raise ValueError('--live permits only the configured Pi Stage on HTTP port 8080')
    elif parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/stage-preview.html':
        raise ValueError('Only the local stage-preview.html fixture is allowed without --live')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-stage-settings-cdp-')
    browser = subprocess.Popen([
        args.edge, '--headless=new', '--disable-gpu', '--no-first-run',
        '--remote-debugging-port=%d' % port, '--user-data-dir=' + profile.name,
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = CdpSession(await cdp_socket(port, args.page))
        await cdp.command('Network.enable')
        await cdp.command('Page.navigate', {'url':args.page})
        await wait_for(cdp, "!!document.querySelector('.stage__bank-readout')", 'Stage loaded')
        original_storage = await cdp.evaluate('JSON.stringify(Object.entries(localStorage).sort())')
        if not args.live:
            assert await cdp.evaluate("typeof window.__stageDoorbell === 'function'")
            await fixture(cdp, 'WAH', title='SETTINGS PREVIEW')
            await cdp.evaluate(PEER)
        await open_appearance(cdp)
        await passive_geometry(cdp, args)
        if args.live:
            await mouse(cdp, '.theme-panel__close')
            assert await cdp.evaluate('JSON.stringify(Object.entries(localStorage).sort())') == original_storage
            await no_actions(cdp, live=True)
            print('PASS LIVE passive open/inspect/close of every settings menu; unchanged local preferences and zero rig/effect commands', flush=True)
        else:
            await input_checks(cdp, args)
            await corner_checks(cdp, args)
            print('PASS all Stage settings browser checks; isolated preview, no hardware connection', flush=True)
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
    parser.add_argument('--screenshot', help='Optional PNG of expanded font options at 320px width')
    parser.add_argument('--live', action='store_true', help='Passive configured Pi inspection only; no option selection')
    asyncio.run(run(parser.parse_args()))
