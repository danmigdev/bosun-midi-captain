"""Exercise the fullscreen bank picker through real Chromium input, without hardware.

Run the Stage dev preview first. The script refuses remote URLs and uses an
isolated browser profile plus the preview's local firmware-message peer. Its
99-bank screenshot is synthetic fixture data, not the connected Captain.
Explicit --live permits only a passive open/inspect/close on the configured Pi:
it never clicks any bank tile and checks the WebSocket trace for mutations.
With --live --live-preselect, an isolated browser additionally previews another
existing bank, cancels without choosing a rig and restores its local setting.

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
from urllib.parse import urlparse

from browser_stage_layout import VIEWPORTS, fixture
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


PEER = r"""(() => {
  const peer = window.__bankPeer = {
    current: {bank: 1, slot: 4}, commands: [], replies: [], hold: false, reject: false,
    pending: null,
    patches: Array.from({length: 99}, (_, index) => index + 1).flatMap(bank =>
      (bank === 99 ? [2, 4] : bank % 2 ? [1, 4] : [2]).map(slot =>
        ({bank, slot, name: `Bank ${bank} Rig ${slot}`}))),
  };
  peer.push = message => {
    peer.replies.push(message);
    window.__stageInbox = [...(window.__stageInbox || []), JSON.stringify(message)];
    window.__stageDoorbell();
  };
  peer.finish = () => {
    const message = peer.pending;
    if (!message) throw new Error('No pending bank command');
    peer.pending = null;
    if (peer.reject) peer.push({type: 'ERROR', id: message.id, error: 'busy'});
    else {
      peer.current = {bank: message.bank, slot: message.slot};
      peer.push({type: 'ACK', id: message.id});
    }
  };
  window.__stageCommand = message => {
    peer.commands.push(message);
    setTimeout(() => {
      switch (message.type) {
        case 'GET_DEVICE_INFO':
          peer.push({type: 'DEVICE_INFO', id: message.id, fw: '0.6.5-native',
            device: 'midi_captain_10', stage_input: true, current: {...peer.current}});
          break;
        case 'LIST_PATCHES':
          peer.push({type: 'PATCH_LIST', id: message.id, patches: peer.patches});
          break;
        case 'GET_PATCH':
          peer.push({type: 'PATCH', id: message.id, bank: message.bank, slot: message.slot,
            patch: {name: 'BANK PICKER PREVIEW', bindings: []}});
          break;
        case 'GET_CONTEXT':
          peer.push({type: 'CONTEXT', id: message.id, context: {}});
          break;
        case 'SWITCH_PATCH':
          peer.pending = message;
          if (!peer.hold) peer.finish();
          break;
      }
    }, 5);
  };
  return true;
})()"""

GEOMETRY = r"""(() => {
  const dialog = document.querySelector('.bank-picker');
  if (!dialog) return null;
  const rect = el => {
    const r = el.getBoundingClientRect();
    return {x:r.x, y:r.y, width:r.width, height:r.height,
      right:r.right, bottom:r.bottom};
  };
  const body = dialog.querySelector('.bank-picker__body');
  const modeTrigger = dialog.querySelector('.bank-picker__mode-trigger[role="combobox"][aria-label="Bank selection"]');
  const modeList = dialog.querySelector('[role="listbox"][aria-label="Bank selection mode"]');
  const buttons = [...dialog.querySelectorAll('.bank-picker__bank')];
  return {
    viewport:{width:innerWidth,height:innerHeight},
    dialog:rect(dialog), modal:dialog.matches(':modal'),
    header:rect(dialog.querySelector('.bank-picker__header')),
    stageHeader:rect(document.querySelector('.stage__header')),
    close:rect(dialog.querySelector('.bank-picker__close')),
    stageClose:rect(document.querySelector('.stage__icon-btn[aria-label="Exit Stage"]')),
    mode:rect(modeTrigger),
    coarse:matchMedia('(pointer: coarse)').matches,
    modeContent:{scrollHeight:modeTrigger.scrollHeight,clientHeight:modeTrigger.clientHeight,
      scrollWidth:modeTrigger.scrollWidth,clientWidth:modeTrigger.clientWidth},
    modeFont:{family:getComputedStyle(modeTrigger).fontFamily,size:getComputedStyle(modeTrigger).fontSize},
    bankFont:{family:getComputedStyle(document.querySelector('.stage__bank')).fontFamily,
      size:getComputedStyle(document.querySelector('.stage__bank')).fontSize},
    modeList:modeList ? {...rect(modeList), scrollHeight:modeList.scrollHeight, clientHeight:modeList.clientHeight} : null,
    modeOptions:[...dialog.querySelectorAll('[role="listbox"][aria-label="Bank selection mode"] [role="option"]')].map(button => ({
      ...rect(button), mode:button.dataset.mode, selected:button.getAttribute('aria-selected'),
      disabled:button.disabled, scrollHeight:button.scrollHeight, clientHeight:button.clientHeight,
    })),
    body:rect(body), scrollTop:body.scrollTop, scrollHeight:body.scrollHeight,
    clientHeight:body.clientHeight, clientWidth:body.clientWidth, scrollWidth:body.scrollWidth,
    buttons:buttons.map(button => ({...rect(button), disabled:button.disabled,
      label:button.getAttribute('aria-label'), current:button.getAttribute('aria-current')})),
    columns:getComputedStyle(dialog.querySelector('.bank-picker__grid')).gridTemplateColumns.split(' ').length,
    documentWidth:document.documentElement.scrollWidth,
  };
})()"""


async def wait_for(cdp, expression, description):
    for _ in range(400):
        result = await cdp.evaluate(expression)
        if result:
            return result
        await asyncio.sleep(.03)
    raise AssertionError('Timed out: ' + description)


async def mouse(cdp, selector):
    point = await cdp.evaluate("""(() => {
      const el = document.querySelector(SELECTOR);
      if (!el) throw new Error('Missing mouse target: ' + SELECTOR);
      const r = el.getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2};
    })()""".replace('SELECTOR', json.dumps(selector)))
    assert 0 <= point['x'] and 0 <= point['y'], point
    for kind in ('mousePressed', 'mouseReleased'):
        await cdp.command('Input.dispatchMouseEvent', {
            'type':kind, **point, 'button':'left', 'clickCount':1,
        })
    await asyncio.sleep(.05)


async def escape(cdp):
    for kind in ('keyDown', 'keyUp'):
        await cdp.command('Input.dispatchKeyEvent', {
            'type':kind, 'key':'Escape', 'code':'Escape', 'windowsVirtualKeyCode':27,
        })
    await asyncio.sleep(.05)


async def key(cdp, name, code):
    for kind in ('keyDown', 'keyUp'):
        await cdp.command('Input.dispatchKeyEvent', {
            'type':kind, 'key':name, 'code':name, 'windowsVirtualKeyCode':code,
        })
    await asyncio.sleep(.05)


async def touch(cdp, selector):
    point = await cdp.evaluate("""(() => {
      const element = document.querySelector(SELECTOR);
      if (!element) throw new Error('Missing touch target: ' + SELECTOR);
      const box = element.getBoundingClientRect();
      return {x:box.x + box.width / 2, y:box.y + box.height / 2};
    })()""".replace('SELECTOR', json.dumps(selector)))
    await cdp.command('Input.dispatchTouchEvent', {'type':'touchStart', 'touchPoints':[point]})
    await cdp.command('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
    await asyncio.sleep(.06)


async def choose_mode(cdp, mode, *, use_touch=False):
    """Choose an expanded, full-height mode using real pointer or touch input."""
    trigger = '.bank-picker__mode-trigger'
    selector = '[role="listbox"][aria-label="Bank selection mode"] [role="option"][data-mode=' + json.dumps(mode) + ']'
    await wait_for(cdp, '!!document.querySelector(' + json.dumps(trigger) + ')', 'Bank selection setting')
    await (touch if use_touch else mouse)(cdp, trigger)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-expanded") === "true"',
                   'Expanded bank selection modes')
    verify_mode_geometry(await cdp.evaluate(GEOMETRY), expanded=True)
    await (touch if use_touch else mouse)(cdp, selector)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger) + ').dataset.mode === ' + json.dumps(mode),
                   'Selected bank mode ' + mode)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-expanded")') == 'false'


async def wheel(cdp, direction):
    geometry = await cdp.evaluate(GEOMETRY)
    body = geometry['body']
    await cdp.command('Input.dispatchMouseEvent', {
        'type':'mouseWheel', 'x':body['x'] + body['width'] / 2,
        'y':body['y'] + body['height'] / 2, 'deltaX':0, 'deltaY':direction * 25000,
    })
    await asyncio.sleep(.16)
    return await cdp.evaluate(GEOMETRY)


async def open_picker(cdp):
    await mouse(cdp, '.stage__bank-readout')
    await wait_for(cdp,
        "document.querySelectorAll('.bank-picker__bank:not(:disabled)').length === 99",
        '99 selectable fixture banks')


async def switches(cdp):
    return await cdp.evaluate("window.__bankPeer.commands.filter(m => m.type === 'SWITCH_PATCH')")


async def mutations(cdp):
    return await cdp.evaluate("window.__bankPeer.commands.filter(m => ['SWITCH_PATCH','ACTIVATE_SWITCH'].includes(m.type))")


async def capture(cdp, path):
    screenshot = await cdp.command('Page.captureScreenshot', {'format':'png', 'captureBeyondViewport':False})
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(base64.b64decode(screenshot['data']))


async def keyboard_mode_checks(cdp):
    """Navigate expanded modes using keyboard keys, with no bank action."""
    trigger = '.bank-picker__mode-trigger'
    # The preceding Escape restores trigger focus without leaving the dialog.
    assert await cdp.evaluate('document.activeElement === document.querySelector(' + json.dumps(trigger) + ')')
    await key(cdp, 'Enter', 13)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-expanded") === "true"',
                   'Keyboard opens bank modes')
    for name, code, highlighted in (('End', 35, 'preselect'), ('Home', 36, 'immediate'), ('ArrowDown', 40, 'preselect')):
        await key(cdp, name, code)
        assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-activedescendant")') == 'bank-selection-' + highlighted
        assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger) + ').dataset.mode') == 'immediate', 'Keyboard navigation committed before Enter'
    assert not await mutations(cdp), 'Keyboard mode navigation sent a device action'
    await key(cdp, 'Enter', 13)
    await wait_for(cdp, "localStorage.getItem('BOSUN_STAGE_BANK_SELECTION') === 'preselect'", 'Keyboard commits preselection mode')
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-expanded")') == 'false'
    await key(cdp, 'Enter', 13)
    await wait_for(cdp, 'document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-expanded") === "true"',
                   'Keyboard reopens bank modes')
    await key(cdp, 'ArrowUp', 38)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(trigger) + ').getAttribute("aria-activedescendant")') == 'bank-selection-immediate'
    await key(cdp, 'Enter', 13)
    await wait_for(cdp, "localStorage.getItem('BOSUN_STAGE_BANK_SELECTION') === 'immediate'", 'Keyboard restores immediate mode')
    assert not await mutations(cdp), 'Keyboard mode selection sent a device action'
    print('PASS mode keyboard Enter, End, Home, ArrowDown and ArrowUp; explicit commit, retained dialog, zero device actions', flush=True)


async def enlarged_mode_checks(cdp, args):
    """Font/layout changes keep all header geometry and shared row sizes aligned."""
    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':320, 'height':568, 'deviceScaleFactor':1, 'mobile':False,
    })
    await asyncio.sleep(.1)
    baseline = await cdp.evaluate(GEOMETRY)
    previous_font = await cdp.evaluate("document.documentElement.style.fontSize")
    try:
        await cdp.evaluate("""document.documentElement.style.fontSize =
          (parseFloat(getComputedStyle(document.documentElement).fontSize) * 1.5) + 'px'""")
        await asyncio.sleep(.15)
        await mouse(cdp, '.bank-picker__mode-trigger')
        await wait_for(cdp, "document.querySelector('.bank-picker__mode-trigger').getAttribute('aria-expanded') === 'true'",
                       'Expanded modes at 150 percent font size')
        await asyncio.sleep(.1)
        state = await cdp.evaluate(GEOMETRY)
        verify_mode_geometry(state, expanded=True)
        assert state['documentWidth'] <= 321, state
        if args.screenshot:
            path = Path(args.screenshot)
            await capture(cdp, path.with_name(path.stem + '-mode-expanded-font150-320' + path.suffix))
        await escape(cdp)
        print(f'PASS 320px viewport with 150% root font: mode/options match {state["stageClose"]["height"]:g}px main X without clipping; full header and X viewport rectangles align', flush=True)
    finally:
        await cdp.evaluate('document.documentElement.style.fontSize = ' + json.dumps(previous_font))
    await asyncio.sleep(.1)
    stage_style = await cdp.evaluate("document.querySelector('.stage').getAttribute('style')")
    try:
        await cdp.evaluate("""(() => {
          const style = document.querySelector('.stage').style;
          style.setProperty('--stage-bank-scale', '2');
          style.setProperty('--stage-bank-font', 'monospace');
        })()""")
        await wait_for(cdp, "getComputedStyle(document.querySelector('.stage__bank')).fontFamily === 'monospace'",
                       'Stage Bank display uses its section font override')
        await mouse(cdp, '.bank-picker__mode-trigger')
        await wait_for(cdp, "document.querySelector('.bank-picker__mode-trigger').getAttribute('aria-expanded') === 'true'",
                       'Expanded modes retain common UI typography')
        await asyncio.sleep(.1)
        state = await cdp.evaluate(GEOMETRY)
        verify_mode_geometry(state, expanded=True)
        assert state['documentWidth'] <= 321, state
        assert state['modeFont']['family'] == baseline['modeFont']['family'], state
        assert state['bankFont']['family'] == 'monospace', state
        assert float(state['bankFont']['size'][:-2]) > float(baseline['bankFont']['size'][:-2]), state
        assert await cdp.evaluate("document.querySelector('.stage').style.getPropertyValue('--stage-bank-scale')") == '2'
        if args.screenshot:
            path = Path(args.screenshot)
            await capture(cdp, path.with_name(path.stem + '-mode-expanded-bank-scale2-320' + path.suffix))
        await escape(cdp)
        print(f'PASS 320px viewport with Stage Bank scale2/monospace: independent control font; mode/options match {state["mode"]["height"]:g}px main X and full header geometry', flush=True)
    finally:
        await cdp.evaluate("""(() => {
          const stage = document.querySelector('.stage');
          if (PREVIOUS === null) stage.removeAttribute('style');
          else stage.setAttribute('style', PREVIOUS);
        })()""".replace('PREVIOUS', json.dumps(stage_style)))
    await asyncio.sleep(.1)
    assert not await mutations(cdp), 'Resizing bank modes sent a device action'


async def preselection_checks(cdp, args):
    """Stage previews another bank without a device command until a rig tap."""
    cancel = '[aria-label="Cancel bank preselection"]'
    mode = '.bank-picker__mode-trigger'
    await cdp.evaluate("""(() => {
      const peer = window.__bankPeer;
      peer.current = {bank:1,slot:4}; peer.commands = []; peer.replies = [];
      peer.hold = false; peer.reject = false;
      peer.patches = peer.patches.filter(p => p.bank !== 99).concat(
        Array.from({length:10}, (_, i) => ({bank:99, slot:i+1,
          name:`Rig ${i+1} - Vintage Deluxe Reverb Ultra Long Preselected Sound`})));
    })()""")
    before = await cdp.evaluate("""({
      title:document.querySelector('.stage__rig-name').textContent.trim(),
      bank:document.querySelector('.stage__bank-readout').textContent.trim(),
      rig:document.querySelector('.stage__rig-readout').textContent.trim(),
      current:{...window.__bankPeer.current},
    })""")
    await open_picker(cdp)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(mode) + ').dataset.mode') == 'immediate'
    await choose_mode(cdp, 'preselect')
    assert not await mutations(cdp), 'Changing bank mode sent a device action'
    assert await cdp.evaluate("localStorage.getItem('BOSUN_STAGE_BANK_SELECTION')") == 'preselect'
    await wheel(cdp, 1)
    await mouse(cdp, '.bank-picker__bank[aria-label="Bank 99"]')
    await wait_for(cdp, '!document.querySelector(".bank-picker") && !!document.querySelector(' + json.dumps(cancel) + ')',
                   'Bank selection returns to Stage preselection')
    assert not await mutations(cdp), 'Preselecting a bank sent a device action'
    assert await cdp.evaluate("document.querySelector('.stage').classList.contains('stage--preselect')")
    assert await cdp.evaluate("getComputedStyle(document.querySelector('.stage'), '::after').animationName !== 'none'")
    assert await cdp.evaluate('window.__bankPeer.current') == before['current']
    assert await cdp.evaluate("document.querySelector('.stage__rig-name').textContent.trim()") == before['title']
    assert '99' in await cdp.evaluate("document.querySelector('.stage__bank-readout').textContent.trim()")

    # The fixture's Stage has no preset-navigation overlay, so it must expose
    # the complete ten-slot fallback with the exact names from the inventory.
    for width, height in ((320, 568), (568, 320)):
        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
        })
        await asyncio.sleep(.1)
        state = await cdp.evaluate("""(() => {
          const rect = el => { const r = el.getBoundingClientRect();
            return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height}; };
          return {width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,
            cancel:rect(document.querySelector('[aria-label="Cancel bank preselection"]')),
            cancelText:document.querySelector('[aria-label="Cancel bank preselection"]').textContent.trim(),
            exit:rect(document.querySelector('[aria-label="Exit Stage"]')),
            tiles:[...document.querySelectorAll('.stage__switch')].map(el => ({...rect(el),
              label:el.querySelector('.stage__switch-label').textContent.trim(), disabled:el.disabled}))};
        })()""")
        assert state['documentWidth'] <= width + 1, state
        assert state['cancel']['width'] >= 40, state
        assert abs(state['cancel']['height'] - state['exit']['height']) <= 1, state
        assert abs((state['cancel']['x'] + state['cancel']['right']) / 2 - width / 2) <= 1, state
        assert state['cancelText'] == 'CANCEL', state
        assert len(state['tiles']) == 10, state
        for index, tile in enumerate(state['tiles'], 1):
            assert tile['label'] == f'Rig {index} - Vintage Deluxe Reverb Ultra Long Preselected Sound', tile
            assert not tile['disabled'], tile
            assert tile['x'] >= 0 and tile['right'] <= width + 1, tile
            assert tile['y'] >= 0 and tile['bottom'] <= height + 1, tile
            assert tile['width'] >= 40 and tile['height'] >= 40, tile
        if args.screenshot and width == 320:
            path = Path(args.screenshot)
            await capture(cdp, path.with_name(path.stem + '-preselection-320x568' + path.suffix))
        print(f'PASS preselected Stage {width}x{height}: ten long rig names, accessible cancel, no overflow or device action', flush=True)

    await cdp.command('Emulation.setEmulatedMedia', {'features':[
        {'name':'prefers-reduced-motion', 'value':'reduce'}]})
    await asyncio.sleep(.1)
    reduced = await cdp.evaluate("""(() => {
      const style = getComputedStyle(document.querySelector('.stage'), '::after');
      return {animation:style.animationName, opacity:Number(style.opacity),
        pending:!!document.querySelector('.stage__preselection-bar')};
    })()""")
    assert reduced['animation'] == 'none' and reduced['opacity'] > 0 and reduced['pending'], reduced
    await cdp.command('Emulation.setEmulatedMedia', {'features':[
        {'name':'prefers-reduced-motion', 'value':'no-preference'}]})
    await asyncio.sleep(.1)
    assert await cdp.evaluate("getComputedStyle(document.querySelector('.stage'), '::after').animationName !== 'none'")
    assert not await mutations(cdp)
    print('PASS preselection reduced motion: steady visible indication, restored pulse, zero device actions', flush=True)

    await mouse(cdp, cancel)
    await wait_for(cdp, '!document.querySelector(' + json.dumps(cancel) + ')', 'Cancel returns to live Stage')
    assert not await mutations(cdp)
    assert await cdp.evaluate("document.querySelector('.stage__bank-readout').textContent.trim()") == before['bank']
    assert await cdp.evaluate("document.querySelector('.stage__rig-readout').textContent.trim()") == before['rig']

    # Escape and the dialog's X cancel without selecting a rig or loading a bank.
    for close in ('escape', 'button'):
        await open_picker(cdp)
        if close == 'escape':
            await escape(cdp)
        else:
            await mouse(cdp, '.bank-picker__close')
        await wait_for(cdp, '!document.querySelector(".bank-picker")', 'Close bank preselection picker')
        assert not await mutations(cdp)
    print('PASS mode change, bank preselection, explicit cancel, Escape and X preserve the live rig with zero actions', flush=True)

    await open_picker(cdp)
    await wheel(cdp, 1)
    await mouse(cdp, '.bank-picker__bank[aria-label="Bank 99"]')
    await wait_for(cdp, '!!document.querySelector(' + json.dumps(cancel) + ')', 'Preselected Stage ready for exact rig')
    await cdp.evaluate('window.__bankPeer.hold=true;window.__bankPeer.commands=[];window.__bankPeer.replies=[]')
    rig_name = 'Rig 7 - Vintage Deluxe Reverb Ultra Long Preselected Sound'
    rig_selector = '.stage__switch[aria-label=' + json.dumps('Rig 7: ' + rig_name) + ']'
    await mouse(cdp, rig_selector)
    await wait_for(cdp, '!!window.__bankPeer.pending', 'Exact rig command awaiting ACK')
    sent = await mutations(cdp)
    assert len(sent) == 1 and sent[0]['type'] == 'SWITCH_PATCH' and sent[0]['bank'] == 99 and sent[0]['slot'] == 7, sent
    commands = await cdp.evaluate('window.__bankPeer.commands')
    types = [command['type'] for command in commands if command['type'] in ('GET_DEVICE_INFO','LIST_PATCHES','SWITCH_PATCH')]
    assert types == ['GET_DEVICE_INFO', 'LIST_PATCHES', 'SWITCH_PATCH'], commands
    assert await cdp.evaluate('window.__bankPeer.current') == before['current']
    assert await cdp.evaluate("document.querySelector('.stage__rig-name').textContent.trim()") == before['title']
    await mouse(cdp, rig_selector)
    assert len(await mutations(cdp)) == 1, 'Pending rig selection dispatched twice'
    await cdp.evaluate('window.__bankPeer.finish()')
    await wait_for(cdp, '!document.querySelector(' + json.dumps(cancel) + ')', 'ACK and fresh confirmation leave preselection')
    assert await cdp.evaluate('window.__bankPeer.current') == {'bank':99, 'slot':7}
    replies = await cdp.evaluate('window.__bankPeer.replies')
    assert any(reply.get('type') == 'ACK' and reply.get('id') == sent[0]['id'] for reply in replies), replies
    assert any(reply.get('type') == 'DEVICE_INFO' and reply.get('current') == {'bank':99, 'slot':7} for reply in replies), replies
    assert len(await mutations(cdp)) == 1
    print('PASS exact rig 7 rather than current/fallback slot: fresh preflight, one SWITCH_PATCH, no optimistic load, correlated ACK and position confirmation', flush=True)

    await cdp.command('Page.reload')
    await wait_for(cdp, "typeof window.__stageDoorbell === 'function' && !!document.querySelector('.stage__bank-readout')", 'Reloaded preview')
    await fixture(cdp, 'WAH', title='BANK PICKER PREVIEW')
    await cdp.evaluate(PEER)
    await open_picker(cdp)
    assert await cdp.evaluate('document.querySelector(' + json.dumps(mode) + ').dataset.mode') == 'preselect'
    assert not await mutations(cdp)
    await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':True, 'maxTouchPoints':1})
    try:
        await choose_mode(cdp, 'immediate', use_touch=True)
        await touch(cdp, '.bank-picker__close')
    finally:
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':False})
    await wait_for(cdp, '!document.querySelector(".bank-picker")', 'Touch closes settings without selection')
    assert not await mutations(cdp)
    assert await cdp.evaluate('window.__bankPeer.current') == {'bank':1,'slot':4}
    assert await cdp.evaluate("localStorage.getItem('BOSUN_STAGE_BANK_SELECTION')") in (None, 'immediate')
    print('PASS preselection preference survives reload; touch restores immediate mode without a device action', flush=True)


async def run_live(cdp, args):
    """Default inspection only; optional bank preview never selects a rig."""
    await wait_for(cdp, "document.querySelector('.stage__bank-readout')?.disabled === false", 'Connected live bank control')
    assert await cdp.evaluate("typeof window.__stageDoorbell === 'undefined'"), 'Live mode requires the deployed Stage'
    position_before = await cdp.evaluate("document.querySelector('.stage__bank-readout').textContent.trim()")
    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':1440, 'height':900, 'deviceScaleFactor':1, 'mobile':False,
    })
    await mouse(cdp, '.stage__bank-readout')
    await wait_for(cdp, "document.querySelectorAll('.bank-picker__bank:not(:disabled)').length > 0", 'Live bank inventory')
    wide = await cdp.evaluate(GEOMETRY)
    labels = [button['label'] for button in wide['buttons']]
    assert len(labels) == len(set(labels)) and 1 <= len(labels) <= 99, labels
    for width, height in ((1440, 900), (375, 667)):
        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
        })
        await asyncio.sleep(.08)
        geometry = await cdp.evaluate(GEOMETRY)
        assert geometry['modal'] and geometry['dialog'] == {
            'x':0, 'y':0, 'width':width, 'height':height, 'right':width, 'bottom':height,
        }, geometry
        assert geometry['scrollWidth'] <= geometry['clientWidth'] + 1, geometry
        assert geometry['documentWidth'] <= width + 1, geometry
        verify_mode_geometry(geometry)
        await mouse(cdp, '.bank-picker__mode-trigger')
        await wait_for(cdp, "document.querySelector('.bank-picker__mode-trigger').getAttribute('aria-expanded') === 'true'",
                       'Inspect live expanded modes without choosing a mode')
        verify_mode_geometry(await cdp.evaluate(GEOMETRY), expanded=True)
        if args.screenshot:
            path = Path(args.screenshot)
            await capture(cdp, path.with_name(path.stem + f'-mode-expanded-{width}' + path.suffix))
        await escape(cdp)
        await wait_for(cdp, "document.querySelector('.bank-picker__mode-trigger')?.getAttribute('aria-expanded') === 'false'",
                       'Live Escape closes only the mode menu')
        assert sum(button['current'] == 'true' for button in geometry['buttons']) == 1, geometry
        assert all(button['width'] >= 48 and button['height'] >= 80 for button in geometry['buttons']), geometry
        if geometry['scrollHeight'] > geometry['clientHeight'] + 2:
            bottom = await wheel(cdp, 1)
            assert bottom['scrollTop'] > geometry['scrollTop'], bottom
            assert bottom['header'] == geometry['header'] and bottom['close'] == geometry['close'], bottom
        if width == 1440 and args.screenshot:
            await capture(cdp, args.screenshot)
        print(f'PASS LIVE passive fullscreen {width}x{height}: {len(labels)} real banks, matching expanded mode targets and fixed close', flush=True)
    await escape(cdp)
    await wait_for(cdp, "!document.querySelector('.bank-picker')", 'Live Escape closes picker')
    assert await cdp.evaluate("document.activeElement === document.querySelector('.stage__bank-readout')")
    await mouse(cdp, '.stage__bank-readout')
    await wait_for(cdp, "document.querySelectorAll('.bank-picker__bank:not(:disabled)').length > 0", 'Reopened live inventory')
    await mouse(cdp, '.bank-picker__close')
    await wait_for(cdp, "!document.querySelector('.bank-picker')", 'Live close button')
    assert await cdp.evaluate("document.querySelector('.stage__bank-readout').textContent.trim()") == position_before
    await asyncio.sleep(.15)
    await cdp.evaluate('true')  # Drain the final network events into the trace.
    sent = [frame['message'] for frame in cdp.frames_since(0) if frame['direction'] == 'SEND']
    forbidden = [message for message in sent if message.get('type') in ('SWITCH_PATCH', 'ACTIVATE_SWITCH')]
    assert not forbidden, forbidden
    kinds = {message.get('type') for message in sent}
    assert {'GET_DEVICE_INFO', 'LIST_PATCHES'} <= kinds, sent
    if args.screenshot:
        print('LIVE current-bank screenshot: ' + args.screenshot, flush=True)
    print('PASS LIVE open/Escape/focus/open/close; unchanged bank; zero SWITCH_PATCH or ACTIVATE_SWITCH frames; banks=' + json.dumps(labels), flush=True)
    if args.live_preselect:
        await live_preselection(cdp, args)


async def live_preselection(cdp, args):
    """Preview another bank, never a rig; restore this isolated profile."""
    storage_key = 'BOSUN_STAGE_BANK_SELECTION'
    original = await cdp.evaluate('localStorage.getItem(' + json.dumps(storage_key) + ')')
    before = await cdp.evaluate("""[...document.querySelectorAll(
      '.stage__rig-name,.stage__bank-readout,.stage__rig-readout')].map(el => el.textContent.trim())""")
    cancel = '[aria-label="Cancel bank preselection"]'
    try:
        await mouse(cdp, '.stage__bank-readout')
        await wait_for(cdp, "!!document.querySelector('.bank-picker__bank[aria-current=true]')", 'Current live bank')
        candidate = '.bank-picker__bank:not([aria-current=true]):not(:disabled)'
        if not await cdp.evaluate('!!document.querySelector(' + json.dumps(candidate) + ')'):
            await mouse(cdp, '.bank-picker__close')
            print('SKIP LIVE preselection preview: only the current bank exists', flush=True)
            return
        await choose_mode(cdp, 'preselect')
        positions = [frame['message'].get('current') for frame in cdp.frames_since(0)
                     if frame['direction'] == 'RECV' and frame['message'].get('type') == 'DEVICE_INFO']
        assert positions and isinstance(positions[-1], dict), positions
        playing = positions[-1]
        await mouse(cdp, candidate)
        await wait_for(cdp, '!document.querySelector(".bank-picker") && !!document.querySelector(' + json.dumps(cancel) + ')',
                       'Another bank preview on Stage')
        assert await cdp.evaluate("document.querySelector('.stage__rig-name').textContent.trim()") == before[0]
        status = await cdp.evaluate("document.querySelector('.stage__preselection-bar small').textContent.trim()")
        assert status == 'Playing bank %s \u00b7 rig %s' % (playing['bank'], playing['slot']), status
        updated = [frame['message'].get('current') for frame in cdp.frames_since(0)
                   if frame['direction'] == 'RECV' and frame['message'].get('type') == 'DEVICE_INFO']
        assert updated[-1] == playing, updated
        if args.screenshot:
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':1045, 'height':399, 'deviceScaleFactor':1, 'mobile':False,
            })
            await asyncio.sleep(.1)
            path = Path(args.screenshot)
            await capture(cdp, path.with_name(path.stem + '-preselection' + path.suffix))
        # Deliberately no Stage tile input: the real rig remains untouched.
        await mouse(cdp, cancel)
        await wait_for(cdp, '!document.querySelector(' + json.dumps(cancel) + ')', 'Cancel live preselection')
        assert await cdp.evaluate("""[...document.querySelectorAll(
          '.stage__rig-name,.stage__bank-readout,.stage__rig-readout')].map(el => el.textContent.trim())""") == before
    finally:
        # This browser profile is private to run(); restore the exact value,
        # including absence, even when a UI assertion stops the check.
        await cdp.evaluate("""(() => {
          const {key, value} = PREFERENCE;
          if (value === null) localStorage.removeItem(key);
          else localStorage.setItem(key, value);
        })()""".replace('PREFERENCE', json.dumps({'key':storage_key,'value':original})))
        assert await cdp.evaluate('localStorage.getItem(' + json.dumps(storage_key) + ')') == original
    await asyncio.sleep(.15)
    await cdp.evaluate('true')
    sent = [frame['message'] for frame in cdp.frames_since(0) if frame['direction'] == 'SEND']
    forbidden = [message for message in sent if message.get('type') in ('SWITCH_PATCH', 'ACTIVATE_SWITCH')]
    assert not forbidden, forbidden
    print('PASS LIVE other-bank preselection/cancel, original local preference restored, unchanged rig and zero device actions', flush=True)


def verify_mode_geometry(state, *, expanded=False):
    """BANK header, X and mode row follow the actual main Stage geometry."""
    minimum = 40 if state['coarse'] else 30
    assert state['mode']['width'] >= 48 and state['mode']['height'] >= minimum, state
    for axis in ('x', 'y', 'width', 'height', 'right', 'bottom'):
        assert abs(state['header'][axis] - state['stageHeader'][axis]) <= 1, ('BANK header rectangle differs from main header', axis, state)
        assert abs(state['close'][axis] - state['stageClose'][axis]) <= 1, state
    assert abs(state['mode']['height'] - state['stageClose']['height']) <= 1, state
    assert abs(state['mode']['y'] - state['stageClose']['y']) <= 1, state
    assert state['modeContent']['scrollHeight'] <= state['modeContent']['clientHeight'] + 1, state
    assert state['modeContent']['scrollWidth'] <= state['modeContent']['clientWidth'] + 1, state
    assert state['mode']['x'] >= 0 and state['mode']['right'] <= state['viewport']['width'] + 1, state
    assert state['mode']['bottom'] <= state['header']['bottom'] + 1, state
    if not expanded:
        return
    assert [button['mode'] for button in state['modeOptions']] == ['immediate', 'preselect'], state
    assert sum(button['selected'] == 'true' for button in state['modeOptions']) == 1, state
    assert state['modeList']['bottom'] <= state['viewport']['height'] + 1, state
    for button in state['modeOptions']:
        assert button['width'] >= 48 and button['height'] >= minimum, button
        assert abs(button['height'] - state['mode']['height']) <= 1, state
        assert button['x'] >= 0 and button['right'] <= state['viewport']['width'] + 1, button
        assert button['y'] >= state['mode']['bottom'], button
        if button['bottom'] > state['viewport']['height'] + 1:
            assert state['modeList']['scrollHeight'] > state['modeList']['clientHeight'], state
        assert button['scrollHeight'] <= button['clientHeight'] + 1, button


def verify_geometry(state):
    assert state and state['modal'], state
    viewport, dialog = state['viewport'], state['dialog']
    assert abs(dialog['x']) <= 1 and abs(dialog['y']) <= 1, state
    assert abs(dialog['width'] - viewport['width']) <= 1, state
    assert abs(dialog['height'] - viewport['height']) <= 1, state
    assert state['scrollWidth'] <= state['clientWidth'] + 1, state
    assert state['documentWidth'] <= viewport['width'] + 1, state
    assert state['scrollHeight'] > state['clientHeight'], state
    assert 2 <= state['columns'] <= 8, state
    assert state['close']['bottom'] <= state['header']['bottom'] + 1, state
    verify_mode_geometry(state)
    assert len(state['buttons']) == 99, state
    for button in state['buttons']:
        assert button['width'] >= 48 and button['height'] >= 80, button
        assert button['x'] >= 0 and button['right'] <= viewport['width'] + 1, button
    assert [button['label'] for button in state['buttons'] if button['current'] == 'true'] == ['Bank 1'], state


async def run(args):
    parsed = urlparse(args.page)
    if args.live_preselect and not args.live:
        raise ValueError('--live-preselect requires the explicit --live mode')
    if args.live:
        if parsed.scheme != 'http' or parsed.hostname != os.environ.get("BOSUN_STAGE_HOST", "bosun-hub") or parsed.port != 8080 or parsed.path != '/':
            raise ValueError('--live permits only the configured Pi Stage on HTTP port 8080')
    elif parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/stage-preview.html':
        raise ValueError('Only the local stage-preview.html fixture is allowed without --live')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-bank-picker-cdp-')
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
        if args.live:
            await run_live(cdp, args)
            return
        assert await cdp.evaluate("typeof window.__stageDoorbell === 'function'")
        await fixture(cdp, 'WAH', title='BANK PICKER PREVIEW')
        await cdp.evaluate(PEER)
        await open_picker(cdp)
        # A native modal must prevent programmatic focus from escaping too.
        assert await cdp.evaluate("""(() => {
          document.querySelector('.stage__bank-readout').focus();
          return document.querySelector('.bank-picker').contains(document.activeElement);
        })()""")
        for width, height in VIEWPORTS:
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':width, 'height':height, 'deviceScaleFactor':1, 'mobile':False,
            })
            await asyncio.sleep(.06)
            top = await wheel(cdp, -1)
            verify_geometry(top)
            await mouse(cdp, '.bank-picker__mode-trigger')
            await wait_for(cdp, "document.querySelector('.bank-picker__mode-trigger').getAttribute('aria-expanded') === 'true'",
                           'Mode options open at each viewport')
            verify_mode_geometry(await cdp.evaluate(GEOMETRY), expanded=True)
            if args.screenshot and width == 320:
                path = Path(args.screenshot)
                await capture(cdp, path.with_name(path.stem + '-mode-expanded-320' + path.suffix))
            await escape(cdp)
            await wait_for(cdp, "document.querySelector('.bank-picker__mode-trigger')?.getAttribute('aria-expanded') === 'false'",
                           'Escape closes only the mode menu')
            bottom = await wheel(cdp, 1)
            assert bottom['scrollTop'] > top['scrollTop'], bottom
            assert abs(bottom['scrollTop'] + bottom['clientHeight'] - bottom['scrollHeight']) <= 2, bottom
            assert bottom['close'] == top['close'] and bottom['header'] == top['header'], bottom
            last = bottom['buttons'][-1]
            assert last['y'] >= bottom['body']['y'] and last['bottom'] <= height + 1, bottom
            print(f'PASS fullscreen {width}x{height}: {top["columns"]} columns, 99 banks, wheel scroll, fixed close; expanded modes match {top["mode"]["height"]:g}px control', flush=True)
        assert not await switches(cdp), 'Opening or scrolling sent a bank command'
        await enlarged_mode_checks(cdp, args)
        await keyboard_mode_checks(cdp)

        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':375, 'height':667, 'deviceScaleFactor':1, 'mobile':False,
        })
        await wheel(cdp, -1)
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':True, 'maxTouchPoints':1})
        await wait_for(cdp, """(() => {
          const stage = document.querySelector('.stage__icon-btn[aria-label="Exit Stage"]').getBoundingClientRect();
          const close = document.querySelector('.bank-picker__close').getBoundingClientRect();
          return matchMedia('(pointer: coarse)').matches &&
            Math.abs(stage.width - close.width) <= 1 && Math.abs(stage.height - close.height) <= 1;
        })()""", 'Close matches Stage X after coarse-pointer layout updates')
        before_touch = await cdp.evaluate(GEOMETRY)
        await cdp.command('Input.dispatchTouchEvent', {
            'type':'touchStart', 'touchPoints':[{'x':100, 'y':560}],
        })
        for offset in range(20, 381, 20):
            await cdp.command('Input.dispatchTouchEvent', {
                'type':'touchMove', 'touchPoints':[{'x':100, 'y':560 - offset}],
            })
            await asyncio.sleep(.016)
        await cdp.command('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
        await asyncio.sleep(.15)
        after_touch = await cdp.evaluate(GEOMETRY)
        assert after_touch['scrollTop'] > before_touch['scrollTop'] + 100, after_touch
        assert after_touch['close'] == before_touch['close'], after_touch
        await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':False})
        await escape(cdp)
        await wait_for(cdp, "!document.querySelector('.bank-picker')", 'Escape closes dialog')
        assert await cdp.evaluate("document.activeElement === document.querySelector('.stage__bank-readout')")
        assert not await switches(cdp)
        print('PASS touch scroll, native focus containment, Escape and focus restoration without navigation', flush=True)

        await cdp.command('Emulation.setDeviceMetricsOverride', {
            'width':1045, 'height':399, 'deviceScaleFactor':1, 'mobile':False,
        })
        await open_picker(cdp)
        await mouse(cdp, '.bank-picker__bank[aria-label="Bank 1"]')
        await wait_for(cdp, "!document.querySelector('.bank-picker')", 'Current bank closes dialog')
        assert not await switches(cdp)
        print('PASS choosing current bank closes without SWITCH_PATCH', flush=True)

        for slots, expected_slot in (([2, 4], 4), ([2, 7], 2)):
            await cdp.evaluate("""(() => {
              const peer = window.__bankPeer;
              peer.current = {bank:1,slot:4}; peer.commands = []; peer.hold = true;
              peer.patches = peer.patches.filter(p => p.bank !== 99).concat(
                SLOTS.map(slot => ({bank:99,slot,name:`Bank 99 Rig ${slot}`})));
            })()""".replace('SLOTS', json.dumps(slots)))
            await open_picker(cdp)
            await wheel(cdp, 1)
            await mouse(cdp, '.bank-picker__bank[aria-label="Bank 99"]')
            await wait_for(cdp, "!!window.__bankPeer.pending", 'Correlated bank command')
            sent = await switches(cdp)
            assert len(sent) == 1 and sent[0]['bank'] == 99 and sent[0]['slot'] == expected_slot, sent
            assert await cdp.evaluate("""[...document.querySelectorAll('.bank-picker button')].every(b => b.disabled)""")
            await escape(cdp)
            assert await cdp.evaluate("!!document.querySelector('.bank-picker')"), 'Pending dialog closed before confirmation'
            await mouse(cdp, '.bank-picker__bank[aria-label="Bank 99"]')
            assert len(await switches(cdp)) == 1, 'Pending bank click dispatched twice'
            await cdp.evaluate('window.__bankPeer.finish()')
            await wait_for(cdp, "!document.querySelector('.bank-picker')", 'ACK plus confirmed position closes picker')
            assert await cdp.evaluate("window.__bankPeer.commands.at(-1).type === 'GET_DEVICE_INFO'")
            print(f'PASS bank 99 selects rig {expected_slot}, one command, pending guard, confirmed close', flush=True)

        await cdp.evaluate("window.__bankPeer.current={bank:1,slot:4};window.__bankPeer.commands=[];window.__bankPeer.hold=false;window.__bankPeer.reject=true")
        await open_picker(cdp)
        await wheel(cdp, 1)
        await mouse(cdp, '.bank-picker__bank[aria-label="Bank 99"]')
        await wait_for(cdp, "document.querySelector('.bank-picker [role=alert]')?.textContent.includes('Bank change not confirmed.')", 'Rejected command error')
        await asyncio.sleep(.25)
        assert len(await switches(cdp)) == 1, 'Rejected selection retried automatically'
        assert await cdp.evaluate("!document.querySelector('.bank-picker__close').disabled")
        await mouse(cdp, '.bank-picker__close')
        await wait_for(cdp, "!document.querySelector('.bank-picker')", 'Close rejected selection')
        print('PASS rejected bank selection stays open, reports error and never retries', flush=True)

        await preselection_checks(cdp, args)

        if args.screenshot:
            await cdp.evaluate('window.__bankPeer.reject=false')
            await cdp.command('Emulation.setDeviceMetricsOverride', {
                'width':1920, 'height':1080, 'deviceScaleFactor':1, 'mobile':False,
            })
            await open_picker(cdp)
            await capture(cdp, args.screenshot)
            print('SYNTHETIC 99-bank fixture screenshot: ' + args.screenshot, flush=True)
        print('PASS all bank picker browser checks; no hardware connection or commands', flush=True)
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
    parser.add_argument('--screenshot', help='Optional PNG of synthetic 99-bank fixture')
    parser.add_argument('--live', action='store_true', help='Passive Pi open/inspect/close only; never select a bank')
    parser.add_argument('--live-preselect', action='store_true', help='With --live, preview/cancel another existing bank; never select a rig')
    asyncio.run(run(parser.parse_args()))
