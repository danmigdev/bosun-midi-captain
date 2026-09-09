"""Compare the actual Desktop/Android App Stage with standalone kiosk rendering.

Uses the local Vite Stage preview config only: its normal index mounts App,
while stage-preview.html mounts the shared Stage directly. Both receive the
same isolated firmware fixture. No hardware connection or device actions.
"""

import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

from browser_stage_bank_picker import capture, mouse, wait_for
from browser_stage_layout import fixture
from browser_stage_transition import EDGE, CdpSession, cdp_socket, cleanup_browser_profile


BOOTSTRAP = r"""(() => {
  localStorage.setItem('BOSUN_ONBOARDED', '1');
  localStorage.removeItem('BOSUN_SESSION');
  localStorage.setItem('BOSUN_CONNECTION', JSON.stringify({mode:'usb',host:'',port:'9876'}));
  localStorage.setItem('BOSUN_THEME', THEME);
  localStorage.setItem('BOSUN_UI_SCALE', String(SCALE));
  localStorage.setItem('BOSUN_STAGE_THEME', JSON.stringify(APPEARANCE));
  const peer = window.__platformPeer = {commands:[]};
  const profile = {id:'parity',name:'Parity fixture',kind:'kemper_player',active:true};
  const patches = [1,2].flatMap(bank => [1,2,3,4,5].map(slot => ({bank,slot,name:'CLEAN'})));
  window.__stagePreviewPatches = patches;
  window.__stagePreviewTitle = 'CLEAN';
  const labels = ['-','-','FLANG','-','BOOST','ACOUSTIC','CLEAN','CRUNCH','HEAVY','LEAD'];
  const colors = ['#ef4444','#10b981','#3b82f6','#f59e0b','#a855f7','#fb7185','#06b6d4','#84cc16','#e879f9','#f97316'];
  const patch = {name:'CLEAN',bindings:['1','2','3','4','up','A','B','C','D','down'].map((sw,index) =>
    ({switch:sw,mode:'latched',label:labels[index],actions:{},led:{on:colors[index]}}))};
  peer.push = message => {
    window.__stageInbox = [...(window.__stageInbox || []), JSON.stringify(message)];
    window.__stageDoorbell?.();
  };
  window.__stageInvoke = command => {
    if (command === 'is_connected') return true;
    if (['list_ports','tcp_list_ports','discover_hubs'].includes(command)) return [];
    if (command === 'bundled_firmware_version') return '0.6.5';
    if (command === 'midi_bridge_status') return {active:false,kemper_port:null,pedal_port:null};
    return null;
  };
  window.__stageCommand = message => {
    peer.commands.push(message);
    let reply;
    switch (message.type) {
      case 'GET_DEVICE_INFO': reply = {type:'DEVICE_INFO', fw:'0.6.5-native',
        device:'midi_captain_10',stage_input:true,profile:'parity',current:{bank:1,slot:1}}; break;
      case 'LIST_PROFILES': reply = {type:'PROFILE_LIST',profiles:[profile],active:'parity'}; break;
      case 'GET_MANIFEST': reply = {type:'MANIFEST',core_messages:[],plugins:[]}; break;
      case 'LIST_PATCHES': reply = {type:'PATCH_LIST',patches}; break;
      case 'GET_GLOBAL': reply = {type:'GLOBAL',device:{}}; break;
      case 'GET_DIRTY': reply = {type:'DIRTY',patches:[]}; break;
      case 'GET_MIDI_LEARN': reply = {type:'MIDI_LEARN',table:{pc_to_patch:[]}}; break;
      case 'GET_PATCH': reply = {type:'PATCH',active_profile:'parity',bank:message.bank,slot:message.slot,
        patch}; break;
      case 'GET_CONTEXT': reply = {type:'CONTEXT',context:{bank:1,slot:1,expression_mode:'WAH',kemper_rig_name:'CLEAN'}}; break;
      case 'PING': reply = {type:'PONG'}; break;
      default: reply = {type:'ACK'};
    }
    setTimeout(() => peer.push({...reply,id:message.id}), 5);
  };
})()"""

PROBE = r"""(() => {
  const selectors = SELECTORS;
  const properties = ['backgroundColor','backgroundImage','borderTopColor','borderTopWidth',
    'borderRadius','color','opacity','fontFamily','fontSize','fontWeight','fontFeatureSettings',
    'lineHeight','letterSpacing','paddingTop','paddingRight','paddingBottom','paddingLeft',
    'colorScheme','textRendering','borderTopLeftRadius','borderTopRightRadius',
    'borderBottomLeftRadius','borderBottomRightRadius','offsetPath','offsetDistance',
    'animationDuration','pointerEvents'];
  return Object.fromEntries(selectors.map(selector => [selector,
    [...document.querySelectorAll(selector)].map(el => {
      const box = el.getBoundingClientRect(), css = getComputedStyle(el);
      return {box:{x:box.x,y:box.y,width:box.width,height:box.height},
        css:Object.fromEntries(properties.map(key => [key,css[key]]))};
    })]));
})()"""

MAIN_SELECTORS = ['.stage','.stage__header','.stage__rig-name','.stage__bank-readout',
    '.stage__rig-readout','.stage__expression','.stage__icon-btn','.stage__bank-btn',
    '.stage__switch','.stage__switch-label','.stage__switch-id',
    '.stage__expression-label','.stage__border-pulse']
BANK_SELECTORS = ['.bank-picker','.bank-picker__header','.bank-picker__close',
    '.bank-picker__mode-trigger','.bank-picker__bank']
SETTINGS_SELECTORS = ['.theme-panel','.theme-panel__sheet','.theme-panel__header',
    '.theme-panel__row','.theme-panel__close','.stage-select__trigger',
    '.theme-panel__reset','.theme-panel__scale','.swatch']


async def snapshot(cdp, selectors):
    return await cdp.evaluate(PROBE.replace('SELECTORS', json.dumps(selectors)))


def compare(actual, expected, description):
    differences = []
    for selector, wanted in expected.items():
        got = actual[selector]
        if len(got) != len(wanted):
            differences.append((selector,'count',len(wanted),len(got)))
            continue
        for index, (item, baseline) in enumerate(zip(got, wanted)):
            for axis, value in baseline['box'].items():
                if abs(item['box'][axis] - value) > 1:
                    differences.append((selector,index,axis,value,item['box'][axis]))
            for key, value in baseline['css'].items():
                if item['css'][key] != value:
                    differences.append((selector,index,key,value,item['css'][key]))
    assert not differences, (description,differences)


async def inspect(cdp, args, *, host, theme, width, height, scale, appearance, coarse):
    await cdp.command('Emulation.setDeviceMetricsOverride', {
        'width':width,'height':height,'deviceScaleFactor':1,'mobile':False,
    })
    await cdp.command('Emulation.setTouchEmulationEnabled', {'enabled':coarse,'maxTouchPoints':1})
    ua = ('Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36'
          if coarse else 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
    await cdp.command('Emulation.setUserAgentOverride', {'userAgent':ua})
    init = BOOTSTRAP.replace("'BOSUN_THEME', THEME", "'BOSUN_THEME', " + json.dumps(theme))
    init = init.replace('String(SCALE)', 'String(' + str(scale) + ')')
    init = init.replace('JSON.stringify(APPEARANCE)', 'JSON.stringify(' + json.dumps(appearance) + ')')
    installed = await cdp.command('Page.addScriptToEvaluateOnNewDocument', {'source':init})
    try:
        page = args.base + ('/' if host == 'app' else '/stage-preview.html')
        await cdp.command('Page.navigate', {'url':page})
        if host == 'app':
            await wait_for(cdp, "[...document.querySelectorAll('.navitem .lbl')].some(el => el.textContent === 'Stage')", 'Actual App navigation')
            if coarse:
                await wait_for(cdp, "history.state?.__bosun_guard === true", 'Android startup lifecycle ready')
            await asyncio.sleep(.15)
            await cdp.evaluate("[...document.querySelectorAll('.navitem')].find(el => el.querySelector('.lbl')?.textContent === 'Stage').click()")
        await wait_for(cdp, "!!document.querySelector('.stage__header')", 'Stage mounted')
        await cdp.evaluate('document.documentElement.style.fontSize = ' + json.dumps(str(scale * 100) + '%'))
        await asyncio.sleep(.25)
        await fixture(cdp, 'WAH')
        # Compare the travelling highlight at the same point in both hosts.
        # Its timing/path remain the production CSS; only the clock is paused.
        await cdp.evaluate("document.querySelector('.stage__border-pulse').getAnimations().forEach(a => { a.pause(); a.currentTime = 1200; })")
        # Disable one card only in the isolated DOM to inspect the host's
        # disabled-button baseline without simulating any hardware action.
        await cdp.evaluate("document.querySelector('.stage__switch').disabled = true")
        await cdp.command('Input.dispatchMouseEvent', {'type':'mouseMoved','x':0,'y':0})
        result = {'main':await snapshot(cdp, MAIN_SELECTORS)}
        stage = result['main']['.stage']
        assert len(stage) == 1, ('Only actual Stage carries .stage',host,stage)
        assert stage[0]['box'] == {'x':0,'y':0,'width':width,'height':height}, (host,'Stage fills viewport',stage)
        beam = result['main']['.stage__border-pulse']
        assert len(beam) == 1 and beam[0]['css']['animationDuration'] == '24s, 2.4s', (host,beam)
        assert await cdp.evaluate("getComputedStyle(document.querySelector('.stage__border-pulse')).width") == '288px'
        assert not await cdp.evaluate("document.querySelector('.stage__expression svg') !== null")
        if not coarse:
            target = await cdp.evaluate("(() => {const r=document.querySelector('.stage__switch--active').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
            await cdp.command('Input.dispatchMouseEvent', {'type':'mouseMoved',**target})
            await asyncio.sleep(.18)
            result['hover'] = await snapshot(cdp, ['.stage__switch--active'])
        if args.screenshots and width == 1920 and scale == 1:
            await capture(cdp, Path(args.screenshots) / (host + '-' + theme + '-1920x440.png'))
        await mouse(cdp, '.stage__bank-readout')
        await wait_for(cdp, "document.querySelectorAll('.bank-picker__bank').length === 2", 'Both bank tiles')
        await cdp.command('Input.dispatchMouseEvent', {'type':'mouseMoved','x':0,'y':0})
        result['bank'] = await snapshot(cdp, BANK_SELECTORS)
        await mouse(cdp, '.bank-picker__close')
        await mouse(cdp, '.stage__icon-btn[aria-label="Stage appearance"]')
        await wait_for(cdp, "!!document.querySelector('.theme-panel__sheet')", 'Appearance panel')
        await cdp.command('Input.dispatchMouseEvent', {'type':'mouseMoved','x':0,'y':0})
        result['settings'] = await snapshot(cdp, SETTINGS_SELECTORS)
        assert len(result['settings']['.stage-select__trigger']) == 8
        assert await cdp.evaluate("[...document.querySelectorAll('.theme-panel__sections input[type=range]')].every(el => el.min === '0.1' && el.max === '5')")
        assert await cdp.evaluate("document.querySelectorAll('.theme-panel__corners input').length") == 1
        assert float(await cdp.evaluate("document.querySelector('.theme-panel__corners input').value")) == appearance.get('corners', .75)
        await mouse(cdp, '.theme-panel__close')
        assert not await cdp.evaluate("window.__platformPeer.commands.some(m => ['ACTIVATE_SWITCH','SWITCH_PATCH','PUT_PATCH','SAVE_PATCH'].includes(m.type))")
        saved = await cdp.evaluate("JSON.parse(localStorage.getItem('BOSUN_STAGE_THEME'))")
        assert saved == appearance, ('Preserved appearance',saved,appearance)
        if host == 'app':
            await mouse(cdp, '.stage__icon-btn[aria-label="Exit Stage"]')
            await wait_for(cdp, "!document.querySelector('.stage') && !!document.querySelector('.topbar')", 'X exits actual App Stage')
            assert await cdp.evaluate("document.documentElement.dataset.theme") == theme
            assert not await cdp.evaluate("document.querySelector('.app').classList.contains('app--stage')")
            if coarse:
                await cdp.evaluate("[...document.querySelectorAll('.navitem')].find(el => el.querySelector('.lbl')?.textContent === 'Stage').click()")
                await wait_for(cdp, "!!document.querySelector('.stage__header')", 'Stage reopened for Android Back')
                await cdp.evaluate("window.dispatchEvent(new PopStateEvent('popstate', {state:null}))")
                await wait_for(cdp, "!document.querySelector('.stage') && document.querySelector('.navitem.active .lbl')?.textContent === 'Patches'", 'Android Back leaves Stage for Patches')
        return result
    except BaseException:
        print(await cdp.evaluate("({body:document.body.innerText,commands:window.__platformPeer?.commands})"), flush=True)
        raise
    finally:
        await cdp.command('Page.removeScriptToEvaluateOnNewDocument', {'identifier':installed['identifier']})


async def run(args):
    parsed = urlparse(args.base)
    if parsed.hostname not in ('127.0.0.1','localhost') or parsed.scheme != 'http':
        raise ValueError('Only an isolated local Stage preview server is allowed')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    profile = tempfile.TemporaryDirectory(prefix='bosun-stage-parity-')
    browser = subprocess.Popen([args.edge,'--headless=new','--disable-gpu','--no-first-run',
        '--remote-debugging-port=' + str(port),'--user-data-dir=' + profile.name,'about:blank'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    cdp, error = None, None
    try:
        cdp = CdpSession(await cdp_socket(port,args.base))
        await cdp.command('Page.enable')
        await cdp.command('Runtime.enable')
        empty = {'version':2,'sections':{}}
        custom = {'version':2,'corners':0,'fontFamily':'Georgia, "Times New Roman", Times, serif',
                  'sections':{'rigName':{'scale':.75,'fontFamily':'monospace'},'switchId':{'scale':.75},
                              'expression':{'scale':1.5,'color':'#ffaa00','fontFamily':'monospace'}}}
        for width,height,scale,appearance,coarse in (
            (1920,440,1,empty,False), (1100,720,1,empty,False),
            (1100,720,1.5,custom,False), (800,360,1,empty,True),
            (375,667,1,empty,True), (375,667,1.5,custom,True),
        ):
            kwargs = dict(width=width,height=height,scale=scale,appearance=appearance,coarse=coarse)
            baseline = await inspect(cdp,args,host='kiosk',theme='dark',**kwargs)
            for theme in ('dark','light'):
                actual = await inspect(cdp,args,host='app',theme=theme,**kwargs)
                for part in baseline:
                    compare(actual[part],baseline[part],(part,theme,kwargs))
                print(f'PASS App/kiosk parity {width}x{height}, root {scale:g}, {theme}, Android={coarse}: full viewport, corners, 288px/24s beam, text-only VOL/WAH, 8 appearance selectors, bank dialog and X/Android Back exit',flush=True)
        print('PASS all platform parity checks; isolated fixture, saved preferences preserved, zero device actions',flush=True)
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
        await cleanup_browser_profile(profile,primary_error=error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='http://127.0.0.1:4732')
    parser.add_argument('--edge',default=EDGE)
    parser.add_argument('--screenshots')
    asyncio.run(run(parser.parse_args()))
