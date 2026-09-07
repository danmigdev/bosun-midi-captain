"""Compare shared Stage controls through real Chromium rendering, without hardware.

Only a local stage-preview.html URL is accepted. The isolated browser checks
bank/settings selectors, their expanded options, all close icons and CANCEL
across viewport, root font and per-section appearance changes. Every device
action is rejected by the assertions; bank preselection stays local.
"""

import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

from browser_stage_bank_picker import GEOMETRY, PEER, capture, escape, mouse, mutations, open_picker, verify_mode_geometry, wait_for
from browser_stage_layout import fixture
from browser_stage_settings import LABELS, choose_named, close_menu, open_appearance, open_menu, trigger
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


VIEWPORTS = ((320, 568), (568, 320), (1045, 399), (1920, 1080))
MAIN_CLOSE = '.stage__icon-btn[aria-label="Exit Stage"]'
MAIN_SETTINGS = '.stage__icon-btn[aria-label="Stage appearance"]'
BANK_MODE = '.bank-picker__mode-trigger'
BANK_CLOSE = '.bank-picker__close'
SETTINGS_CLOSE = '.theme-panel__close'
CANCEL = '[aria-label="Cancel bank preselection"]'
SECTION_VARS = ('rig-name', 'bank', 'bpm', 'tuner', 'switch-label', 'switch-id')

PROBE = r"""(() => {
  const el = document.querySelector(SELECTOR);
  if (!el) throw new Error('Missing style target: ' + SELECTOR);
  const rect = target => {
    const r = target.getBoundingClientRect();
    return {x:r.x, y:r.y, width:r.width, height:r.height, right:r.right, bottom:r.bottom};
  };
  const css = (target, properties) => {
    const style = getComputedStyle(target);
    return Object.fromEntries(properties.map(name => [name, style[name]]));
  };
  const properties = ['fontFamily','fontSize','fontWeight','lineHeight','letterSpacing',
    'color','backgroundColor','borderTopColor','borderTopWidth','borderTopStyle',
    'borderRadius','paddingTop','paddingRight','paddingBottom','paddingLeft','minHeight'];
  const svg = el.querySelector('svg');
  const list = document.getElementById(el.getAttribute('aria-controls'));
  return {
    box:rect(el), css:css(el, properties), text:el.textContent.trim(),
    clientHeight:el.clientHeight, scrollHeight:el.scrollHeight,
    clientWidth:el.clientWidth, scrollWidth:el.scrollWidth,
    svg:svg ? {box:rect(svg), css:css(svg, ['fill','stroke','strokeWidth']),
      viewBox:svg.getAttribute('viewBox'), path:svg.querySelector('path')?.getAttribute('d')} : null,
    list:list ? {box:rect(list), css:css(list, properties), clientHeight:list.clientHeight,
      scrollHeight:list.scrollHeight, clientWidth:list.clientWidth, scrollWidth:list.scrollWidth,
      options:[...list.querySelectorAll('[role="option"]')].map(item => ({box:rect(item),
        css:css(item, properties), selected:item.getAttribute('aria-selected') === 'true',
        scrollHeight:item.scrollHeight, clientHeight:item.clientHeight}))} : null,
  };
})()"""


async def probe(cdp, selector):
    # Remove hover before comparing normal-state backgrounds.
    await cdp.command('Input.dispatchMouseEvent', {'type':'mouseMoved', 'x':0, 'y':0})
    await asyncio.sleep(.035)
    return await cdp.evaluate(PROBE.replace('SELECTOR', json.dumps(selector)))


def same_css(actual, expected, description, *, properties=None):
    wanted = properties or expected['css'].keys()
    differences = {key:(expected['css'][key], actual['css'][key])
                   for key in wanted if actual['css'][key] != expected['css'][key]}
    assert not differences, (description, differences)


def same_close(actual, expected, description):
    same_css(actual, expected, description,
             properties=('color','backgroundColor','borderTopColor','borderTopWidth',
                         'borderTopStyle','borderRadius','paddingTop','paddingRight',
                         'paddingBottom','paddingLeft'))
    for axis in ('width', 'height'):
        assert abs(actual['box'][axis] - expected['box'][axis]) <= 1, (description, actual, expected)
        assert actual['svg'] and expected['svg'], (description, actual, expected)
        assert abs(actual['svg']['box'][axis] - expected['svg']['box'][axis]) <= 1, (description, actual, expected)
    assert actual['svg']['css'] == expected['svg']['css'], (description, actual, expected)
    assert actual['svg']['viewBox'] == expected['svg']['viewBox'], (description, actual, expected)
    assert actual['svg']['path'] == expected['svg']['path'], (description, actual, expected)


def verify_options(state, width, height, main_close, *, coarse=False):
    popup = state['list']
    assert popup and popup['options'], state
    assert state['box']['height'] >= (40 if coarse else 30), state
    assert abs(state['box']['height'] - main_close['box']['height']) <= 1, ('Selector row differs from main X', state, main_close)
    assert state['scrollHeight'] <= state['clientHeight'] + 1, state
    assert state['scrollWidth'] <= state['clientWidth'] + 1, state
    assert popup['box']['x'] >= -1 and popup['box']['right'] <= width + 1, state
    assert popup['box']['y'] >= -1 and popup['box']['bottom'] <= height + 1, state
    assert popup['scrollWidth'] <= popup['clientWidth'] + 1, state
    for item in popup['options']:
        assert abs(item['box']['height'] - state['box']['height']) <= 1, state
        assert item['scrollHeight'] <= item['clientHeight'] + 1, state
    if sum(item['box']['height'] for item in popup['options']) > popup['box']['height'] + 2:
        assert popup['scrollHeight'] > popup['clientHeight'], state


async def choose_mode(cdp, value):
    await mouse(cdp, BANK_MODE)
    selector = '.bank-picker [role="option"][data-value=' + json.dumps(value) + ']'
    await wait_for(cdp, '!!document.querySelector(' + json.dumps(selector) + ')', 'Bank mode option')
    await mouse(cdp, selector)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(BANK_MODE) + ').dataset.value === ' + json.dumps(value), 'Bank mode chosen')


async def global_monospace_check(cdp, args):
    """The widest global UI font must fit expanded rows on a narrow screen."""
    await open_appearance(cdp)
    await choose_named(cdp, 'Default font', 'Monospace')
    await mouse(cdp, SETTINGS_CLOSE)
    css = await inspect(cdp, args, 320, 568, 1.5, False)
    assert 'monospace' in css['fontFamily'], ('Expected actual global Monospace controls', css)
    print('PASS global Monospace at 320px and 150% root font: full option labels fit rows matching main X', flush=True)


async def inspect(cdp, args, width, height, scale, section_overrides, coarse=False):
    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
    })
    await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':coarse, 'maxTouchPoints':1})
    await cdp.evaluate('document.documentElement.style.fontSize = ' + json.dumps(str(16 * scale) + 'px'))
    await cdp.evaluate("""(() => {
      const style = document.querySelector('.stage').style;
      for (const section of SECTIONS) {
        if (OVERRIDE) {
          style.setProperty('--stage-' + section + '-scale', '2');
          style.setProperty('--stage-' + section + '-font', 'monospace');
        } else {
          style.removeProperty('--stage-' + section + '-scale');
          style.removeProperty('--stage-' + section + '-font');
        }
      }
    })()""".replace('SECTIONS', json.dumps(SECTION_VARS)).replace('OVERRIDE', json.dumps(section_overrides)))
    await asyncio.sleep(.12)
    main = await probe(cdp, MAIN_CLOSE)
    settings_button = await probe(cdp, MAIN_SETTINGS)
    for axis in ('width','height'):
        assert abs(main['box'][axis] - settings_button['box'][axis]) <= 1, (main, settings_button)
    screenshot_case = args.screenshots and (width, height, scale, section_overrides, coarse) == (1045,399,1,False,False)
    if screenshot_case:
        await capture(cdp, Path(args.screenshots) / 'main-header.png')
    await open_picker(cdp)
    bank_close = await probe(cdp, BANK_CLOSE)
    same_close(bank_close, main, 'BANK X matches main Stage X')
    verify_mode_geometry(await cdp.evaluate(GEOMETRY))
    if screenshot_case:
        await capture(cdp, Path(args.screenshots) / 'bank-header.png')
    await mouse(cdp, BANK_MODE)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(BANK_MODE) + ').getAttribute("aria-expanded") === "true"', 'Bank selector open')
    bank = await probe(cdp, BANK_MODE)
    verify_options(bank, width, height, main, coarse=coarse)
    verify_mode_geometry(await cdp.evaluate(GEOMETRY), expanded=True)
    if screenshot_case:
        await capture(cdp, Path(args.screenshots) / 'bank-controls.png')
    await escape(cdp)
    await mouse(cdp, BANK_CLOSE)
    await open_appearance(cdp)
    same_close(await probe(cdp, SETTINGS_CLOSE), main, 'Settings X matches main Stage X')
    for label in LABELS:
        await open_menu(cdp, label)
        settings = await probe(cdp, trigger(label))
        same_css(settings, bank, label + ' matches BANK selector')
        verify_options(settings, width, height, main, coarse=coarse)
        same_css(settings['list'], bank['list'], label + ' popup matches BANK popup')
        for selected in (False, True):
            bank_option = next(item for item in bank['list']['options'] if item['selected'] == selected)
            for item in settings['list']['options']:
                if item['selected'] == selected:
                    same_css(item, bank_option, label + ' option matches BANK option')
        if args.screenshots and label == 'Default font' and (width,height,scale,section_overrides,coarse) == (1045,399,1,False,False):
            await capture(cdp, Path(args.screenshots) / 'settings-controls.png')
        await close_menu(cdp, label)
    await mouse(cdp, SETTINGS_CLOSE)
    await open_picker(cdp)
    await choose_mode(cdp, 'preselect')
    await mouse(cdp, '.bank-picker__bank[aria-label="Bank 2"]')
    await wait_for(cdp, '!!document.querySelector(' + json.dumps(CANCEL) + ')', 'Preview bank2 without loading')
    cancel = await probe(cdp, CANCEL)
    active_main = await probe(cdp, MAIN_CLOSE)
    assert cancel['text'] == 'CANCEL', cancel
    assert abs(cancel['box']['height'] - active_main['box']['height']) <= 1, (cancel, active_main)
    assert abs((cancel['box']['x'] + cancel['box']['right']) / 2 - width / 2) <= 1, cancel
    same_css(cancel, active_main, 'CANCEL uses shared button surface',
             properties=('color','backgroundColor','borderTopColor','borderTopWidth','borderTopStyle','borderRadius'))
    await mouse(cdp, CANCEL)
    await wait_for(cdp, '!document.querySelector(' + json.dumps(CANCEL) + ')', 'Preview cancelled')
    await open_picker(cdp)
    await choose_mode(cdp, 'immediate')
    await mouse(cdp, BANK_CLOSE)
    assert not await mutations(cdp), 'Control checks sent rig/effect actions'
    assert await cdp.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'Document overflow'
    print(f'PASS common control CSS {width}x{height}, root {scale:g}x, section overrides={section_overrides}, coarse={coarse}: full BANK/main header and X rectangles match; 8 combo/option heights match X; centered CANCEL; no device actions', flush=True)
    return bank['css']


async def run(args):
    parsed = urlparse(args.page)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/stage-preview.html':
        raise ValueError('Only the isolated local stage-preview.html fixture is allowed')
    with socket.socket() as probe_socket:
        probe_socket.bind(('127.0.0.1', 0))
        port = probe_socket.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-stage-control-styles-cdp-')
    browser = subprocess.Popen([
        args.edge, '--headless=new', '--disable-gpu', '--no-first-run',
        '--remote-debugging-port=%d' % port, '--user-data-dir=' + profile.name,
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = CdpSession(await cdp_socket(port, args.page))
        await cdp.command('Page.navigate', {'url':args.page})
        await wait_for(cdp, "typeof window.__stageDoorbell === 'function' && !!document.querySelector('.stage__bank-readout')", 'Local Stage preview')
        await fixture(cdp, 'WAH', title='UNIFORM CONTROLS')
        await cdp.evaluate(PEER)
        for scale in (1, 1.5):
            for width, height in VIEWPORTS:
                baseline = await inspect(cdp, args, width, height, scale, False)
                overrides = await inspect(cdp, args, width, height, scale, True)
                # Section type can resize the main header, and therefore the
                # common row and its fitted text/padding. Font family and all
                # visual surfaces remain shared across this layout change.
                adaptive = {'fontSize', 'paddingTop', 'paddingBottom', 'paddingLeft', 'paddingRight', 'minHeight', 'lineHeight'}
                assert {k:v for k,v in baseline.items() if k not in adaptive} == {k:v for k,v in overrides.items() if k not in adaptive}, ('Per-section appearance changed common control styling', baseline, overrides)
        for scale in (1, 1.5):
            for width,height in VIEWPORTS[:2]:
                await inspect(cdp, args, width, height, scale, False, coarse=True)
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':False})
        await open_appearance(cdp)
        await choose_named(cdp, 'Default font', 'Serif')
        await choose_named(cdp, 'Rig name font', 'Monospace')
        await mouse(cdp, SETTINGS_CLOSE)
        for scale in (1, 1.5):
            for width,height in (VIEWPORTS[0], VIEWPORTS[2]):
                css = await inspect(cdp, args, width, height, scale, False)
                assert 'Georgia' in css['fontFamily'], ('Shared controls should follow the default UI font', css)
        print('PASS changed global font applies uniformly while a section font stays independent', flush=True)
        await global_monospace_check(cdp, args)
        print('PASS all common Stage control style checks; isolated browser and no hardware connection', flush=True)
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
    parser.add_argument('--screenshots', help='Optional directory for synthetic bank/settings control screenshots')
    asyncio.run(run(parser.parse_args()))
