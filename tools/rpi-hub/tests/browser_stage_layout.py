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
  const expression = document.querySelector('.stage__expression');
  const optional = selector => {
    const el = document.querySelector(selector);
    return el ? rect(el) : null;
  };
  const bankControls = document.querySelector('.stage__bank-controls');
  const bankCss = getComputedStyle(bankControls);
  const controlsCss = getComputedStyle(document.querySelector('.stage__controls'));
  const headerCss = getComputedStyle(header);
  const metaCss = getComputedStyle(document.querySelector('.stage__meta'));
  return {
    width:innerWidth, height:innerHeight, stage:rect(stage), header:rect(header),
    divider:optional('.stage__divider'),
    dividerAfterHeader:header.nextElementSibling?.matches('.stage__divider'),
    headerStyle:{background:headerCss.backgroundColor, image:headerCss.backgroundImage,
      shadow:headerCss.boxShadow,
      border:['Top','Right','Bottom','Left'].map(side => parseFloat(headerCss['border' + side + 'Width'])),
      padding:['Top','Right','Bottom','Left'].map(side => parseFloat(headerCss['padding' + side]))},
    headerBorders:[...header.querySelectorAll(
      '.stage__bank-readout,.stage__rig-readout,.stage__expression,.stage__icon-btn,.stage__bank-btn')].map(el => {
        const css = getComputedStyle(el);
        return ['Top','Right','Bottom','Left'].map(side => ({
          width:css['border' + side + 'Width'], color:css['border' + side + 'Color'],
          style:css['border' + side + 'Style'],
        }));
      }),
    bankGroupStyle:{background:bankCss.backgroundColor, image:bankCss.backgroundImage,
      border:['Top','Right','Bottom','Left'].map(side => parseFloat(bankCss['border' + side + 'Width'])),
      padding:['Top','Right','Bottom','Left'].map(side => parseFloat(bankCss['padding' + side])),
      gap:parseFloat(bankCss.rowGap), controlsGap:parseFloat(controlsCss.rowGap)},
    coarsePointer:matchMedia('(pointer: coarse)').matches,
    controls:optional('.stage__controls'),
    controlsVisible:[...document.querySelectorAll('.stage__controls,.stage__icon-btn')].every(el => {
      const css = getComputedStyle(el);
      return css.display !== 'none' && css.visibility === 'visible' && Number(css.opacity) === 1;
    }),
    buttons:[...document.querySelectorAll('.stage__icon-btn')].map(el => ({
      ...rect(el), label:el.getAttribute('aria-label'),
    })),
    headerPaddingRight:parseFloat(getComputedStyle(header).paddingRight),
    rig:rect(document.querySelector('.stage__rig-name')),
    rigText:document.querySelector('.stage__rig-name')?.textContent.trim(),
    meta:rect(document.querySelector('.stage__meta')),
    metaStyle:{background:metaCss.backgroundColor, image:metaCss.backgroundImage, shadow:metaCss.boxShadow,
      border:['Top','Right','Bottom','Left'].map(side => parseFloat(metaCss['border' + side + 'Width'])),
      padding:['Top','Right','Bottom','Left'].map(side => parseFloat(metaCss['padding' + side]))},
    bankReadout:optional('.stage__bank-readout'),
    rigReadout:optional('.stage__rig-readout'),
    rigPosition:optional('.stage__rig'),
    rigPositionText:document.querySelector('.stage__rig')?.textContent.trim(),
    bankControls:optional('.stage__bank-controls'),
    bankControlsInHeader:document.querySelector('.stage__bank-controls')?.parentElement === header,
    bankButtons:[...document.querySelectorAll('.stage__bank-btn')].map(el => {
      const css = getComputedStyle(el);
      return {...rect(el), text:el.textContent.trim(), label:el.getAttribute('aria-label'),
        disabled:el.disabled, visible:css.display !== 'none' &&
        css.visibility === 'visible' && Number(css.opacity) > 0};
    }),
    navigationError:optional('.stage__navigation-error'),
    bank:document.querySelector('.stage__bank') ? rect(document.querySelector('.stage__bank')) : null,
    bankText:document.querySelector('.stage__bank')?.textContent.trim(),
    bpm:document.querySelector('.stage__bpm') ? rect(document.querySelector('.stage__bpm')) : null,
    bpmText:document.querySelector('.stage__bpm')?.textContent.trim(),
    tuner:optional('.stage__tuner'),
    tunerText:document.querySelector('.stage__tuner')?.textContent.trim(),
    expression:expression ? rect(expression) : null,
    expressionText:expression?.textContent.trim(),
    cards:[...document.querySelectorAll('.stage__switch')].map(rect),
    textFrames:[...document.querySelectorAll(
      '.stage__rig-name,.stage__bank,.stage__rig,.stage__switch-label')].map(el => ({
        text:el.textContent.trim(), width:el.clientWidth,
        textWidth:el.firstElementChild.scrollWidth,
        scrolling:el.firstElementChild.classList.contains('stage__marquee-active'),
      })),
  };
})()"""

VIEWPORTS = ((1045, 399), (800, 480), (640, 360), (568, 320),
             (480, 800), (375, 667), (320, 568), (834, 1112), (1920, 1080))


async def fixture(cdp, mode, *, title='CLEAN', bpm=None, tuner=False):
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
      const colors = ['#ef4444','#10b981','#3b82f6','#f59e0b','#a855f7',
                      '#fb7185','#06b6d4','#84cc16','#e879f9','#f97316'];
      await push({type:'PATCH', bank:1, slot:1, patch:{name:'CLEAN', bindings:
        ['1','2','3','4','up','A','B','C','D','down'].map((switchId, index) => ({
          switch:switchId, mode:'latched', label:labels[index], actions:{}, led:{on:colors[index]}
        }))}});
      await push({type:'CONTEXT', context:{bank:1, slot:1, kemper_rig_name:TITLE,
        kemper_bpm:BPM, expression_mode:MODE, kemper_tuner:TUNER ? 'on' : 'off',
        kemper_tuner_note:'F#', kemper_tuner_deviance:8192}});
      for (const sw of ['3', 'B', 'down'])
        await push({type:'EVENT', event:'binding_fired', switch:sw, action:'toggle_on'});
      await new Promise(resolve => setTimeout(resolve, 180));
      return true;
    })()""".replace("MODE", json.dumps(mode))
            .replace("TITLE", json.dumps(title)).replace("BPM", json.dumps(bpm))
            .replace("TUNER", json.dumps(tuner)),
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


async def steady_active_cards(cdp, *, expect_active=None, fixture_colors=False):
    expression = """(() => {
      const cards = [...document.querySelectorAll('.stage__switch')];
      const animations = document.getAnimations().filter(a =>
        a.effect?.target?.matches?.('.stage__switch'));
      return {active:cards.filter(el => el.classList.contains('stage__switch--active')).length,
        reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,
        animations:animations.map(animation => {
          const timing = animation.effect.getTiming(), frames = animation.effect.getKeyframes();
          return {id:animation.effect.target.querySelector('.stage__switch-id').textContent.trim(),
            pseudo:animation.effect.pseudoElement, running:animation.playState === 'running',
            duration:timing.duration, iterations:String(timing.iterations),
            properties:[...new Set(frames.flatMap(frame => Object.keys(frame).filter(key =>
              !['offset','computedOffset','easing','composite'].includes(key))))],
            opacities:frames.map(frame => Number(frame.opacity))};
        }),
        cards:cards.map(el => {
          const css = getComputedStyle(el), marker = getComputedStyle(el, '::after');
          const before = getComputedStyle(el, '::before'), box = el.getBoundingClientRect();
          return {id:el.querySelector('.stage__switch-id').textContent.trim(),
            active:el.classList.contains('stage__switch--active'),
            box:[box.left, box.top, box.width, box.height],
            animations:[css.animationName, before.animationName, marker.animationName],
            led:css.getPropertyValue('--switch-led').trim(),
            borders:[css.borderTopColor, css.borderRightColor, css.borderBottomColor, css.borderLeftColor],
            marker:marker.backgroundColor, opacity:marker.opacity, height:parseFloat(marker.height),
            markerGeometry:[marker.left, marker.right, marker.top, marker.bottom, marker.width, marker.transform],
            background:css.backgroundColor, transform:css.transform,
            shadow:css.boxShadow, markerShadow:marker.boxShadow};
        })};
    })()"""
    state = await cdp.evaluate(expression)
    if expect_active is not None:
        assert state['active'] == expect_active, state
    active_ids = {card['id'] for card in state['cards'] if card['active']}
    if state['reduced']:
        assert not state['animations'], ('Reduced-motion LEDs are animated: ' + json.dumps(state))
    else:
        assert len(state['animations']) == state['active'], state
        assert {animation['id'] for animation in state['animations']} == active_ids, state
        for animation in state['animations']:
            assert animation['pseudo'] == '::after', ('Pulse is not limited to the active LED bar: ' + json.dumps(state))
            assert_light_pulse(animation)
    expected = {'3':'#3b82f6', 'B':'#06b6d4', 'DOWN':'#f97316'}
    for card in state['cards']:
        assert card['animations'][:2] == ['none', 'none'], card
        if not card['active'] or state['reduced']:
            assert card['animations'][2] == 'none', card
        else:
            assert card['animations'][2] != 'none', card
            assert .3499 <= float(card['opacity']) <= 1, card
        if not card['active']:
            continue
        color = card['led']
        assert len(color) == 7 and color.startswith('#'), card
        rgb = 'rgb(%s)' % ', '.join(str(int(color[index:index + 2], 16)) for index in (1, 3, 5))
        assert card['borders'] == [rgb] * 4, card
        assert card['marker'] == rgb and card['height'] >= 2, card
        if state['reduced']:
            assert card['opacity'] == '1', card
        if fixture_colors:
            assert color == expected[card['id']], card
    # Read the same rendered geometry and colours again without pausing or
    # rewriting CSS. Only active-bar opacity may change; inactive bars,
    # panel geometry, borders and assigned LED colours must remain steady.
    def stable_properties(snapshot):
        return {**snapshot, 'cards':[
            {key: value for key, value in card.items()
             if key != 'opacity' or not card['active'] or snapshot['reduced']}
            for card in snapshot['cards']]}
    await asyncio.sleep(.16)
    assert stable_properties(await cdp.evaluate(expression)) == stable_properties(state), (
        'LED colour, inactive bar or panel geometry changed: ' + json.dumps(state))
    return state


def assert_light_pulse(animation):
    assert animation['running'] and animation['iterations'] == 'Infinity', animation
    assert animation['duration'] == 3000, animation
    assert animation['properties'] == ['opacity'], ('Light pulse animates properties other than opacity: ' + json.dumps(animation))
    low, high = min(animation['opacities']), max(animation['opacities'])
    assert abs(low - .35) < .0001 and high == 1, animation


async def assert_divider_motion(cdp):
    state = await cdp.evaluate("""(() => {
      const divider = document.querySelector('.stage__divider');
      if (!divider) return {error:'Missing header divider'};
      const animations = document.getAnimations().filter(a => a.effect?.target === divider);
      return {layers:['::before','::after'].map(pseudo => {
        const css = getComputedStyle(divider, pseudo);
        return {pseudo, background:css.backgroundImage, filter:css.filter, transform:css.transform};
      }), animations:animations.map(animation => {
        const timing = animation.effect.getTiming(), frames = animation.effect.getKeyframes();
        return {pseudo:animation.effect.pseudoElement,
          running:animation.playState === 'running', duration:timing.duration,
          iterations:String(timing.iterations), properties:[...new Set(frames.flatMap(frame =>
            Object.keys(frame).filter(key => !['offset','computedOffset','easing','composite'].includes(key))))],
          opacities:frames.map(frame => Number(frame.opacity))};
      })};
    })()""")
    assert 'error' not in state, state
    assert all('linear-gradient(' in layer['background'] and layer['filter'] == 'none'
               and layer['transform'] == 'none' for layer in state['layers']), state
    assert len(state['animations']) == 2, state
    animations = {animation['pseudo']: animation for animation in state['animations']}
    assert set(animations) == {'::before', '::after'}, state
    assert_light_pulse(animations['::before'])
    wide = animations['::after']
    assert wide['running'] and wide['iterations'] == 'Infinity' and wide['duration'] == 3000, state
    assert wide['properties'] == ['opacity'], state
    assert min(wide['opacities']) == 0 and max(wide['opacities']) == 1, state


async def assert_pulse_synchronization(cdp, *, exercise_activation=False):
    """Inspect the actual CSS animations; activation uses only preview messages."""
    expression = """(() => {
      const animations = document.getAnimations().filter(animation =>
        animation.effect?.target?.matches?.('.stage__divider,.stage__switch') &&
        /stage-(light-pulse|divider-wide-pulse)$/.test(animation.animationName || ''));
      const cards = [...document.querySelectorAll('.stage__switch')];
      return {animations:animations.map(animation => ({
        id:animation.effect.target.querySelector('.stage__switch-id')?.textContent.trim() || 'divider',
        pseudo:animation.effect.pseudoElement, start:animation.startTime,
        time:animation.currentTime, duration:animation.effect.getTiming().duration,
        progress:animation.effect.getComputedTiming().progress,
        opacity:Number(getComputedStyle(animation.effect.target, animation.effect.pseudoElement).opacity),
      })), cards:cards.map(card => ({
        id:card.querySelector('.stage__switch-id').textContent.trim(),
        active:card.classList.contains('stage__switch--active'),
        animation:getComputedStyle(card, '::after').animationName,
        opacity:Number(getComputedStyle(card, '::after').opacity),
      }))};
    })()"""

    async def synchronized(expected_active=None):
        state = await cdp.evaluate(expression)
        active = [card for card in state['cards'] if card['active']]
        if expected_active is not None:
            assert len(active) == expected_active, state
        pulses = state['animations']
        assert len(pulses) == len(active) + 2, state
        assert all(pulse['duration'] == 3000 and pulse['start'] is not None
                   and pulse['time'] is not None and pulse['progress'] is not None for pulse in pulses), state
        assert max(pulse['start'] for pulse in pulses) - min(pulse['start'] for pulse in pulses) < .01, (
            'Pulse animations have different timeline origins: ' + json.dumps(state))
        assert max(pulse['time'] for pulse in pulses) - min(pulse['time'] for pulse in pulses) < .01, state
        assert max(pulse['progress'] for pulse in pulses) - min(pulse['progress'] for pulse in pulses) < .00001, state
        lights = [pulse for pulse in pulses if pulse['id'] != 'divider' or pulse['pseudo'] == '::before']
        assert max(pulse['opacity'] for pulse in lights) - min(pulse['opacity'] for pulse in lights) < .0001, (
            'Active bars and divider light are visibly out of phase: ' + json.dumps(state))
        assert all(card['animation'] == 'none' for card in state['cards'] if not card['active']), state
        return state

    baseline = await synchronized(3 if exercise_activation else None)
    if not exercise_activation:
        return baseline
    origin = baseline['animations'][0]['start']

    async def toggle(action):
        await cdp.evaluate("""(async () => {
          if (!window.__stageDoorbell) throw new Error('Pulse activation requires the local preview');
          window.__stageInbox = [JSON.stringify({type:'EVENT', event:'binding_fired', switch:'1', action:ACTION})];
          window.__stageDoorbell();
          await new Promise(resolve => setTimeout(resolve, 80));
        })()""".replace('ACTION', json.dumps(action)), await_promise=True)

    await asyncio.sleep(.73)
    await toggle('toggle_on')
    activated = await synchronized(4)
    assert activated['animations'][0]['start'] == origin, activated
    await toggle('toggle_off')
    inactive = await synchronized(3)
    first = next(card for card in inactive['cards'] if card['id'] == '1')
    assert not first['active'] and first['animation'] == 'none', inactive
    await asyncio.sleep(.41)
    assert next(card for card in (await cdp.evaluate(expression))['cards'] if card['id'] == '1') == first
    await toggle('toggle_on')
    reactivated = await synchronized(4)
    assert reactivated['animations'][0]['start'] == origin, reactivated
    await cdp.command('Emulation.setEmulatedMedia', {'features':[
        {'name':'prefers-reduced-motion', 'value':'reduce'}]})
    try:
        await asyncio.sleep(.1)
        reduced = await cdp.evaluate(expression)
        assert not reduced['animations'], reduced
        assert all(card['animation'] == 'none' for card in reduced['cards']), reduced
        assert all(card['opacity'] == 1 for card in reduced['cards'] if card['active']), reduced
    finally:
        await cdp.command('Emulation.setEmulatedMedia', {'features':[
            {'name':'prefers-reduced-motion', 'value':'no-preference'}]})
    await asyncio.sleep(.1)
    resumed = await synchronized(4)
    assert resumed['animations'][0]['start'] == origin, resumed
    # Same-patch reads preserve confirmed latches. Restore the fixture before
    # the broader layout scenarios, which expect its original three active LEDs.
    await toggle('toggle_off')
    return {'baseline': baseline, 'late_activation': activated, 'reactivation': reactivated,
            'reduced_motion': reduced, 'resumed': resumed}


async def capture_divider_phases(cdp, path):
    # Capture the actual static gradients at the two CSS animation phases.
    # This changes browser animation time only, never firmware or layout CSS.
    await cdp.evaluate("""(() => {
      // Advance all synchronized light layers together for these controlled
      // captures; freezing only the divider would create artificial phase drift.
      window.__dividerCapture = document.getAnimations().filter(a =>
        a.effect?.target?.matches?.('.stage__divider,.stage__switch') &&
        /stage-(light-pulse|divider-wide-pulse)$/.test(a.animationName || ''))
        .map(animation => ({animation, time:animation.currentTime, start:animation.startTime, state:animation.playState}));
      for (const {animation} of window.__dividerCapture) animation.pause();
    })()""")
    snapshots = []
    try:
        for name, time in [('minimum', 0), ('peak', 1500)]:
            await cdp.evaluate("""(() => {
              for (const {animation} of window.__dividerCapture) animation.currentTime = TIME;
            })()""".replace('TIME', str(time)))
            snapshot = await cdp.evaluate("""(() => {
              const divider = document.querySelector('.stage__divider'), box = divider.getBoundingClientRect();
              return {box:[box.left, box.top, box.width, box.height], layers:['::before','::after'].map(pseudo => {
                const css = getComputedStyle(divider, pseudo);
                return {opacity:Number(css.opacity), background:css.backgroundImage,
                  geometry:[css.left, css.top, css.width, css.height, css.transform], filter:css.filter};
              })};
            })()""")
            snapshots.append(snapshot)
            shot = await cdp.command('Page.captureScreenshot', {'format':'png'})
            path.with_name(path.stem + '-divider-' + name + path.suffix).write_bytes(base64.b64decode(shot['data']))
        minimum, peak = snapshots
        assert minimum['layers'][1]['opacity'] == 0 and peak['layers'][1]['opacity'] == 1, snapshots
        assert minimum['box'] == peak['box'], snapshots
        for low, high in zip(minimum['layers'], peak['layers']):
            assert {key: value for key, value in low.items() if key != 'opacity'} == {
                key: value for key, value in high.items() if key != 'opacity'}, snapshots
    finally:
        await cdp.evaluate("""(() => {
          for (const {animation, time, start, state} of window.__dividerCapture || []) {
            if (state === 'running') {
              animation.play();
              animation.startTime = start;
            } else animation.currentTime = time;
          }
          delete window.__dividerCapture;
        })()""")


def assert_geometry(state, *, expect_mode=None, expect_title=None, expect_bpm=None, expect_tuner=False):
    width, height = state['width'], state['height']
    assert abs(state['stage']['height'] - height) < 1, (
        'Stage overflows viewport: ' + json.dumps(state))
    assert len(state['cards']) == 10, state
    divider = state['divider']
    assert divider is not None and state['dividerAfterHeader'], 'Missing divider immediately after the header'
    assert divider['top'] > state['header']['bottom'], state
    assert divider['bottom'] < min(card['top'] for card in state['cards']), state
    assert divider['left'] >= 1 and divider['right'] <= width - 1, state
    assert 0 < divider['height'] <= 12, state
    header_style = state['headerStyle']
    assert header_style['background'] == 'rgba(0, 0, 0, 0)' and header_style['image'] == 'none', state
    assert header_style['shadow'] == 'none', state
    assert header_style['border'] == [0] * 4 and header_style['padding'] == [0] * 4, state
    assert len(state['headerBorders']) == 7, state
    reference_border = state['headerBorders'][0][0]
    assert reference_border['width'] == '1px' and reference_border['style'] == 'solid', state
    assert all(side == reference_border for border in state['headerBorders'] for side in border), (
        'Header panel and button borders differ: ' + json.dumps(state))
    for card in state['cards']:
        assert card['left'] >= 1 and card['right'] <= width - 1, state
        assert card['bottom'] <= height - 1, ('Bottom border clipped: ' + json.dumps(state))
        assert card['top'] >= state['header']['bottom'] + 1, state
        assert card['height'] > 30, state
    badge = state['expression']
    assert badge is not None, 'Missing expression pedal indicator'
    assert badge['bottom'] <= state['header']['bottom'], state
    header = state['header']
    controls = state['controls']
    assert controls is not None, 'Missing Stage controls'
    bank_controls = state['bankControls']
    assert bank_controls is not None, 'Missing bank navigation controls'
    assert state['bankControlsInHeader'], 'Bank navigation must be a direct header group'
    group_style = state['bankGroupStyle']
    assert group_style['background'] == 'rgba(0, 0, 0, 0)' and group_style['image'] == 'none', state
    assert group_style['border'] == [0] * 4 and group_style['padding'] == [0] * 4, (
        'Bank navigation has an unwanted outer frame: ' + json.dumps(state))
    assert abs(group_style['gap'] - group_style['controlsGap']) <= .5, state
    assert abs(bank_controls['width'] - controls['width']) <= .5, state
    assert state['controlsVisible'], 'Stage controls must be visible without hover or focus'
    assert controls['left'] >= badge['right'] + 1, ('Stage controls are not right of the expression indicator: ' + json.dumps(state))
    assert abs(header['right'] - controls['right'] - state['headerPaddingRight']) <= 1, state
    assert bank_controls['right'] < state['meta']['left'], ('Bank navigation is not left of the metadata: ' + json.dumps(state))
    elements = [state['rig'], bank_controls, state['meta'], badge, controls]
    for element in elements:
        assert element['left'] >= header['left'] - .5, state
        assert element['right'] <= header['right'] + .5, state
        assert element['top'] >= header['top'] - .5, state
        assert element['bottom'] <= header['bottom'] + .5, state
    def no_overlap(boxes, message):
        for index, left in enumerate(boxes):
            for right in boxes[index + 1:]:
                overlap_x = min(left['right'], right['right']) - max(left['left'], right['left'])
                overlap_y = min(left['bottom'], right['bottom']) - max(left['top'], right['top'])
                assert overlap_x <= .5 or overlap_y <= .5, (message + ': ' + json.dumps(state))
    no_overlap(elements, 'Header elements overlap')
    no_overlap(state['cards'], 'Pedal cards overlap')
    assert len(state['buttons']) == 2, state
    for box in [controls, *state['buttons']]:
        assert box['left'] >= 0 and box['right'] <= width and box['top'] >= 0 and box['bottom'] <= height, state
    for button in state['buttons']:
        assert button['left'] >= controls['left'] - .5 and button['right'] <= controls['right'] + .5, state
        assert button['top'] >= controls['top'] - .5 and button['bottom'] <= controls['bottom'] + .5, state
    no_overlap(state['buttons'], 'Stage controls overlap')
    upper, lower = state['buttons']
    assert upper['label'] == 'Exit Stage' and lower['label'] == 'Stage appearance', state
    assert upper['bottom'] < lower['top'], ('Stage controls are not stacked vertically: ' + json.dumps(state))
    assert abs(upper['left'] - lower['left']) <= .5 and abs(upper['right'] - lower['right']) <= .5, state
    for panel in (bank_controls, state['meta'], controls):
        assert abs(panel['top'] - badge['top']) <= .5, state
        assert abs(panel['height'] - badge['height']) <= .5, ('Header readout and control panels have different heights: ' + json.dumps(state))
    if state['coarsePointer']:
        assert all(button['width'] >= 40 and button['height'] >= 40 for button in state['buttons']), state
    assert len(state['bankButtons']) == 2, state
    for box in state['bankButtons']:
        assert box['left'] >= bank_controls['left'] - .5 and box['right'] <= bank_controls['right'] + .5, state
        assert box['top'] >= bank_controls['top'] - .5 and box['bottom'] <= bank_controls['bottom'] + .5, state
    no_overlap(state['bankButtons'], 'Bank navigation buttons overlap')
    following, previous = state['bankButtons']
    assert following['text'] == '+' and previous['text'] == '\u2212', state
    assert following['label'] == 'Next bank' and previous['label'] == 'Previous bank', state
    assert following['bottom'] < previous['top'], ('Bank navigation buttons are not stacked vertically: ' + json.dumps(state))
    assert abs(previous['left'] - following['left']) <= .5 and abs(previous['right'] - following['right']) <= .5, state
    for button in state['bankButtons']:
        assert button['visible'], 'Bank navigation must remain visible without hover, even while disabled'
        assert button['width'] >= 48 and button['height'] >= (40 if state['coarsePointer'] else 30), state
    meta_style = state['metaStyle']
    assert meta_style['background'] == 'rgba(0, 0, 0, 0)' and meta_style['image'] == 'none', state
    assert meta_style['shadow'] == 'none', state
    assert meta_style['border'] == [0] * 4 and meta_style['padding'] == [0] * 4, state
    assert state['bankReadout'] is not None and state['rigReadout'] is not None, 'Missing separate bank and rig readouts'
    bank_box, rig_box = state['bankReadout'], state['rigReadout']
    assert bank_box['right'] < rig_box['left'], ('Bank and rig readouts must stay side by side: ' + json.dumps(state))
    assert abs(bank_box['top'] - rig_box['top']) <= .5 and abs(bank_box['height'] - rig_box['height']) <= .5, state
    # BPM/tuner may occupy a second line on narrow screens. Both readouts
    # still share the first line; expression and controls span the full meta.
    wrapped_metadata = any(state[key] is not None and state[key]['top'] >= bank_box['bottom'] - .5
                           for key in ('bpm', 'tuner', 'navigationError'))
    for outer, inner in [('bankReadout', 'bank'), ('rigReadout', 'rigPosition')]:
        box, frame = state[outer], state[inner]
        assert frame is not None and frame['width'] >= 30, ('Bank or rig marquee collapsed: ' + json.dumps(state))
        assert frame['left'] >= box['left'] - .5 and frame['right'] <= box['right'] + .5, state
        assert frame['top'] >= box['top'] - .5 and frame['bottom'] <= box['bottom'] + .5, state
        assert abs(box['top'] - badge['top']) <= .5, state
        if not wrapped_metadata:
            assert abs(box['height'] - badge['height']) <= .5, ('Bank or rig readout does not align with the expression panel: ' + json.dumps(state))
    assert '\u00b7' not in state['bankText'] and '\u00b7' not in state['rigPositionText'], state
    metadata = [state[key] for key in ('bankReadout', 'rigReadout', 'bpm', 'tuner', 'navigationError') if state[key] is not None]
    no_overlap(metadata, 'Bank readout, BPM, tuner or error overlap')
    for box in metadata:
        assert box['left'] >= state['meta']['left'] - .5 and box['right'] <= state['meta']['right'] + .5, state
        assert box['top'] >= state['meta']['top'] - .5 and box['bottom'] <= state['meta']['bottom'] + .5, state
    if state['tuner'] is not None:
        assert state['tuner']['left'] >= state['meta']['left'] - .5, state
        assert state['tuner']['right'] <= state['meta']['right'] + .5, state
    if expect_tuner:
        assert state['tuner'] is not None and state['tunerText'].startswith('F#'), state
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


async def assert_marquee_motion(cdp):
    result = await cdp.evaluate("""(() => {
      return [...document.querySelectorAll('.stage__marquee-active')].map(track => {
        const animation = track.getAnimations().find(a => (a.animationName || '').includes('stage-marquee'));
        if (!animation) return {error:'Overflowing text has no marquee animation', text:track.textContent};
        const frame = track.parentElement;
        const measure = () => ({frameLeft:frame.getBoundingClientRect().left,
          frameRight:frame.getBoundingClientRect().right, left:track.getBoundingClientRect().left,
          right:track.getBoundingClientRect().right});
        animation.pause(); animation.currentTime = 0;
        const start = measure();
        animation.currentTime = 2500;
        const end = measure();
        animation.play();
        return {text:track.textContent, start, end, overflow:getComputedStyle(frame).overflowX};
      });
    })()""")
    assert result, 'Long title should exercise a real marquee'
    for track in result:
        assert 'error' not in track, track
        start, end = track['start'], track['end']
        assert track['overflow'] == 'hidden', track
        assert abs(start['frameLeft'] - end['frameLeft']) < .1, track
        assert abs(start['frameRight'] - end['frameRight']) < .1, track
        assert end['left'] < start['left'] - 2, track
        assert abs(start['left'] - start['frameLeft']) <= 2, track
        assert abs(end['right'] - end['frameRight']) <= 2, track


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
            # Five navigation names may arrive just before the active state.
            # Let the finite 150 ms border transition finish before measuring
            # LED colours. This is a layout check, not the cold-start timing
            # benchmark; only active-bar opacity may animate below.
            await asyncio.sleep(.25)
            cases = [('live', None, None, False)]
        else:
            cases = [('short', 'CLEAN', None, False),
                     ('long-title', 'Vintage Deluxe Reverb Ultra Mega Long Rig Name', 120, False),
                     ('long-title-high-bpm', 'CRUNCH BOOST DELAY REVERB LEAD', 250, False),
                     ('long-title-bpm-tuner', 'Vintage Deluxe Reverb Ultra Mega Long Rig Name', 250, True)]
        await assert_divider_motion(cdp)
        print('PASS two fixed divider gradients with opacity-only pulse', flush=True)
        if not args.live:
            await fixture(cdp, 'VOL')
        await assert_pulse_synchronization(cdp, exercise_activation=not args.live)
        print('PASS synchronized divider and active LED pulses' +
              (' (passive snapshot)' if args.live else ' including late activation, reactivation and reduced-motion resume'), flush=True)
        if args.pulse_only:
            return
        for case, title, bpm, tuner in cases:
            if not args.live:
                await fixture(cdp, 'VOL', title=title, bpm=bpm, tuner=tuner)
            for width, height in VIEWPORTS:
                await cdp.command('Emulation.setDeviceMetricsOverride', {
                    'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
                })
                await asyncio.sleep(.1)
                # The active bottom-right panel must remain fully visible;
                # pulsing LEDs retain their assigned colour and fixed shape.
                await steady_active_cards(cdp, expect_active=None if args.live else 3,
                                          fixture_colors=not args.live)
                state = await cdp.evaluate(GEOMETRY)
                assert_geometry(state, expect_mode=None if args.live else 'VOL',
                                expect_title=title, expect_bpm=bpm, expect_tuner=tuner)
                if not args.live and case == 'long-title':
                    await assert_marquee_motion(cdp)
                print('PASS geometry %s %dx%d (stable panels, opacity-only active LEDs and vertical controls)' % (case,width,height), flush=True)
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
            # A bank refresh briefly replaces the expression mode with ---.
            # Neither that placeholder nor WAH may move any header readout.
            for width, height in VIEWPORTS:
                await cdp.command('Emulation.setDeviceMetricsOverride', {
                    'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
                })
                baseline = None
                for mode, expected in [('VOL', 'VOL'), ('', '---'), ('WAH', 'WAH'), ('', '---')]:
                    await fixture(cdp, mode, title='CLEAN', bpm=250, tuner=True)
                    state = await cdp.evaluate(GEOMETRY)
                    assert_geometry(state, expect_mode=expected, expect_title='CLEAN',
                                    expect_bpm=250, expect_tuner=True)
                    positions = {key: state[key] for key in (
                        'header', 'rig', 'bankControls', 'bankButtons', 'meta', 'bankReadout',
                        'rigReadout', 'bank', 'rigPosition', 'bpm', 'tuner', 'expression', 'controls', 'buttons')}
                    if baseline is None:
                        baseline = positions
                    assert positions == baseline, ('Expression mode shifted the header: ' + json.dumps({
                        'viewport': [width, height], 'mode':expected,
                        'before':baseline, 'after':positions}))
                print('PASS stable header through VOL/---/WAH/--- %dx%d' % (width, height), flush=True)
        # CDP touch emulation changes the real pointer media feature. Include
        # narrow portrait geometry with controls actually visible and >=40px.
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':True, 'maxTouchPoints':1})
        if not args.live:
            await fixture(cdp, 'VOL', title='CLEAN', bpm=250, tuner=True)
        for width, height in ((320, 568), (375, 667), (800, 480)):
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
            })
            await asyncio.sleep(.2)
            state = await cdp.evaluate(GEOMETRY)
            assert state['coarsePointer'], 'Browser did not enable coarse-pointer media emulation'
            assert_geometry(state, expect_mode=None if args.live else 'VOL',
                            expect_bpm=None if args.live else 250, expect_tuner=not args.live)
            print('PASS coarse-pointer controls %dx%d' % (width, height), flush=True)
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':False})
        if not args.live:
            await fixture(cdp, 'VOL', title='Vintage Deluxe Reverb Ultra Mega Long Rig Name', bpm=120)
        await cdp.command('Emulation.setEmulatedMedia', {'features':[
            {'name':'prefers-reduced-motion', 'value':'reduce'}]})
        await asyncio.sleep(.2)
        assert_geometry(await cdp.evaluate(GEOMETRY))
        await steady_active_cards(cdp, expect_active=None if args.live else 3, fixture_colors=not args.live)
        motion = await cdp.evaluate("""({reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,
          animations:document.getAnimations().filter(a => a.effect?.target?.closest?.('.stage'))
            .map(a => a.animationName || a.transitionProperty)})""")
        assert motion['reduced'] and not motion['animations'], motion
        print('PASS reduced-motion display and static LED bars', flush=True)
        await cdp.command('Emulation.setEmulatedMedia', {'features':[
            {'name':'prefers-reduced-motion', 'value':'no-preference'}]})
        if args.screenshot:
            if not args.live:
                await fixture(cdp, 'VOL', title='CLEAN')
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':1045, 'height':399, 'deviceScaleFactor':1, 'mobile':False,
            })
            await asyncio.sleep(.1)
            shot = await cdp.command('Page.captureScreenshot', {'format':'png'})
            Path(args.screenshot).write_bytes(base64.b64decode(shot['data']))
            await capture_divider_phases(cdp, Path(args.screenshot))
            print('PASS divider minimum/peak: fixed gradients and geometry', flush=True)
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
    parser.add_argument('--pulse-only', action='store_true', help='Run only divider/pulse checks; --live remains passive')
    parser.add_argument('--screenshot')
    asyncio.run(run(parser.parse_args()))
