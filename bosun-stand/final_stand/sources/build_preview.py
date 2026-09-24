"""Build the final stand preview with independent arm and display controls."""
from pathlib import Path
import argparse
import base64
import json
import math
import subprocess
import tempfile
import time
import urllib.request
import websocket

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'final_stand'


def html_preview(scene):
    core = (ROOT / 'renderer_webgl.js').read_text(encoding='utf-8')
    clearance_core = (ROOT / 'motion_clearance.js').read_text(encoding='utf-8')
    text = (ROOT / 'preview_template.html').read_text(encoding='utf-8')
    text = text.replace('__RENDER_CORE__', core).replace('__CLEARANCE_CORE__', clearance_core).replace(
        '__SCENE__', json.dumps(scene, separators=(',', ':'), ensure_ascii=False))
    assert '__SCENE__' not in text and '__RENDER_CORE__' not in text and '__CLEARANCE_CORE__' not in text
    OUT.mkdir(exist_ok=True)
    (OUT / 'preview_3d.html').write_text(text, encoding='utf-8')


def verify_browser():
    previews = OUT / 'images'
    previews.mkdir(exist_ok=True)
    errors, checks = [], []
    with tempfile.TemporaryDirectory(prefix='bosun-final-preview-') as profile:
        browser = subprocess.Popen([
            'C:/Program Files/Google/Chrome/Application/chrome.exe', '--headless',
            '--no-first-run', '--disable-background-networking', '--enable-unsafe-swiftshader',
            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9351',
            f'--user-data-dir={profile}', '--window-size=1700,1180',
            (OUT / 'preview_3d.html').as_uri()], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        ws = None
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:9351/json', timeout=1) as response:
                        target = next(t for t in json.load(response)
                                      if t['type'] == 'page' and 'final_stand' in t['url'])
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError('Cannot connect to Chrome for preview verification')
            ws = websocket.create_connection(target['webSocketDebuggerUrl'],
                                             suppress_origin=True, timeout=30)
            sequence = 0

            def call(method, params=None):
                nonlocal sequence
                sequence += 1
                ws.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
                while True:
                    response = json.loads(ws.recv())
                    if response.get('method') == 'Runtime.exceptionThrown':
                        errors.append(response['params'])
                    if response.get('id') == sequence:
                        assert 'error' not in response, response
                        return response.get('result', {})

            def evaluate(expression):
                result = call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
                assert 'exceptionDetails' not in result, result
                return result['result'].get('value')

            def shot(name):
                time.sleep(.15)
                (previews / name).write_bytes(base64.b64decode(
                    call('Page.captureScreenshot', {'format': 'png'})['data']))

            def camera_state():
                state = evaluate("(()=>{const camera=bosunPreview.getCameraState();return {...camera,matrix:lookAt(camera.eye,camera.target,camera.up)};})()")
                assert all(math.isfinite(value) for value in state['matrix']), state
                axes = [[state['matrix'][offset + 4 * index] for index in range(3)]
                        for offset in range(3)]
                for axis in axes:
                    assert abs(sum(value * value for value in axis) - 1) < 1e-9, state
                for first, second in ((0, 1), (0, 2), (1, 2)):
                    assert abs(sum(a * b for a, b in zip(axes[first], axes[second]))) < 1e-9, state
                return state

            def same_camera(first, second, tolerance=1e-7):
                assert abs(first['span'] - second['span']) < 1e-9, (first, second)
                for field in ('eye', 'target', 'up', 'matrix'):
                    assert all(abs(a - b) < tolerance for a, b in zip(first[field], second[field])), (field, first, second)

            def drag_view(dx, dy, steps=24):
                bounds = evaluate("(()=>{const bounds=canvas.getBoundingClientRect();return {x:bounds.x,y:bounds.y,width:bounds.width,height:bounds.height};})()")
                assert abs(dx) < bounds['width'] - 20 and abs(dy) < bounds['height'] - 20, bounds
                start_x = bounds['x'] + (bounds['width'] - dx) / 2
                start_y = bounds['y'] + (bounds['height'] - dy) / 2
                call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': start_x, 'y': start_y})
                call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': start_x, 'y': start_y,
                                                  'button': 'left', 'buttons': 1, 'clickCount': 1})
                samples = []
                for index in range(1, steps + 1):
                    call('Input.dispatchMouseEvent', {'type': 'mouseMoved',
                                                      'x': start_x + dx * index / steps,
                                                      'y': start_y + dy * index / steps,
                                                      'button': 'left', 'buttons': 1})
                    samples.append(camera_state())
                call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': start_x + dx,
                                                  'y': start_y + dy, 'button': 'left', 'buttons': 0,
                                                  'clickCount': 1})
                return samples

            def verify_camera_rotation():
                evaluate("document.querySelector('[data-view=iso]').click()")
                initial_pose = evaluate('bosunPreview.getState()')
                initial_camera = camera_state()
                revolution_pixels = 2 * math.pi / .008
                for axis in ('horizontal', 'vertical'):
                    before = camera_state()
                    samples = drag_view(revolution_pixels if axis == 'horizontal' else 0,
                                        revolution_pixels if axis == 'vertical' else 0)
                    same_camera(before, camera_state(), tolerance=.002)
                    assert any(sum((a - b) ** 2 for a, b in zip(sample['eye'], before['eye'])) > 1
                               for sample in samples), samples
                    if axis == 'vertical':
                        assert any(sample['eye'][2] < sample['target'][2] for sample in samples), samples
                    checks.append({'camera_pointer_full_revolution': axis, 'periodic': True,
                                   'finite_orthonormal_frames': True,
                                   'underside_accessible': axis == 'vertical'})
                before = camera_state()
                for _ in range(4):
                    drag_view(revolution_pixels / 4, -revolution_pixels / 4, steps=8)
                same_camera(before, camera_state(), tolerance=.002)
                checks.append({'camera_repeated_pointer_drags': 4, 'both_axes_periodic': True})
                for pole in (-math.pi / 2, math.pi / 2, -math.pi, math.pi):
                    frames = []
                    for offset in (-1e-6, 0, 1e-6):
                        target = pole + offset
                        evaluate(f"bosunPreview.rotateView(0,({target}-bosunPreview.getCameraState().elevation)/.008)")
                        frames.append(camera_state())
                    for first, second in zip(frames, frames[1:]):
                        for axis_offset in range(3):
                            alignment = sum(first['matrix'][axis_offset + 4 * index]
                                            * second['matrix'][axis_offset + 4 * index] for index in range(3))
                            assert alignment > .999999, (pole, first, second)
                checks.append({'camera_vertical_poles_and_angle_wrap': True,
                               'exact_poles_finite_and_orthonormal': True, 'orientation_continuous': True})
                assert evaluate('bosunPreview.getState()') == initial_pose
                checks.append({'camera_rotation_preserves_arm_and_display_pose': True})
                for name in ('iso', 'rear', 'side', 'top'):
                    evaluate(f"document.querySelector('[data-view={name}]').click()")
                    preset_camera = camera_state()
                    drag_view(45, -45, steps=3)
                    evaluate(f"document.querySelector('[data-view={name}]').click()")
                    same_camera(preset_camera, camera_state())
                evaluate("document.querySelector('[data-view=iso]').click()")
                same_camera(initial_camera, camera_state())
                checks.append({'camera_view_buttons_restore_orientation': ['iso', 'rear', 'side', 'top']})

            def assert_pose(name):
                actual = evaluate("({state:bosunPreview.getState(),expected:poses[mode]})")
                assert actual['state']['mode'] == name, actual
                assert not actual['state']['folding'], actual
                assert [actual['state']['armAngle'], actual['state']['angle']] == actual['expected'], actual
                assert actual['state']['release'] == 0, actual
                if name == 'front':
                    gap = actual['state']['frontGap']
                    if gap:
                        assert 0 < gap['front_clearance_y_mm'] <= evaluate('p.front_gap_max_mm') + .0001, gap
                        shown = evaluate("({hidden:document.getElementById('frontGapReadout').hidden,text:document.getElementById('frontGapValue').textContent})")
                        assert not shown['hidden'], shown
                        assert shown['text'] == f"{gap['front_clearance_y_mm']:.2f} mm", shown
                return actual['state']

            def wait_pose(name, horizontal_only=False):
                samples = []
                for _ in range(100):
                    state = evaluate('bosunPreview.getState()')
                    samples.append([state['armAngle'], state['angle'], state['release']])
                    if horizontal_only:
                        assert abs(state['angle'] - evaluate('horizontal.display_deg')) < 1e-8, state
                    if not state['folding']:
                        checks.append({'pose': name, 'state': assert_pose(name),
                                       'horizontal_only': horizontal_only, 'motion_samples': samples})
                        return
                    time.sleep(.08)
                raise AssertionError(('Animation did not finish', name))

            call('Runtime.enable')
            time.sleep(.7)
            assert evaluate('!!gl && gl.getError()===gl.NO_ERROR')
            assert evaluate("document.documentElement.lang==='en'")
            assert assert_pose('front')['printed'] == 13
            assert evaluate("['armPosition','displayRotation'].every(id=>document.getElementById(id)&&!document.getElementById(id).disabled)")
            assert evaluate("items.filter(i=>i.group==='fixed'||i.group==='reference_fixed').every(i=>JSON.stringify(modelMatrix(i))===JSON.stringify(identity()))")
            checks.append({'fixed_parts_use_CAD_coordinates': True})
            background = evaluate("(()=>{draw();const pixel=new Uint8Array(4);gl.readPixels(canvas.width-2,canvas.height-2,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixel);return {pixel:[...pixel],clear:[...gl.getParameter(gl.COLOR_CLEAR_VALUE)],viewport:getComputedStyle(document.getElementById('viewport')).backgroundColor,canvas:getComputedStyle(canvas).backgroundColor,hint:getComputedStyle(document.querySelector('.hint')).color};})()")
            assert background['pixel'] == [230, 232, 235, 255], background
            assert background['viewport'] == background['canvas'] == 'rgb(230, 232, 235)', background
            assert background['hint'] == 'rgb(68, 80, 92)', background
            checks.append({'light_grey_background': background})
            verify_camera_rotation()
            shot('front.png')
            evaluate("document.querySelector('[data-view=side]').click()")
            shot('front_side.png')
            assert evaluate("(()=>{const names=Object.keys(poses);return names.every(a=>names.every(b=>{const path=pathBetween(a,b);return JSON.stringify(path[0])===JSON.stringify(poses[a])&&JSON.stringify(path[path.length-1])===JSON.stringify(poses[b]);}));})()")
            assert evaluate("pathBetween('front','rear').some(point=>JSON.stringify(point)===JSON.stringify(poses.rear_flat))")
            checks.append({'all_motion_path_endpoints': True})
            controls = evaluate("Object.fromEntries(['armPosition','displayRotation'].map(id=>{const input=document.getElementById(id);return [id,{min:Number(input.min),max:Number(input.max),step:input.step}]}))")
            assert controls == {'armPosition': {'min': -180, 'max': 180, 'step': 'any'},
                                'displayRotation': {'min': -180, 'max': 180, 'step': 'any'}}, controls
            checks.append({'independent_control_ranges': controls})

            def move_control(control_id, value):
                evaluate(f"document.getElementById('{control_id}').value={value};document.getElementById('{control_id}').dispatchEvent(new Event('input'))")
                moving = evaluate('bosunPreview.getState()')
                assert moving['manualAdjusting'] and moving['release'] > 0, moving
                evaluate(f"document.getElementById('{control_id}').dispatchEvent(new Event('change'))")
                settled = evaluate('bosunPreview.getState()')
                assert not settled['manualAdjusting'] and settled['release'] > 0 and not settled['locked'], settled
                assert settled['mode'] == 'custom', settled
                return settled

            for preset in ('front', 'rear_flat', 'rear', 'transport'):
                evaluate(f"setPoseImmediate('{preset}')")
                assert evaluate("['armPosition','displayRotation'].every(id=>!document.getElementById(id).disabled)")
                before = evaluate('bosunPreview.getState()')
                arm_state = move_control('armPosition', -12.345)
                assert arm_state['angle'] == before['angle'] and abs(arm_state['armAngle'] + 12.345) < 1e-7, arm_state
                pivot_before = evaluate("(()=>{const matrix=modelMatrix(items.find(item=>item.name==='Display_nominal_envelope'));return [matrix[4]*p.pivot_y+matrix[8]*p.pivot_z+matrix[12],matrix[5]*p.pivot_y+matrix[9]*p.pivot_z+matrix[13],matrix[6]*p.pivot_y+matrix[10]*p.pivot_z+matrix[14]];})()")
                display_state = move_control('displayRotation', -37.625)
                assert display_state['armAngle'] == arm_state['armAngle'] and abs(display_state['angle'] + 37.625) < 1e-7, display_state
                pivot_after = evaluate("(()=>{const matrix=modelMatrix(items.find(item=>item.name==='Display_nominal_envelope'));return [matrix[4]*p.pivot_y+matrix[8]*p.pivot_z+matrix[12],matrix[5]*p.pivot_y+matrix[9]*p.pivot_z+matrix[13],matrix[6]*p.pivot_y+matrix[10]*p.pivot_z+matrix[14]];})()")
                assert all(abs(a - b) < 1e-7 for a, b in zip(pivot_before, pivot_after)), (pivot_before, pivot_after)
                assert display_state['pivot'] == arm_state['pivot'], display_state
                checks.append({'independent_controls_from_preset': preset, 'arm_state': arm_state,
                               'display_state': display_state, 'display_pivot_unchanged': True})
            shifts = evaluate("[modelMatrix(items.find(item=>item.name==='03_arm_right'))[12],modelMatrix(items.find(item=>item.name==='03_arm_left'))[12]]")
            assert shifts == [display_state['release'], -display_state['release']], shifts
            checks.append({'joint_release_directions': shifts})

            for control_id, field, other_field, targets in (
                    ('armPosition', 'armAngle', 'angle', (-180, 180, 12.375)),
                    ('displayRotation', 'angle', 'armAngle', (-180, 180, 43.875))):
                for target in targets:
                    before = evaluate('bosunPreview.getState()')
                    state = move_control(control_id, target)
                    assert abs(state[field] - target) < 1e-7 and state[other_field] == before[other_field], state
                    checks.append({'free_control': control_id, 'target': target, 'state': state})
            for key, shift, expected in (('Home', False, -180), ('ArrowRight', False, -179),
                                         ('ArrowRight', True, -178.9), ('End', False, 180)):
                before = evaluate('bosunPreview.getState()')
                evaluate(f"document.getElementById('armPosition').dispatchEvent(new KeyboardEvent('keydown',{{key:'{key}',shiftKey:{str(shift).lower()},cancelable:true}}))")
                keyed = evaluate('bosunPreview.getState()')
                assert abs(keyed['armAngle'] - expected) < 1e-7 and keyed['angle'] == before['angle'], (key, keyed)
                assert keyed['release'] > 0 and not keyed['locked'], keyed
                checks.append({'free_arm_key': key, 'shift': shift, 'state': keyed})

            evaluate('dragArm(20.125);dragDisplay(45.875);commitManual()')
            before_snap = evaluate('bosunPreview.getState()')
            assert before_snap['release'] > 0 and not before_snap['teethAligned'], before_snap
            evaluate("document.getElementById('snapTeeth').click()")
            snapped = evaluate('bosunPreview.getState()')
            step = evaluate('toothStep')
            assert snapped['release'] == 0 and snapped['locked'] and snapped['teethAligned'], snapped
            assert abs(snapped['armAngle'] / step - round(snapped['armAngle'] / step)) < 1e-7, snapped
            assert abs((snapped['angle'] - snapped['armAngle']) / step - round((snapped['angle'] - snapped['armAngle']) / step)) < 1e-7, snapped
            checks.append({'explicit_snap_to_teeth': snapped})

            evaluate('dragArm(-90);dragDisplay(0);commitManual()')
            floor_state = evaluate('bosunPreview.getState()')
            assert floor_state['clearance']['status'] == 'possible_contact', floor_state
            assert any('floor' in contact['label'].lower() for contact in floor_state['clearance']['contacts']), floor_state
            evaluate('dragArm(90);dragDisplay(90);commitManual()')
            cable_state = evaluate('bosunPreview.getState()')
            assert cable_state['clearance']['status'] == 'possible_contact', cable_state
            assert any('cable' in contact['label'].lower() for contact in cable_state['clearance']['contacts']), cable_state
            evaluate('dragArm(-90);dragDisplay(-90);commitManual()')
            body_state = evaluate('bosunPreview.getState()')
            assert body_state['clearance']['status'] == 'possible_contact', body_state
            assert any('captain' in contact['label'].lower() or 'switch' in contact['label'].lower() for contact in body_state['clearance']['contacts']), body_state
            evaluate('dragArm(0);dragDisplay(0);commitManual()')
            clear_state = evaluate('bosunPreview.getState()')
            assert clear_state['clearance']['status'] == 'no_contact_detected', clear_state
            evaluate('dragArm(20);dragDisplay(45);commitManual()')
            diagonal_state = evaluate('bosunPreview.getState()')
            assert diagonal_state['clearance']['status'] == 'no_contact_detected', diagonal_state
            checks.append({'free_angle_clearance': {'floor_contact': floor_state['clearance'],
                                                   'cable_contact': cable_state['clearance'],
                                                   'body_contact': body_state['clearance'],
                                                   'clear_pose': clear_state['clearance'],
                                                   'clear_diagonal': diagonal_state['clearance']}})
            evaluate("document.getElementById('poseFront').click()")
            selected = assert_pose('front')
            assert not selected['folding'], selected
            assert evaluate("document.getElementById('adjustmentStatus').textContent.includes('Selected preset')")
            assert selected['clearance']['status'] == 'no_contact_detected', selected
            checks.append({'custom_to_preset_without_unchecked_animation': selected})
            evaluate("document.getElementById('horizontalToggle').click()")
            wait_pose('rear_flat', horizontal_only=True)
            shot('rear_flat_side.png')
            evaluate("document.querySelector('[data-view=iso]').click()")
            shot('rear_flat.png')
            evaluate("document.getElementById('horizontalToggle').click()")
            wait_pose('front', horizontal_only=True)
            for source in ('front', 'rear_flat', 'rear', 'transport'):
                for destination in ('front', 'rear_flat', 'rear', 'transport'):
                    if source == destination:
                        continue
                    evaluate(f"setPoseImmediate('{source}');document.querySelector('[data-pose={destination}]').click()")
                    wait_pose(destination, horizontal_only={source, destination} == {'front', 'rear_flat'})
                    assert evaluate("['armPosition','displayRotation'].every(id=>!document.getElementById(id).disabled)")
            evaluate("document.getElementById('poseTransport').click()")
            wait_pose('transport')
            evaluate("document.querySelector('[data-view=side]').click()")
            shot('transport_side.png')
            evaluate("document.getElementById('poseRear').click()")
            wait_pose('rear')
            evaluate("document.querySelector('[data-view=iso]').click()")
            shot('rear_tilted.png')
            evaluate("document.querySelector('[data-view=rear]').click();document.getElementById('refs').click()")
            shot('structure.png')
            evaluate("document.getElementById('explode').click()")
            assert abs(evaluate("modelMatrix(items.find(i=>i.name==='04_display_crossbar'))[12]")) < 1e-7
            evaluate("document.getElementById('explode').click();document.getElementById('refs').click()")
            evaluate("setPoseImmediate('front');document.querySelector('[data-view=iso]').click()")
            call('Emulation.setDeviceMetricsOverride',
                 {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
            time.sleep(.2)
            assert evaluate('document.documentElement.scrollWidth<=390')
            assert evaluate('canvas.clientHeight>300')
            assert evaluate("!document.getElementById('frontGapReadout').hidden || !frontGap()")
            call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 1})
            before_touch = camera_state()
            before_touch_pose = evaluate('bosunPreview.getState()')
            call('Input.dispatchTouchEvent', {'type': 'touchStart',
                                             'touchPoints': [{'x': 195, 'y': 380, 'id': 1}]})
            for index in range(1, 9):
                call('Input.dispatchTouchEvent', {'type': 'touchMove',
                                                 'touchPoints': [{'x': 195, 'y': 380 - 220 * index / 8, 'id': 1}]})
            call('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
            after_touch = camera_state()
            assert abs(after_touch['elevation'] - before_touch['elevation'] + 220 * .008) < 1e-7, after_touch
            assert after_touch['eye'][2] < after_touch['target'][2], after_touch
            assert evaluate('bosunPreview.getState()') == before_touch_pose
            checks.append({'camera_touch_drag': True, 'underside_accessible': True,
                           'arm_and_display_pose_unchanged': True})
            evaluate("document.querySelector('[data-view=iso]').click()")
            shot('mobile.png')
            assert evaluate('gl.getError()===gl.NO_ERROR')
            assert not errors, errors
            result = {'browser': 'Chrome headless / WebGL', 'language': 'en', 'checks': checks,
                      'pose_cycle': True,
                      'independent_arm_and_display_controls': True, 'free_angles_retained_on_release': True,
                      'explicit_snap_to_teeth': True, 'free_angle_clearance': True,
                      'custom_to_preset_without_unchecked_animation': True, 'display_level_during_orbit': True,
                      'light_grey_background_pixel': True, 'default_pose': 'front',
                      'camera_full_rotation': True, 'camera_touch_rotation': True,
                      'CAD_motion_paths': True, 'mobile_layout': True,
                      'monoblock_exploded_centered': True, 'runtime_errors': errors}
            (OUT / 'browser_report.json').write_text(
                json.dumps(result, indent=2), encoding='utf-8')
            print('Preview verified: independent arm and display controls, free angles, explicit tooth snapping, '
                  'four presets, optional level movement, clearance checks, full camera rotation, '
                  'grey background, mobile layout, WebGL.', flush=True)
        finally:
            if ws:
                try:
                    call('Browser.close')
                except Exception:
                    pass
                ws.close()
            try:
                browser.wait(timeout=8)
            except subprocess.TimeoutExpired:
                browser.terminate()
                browser.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify-browser', action='store_true')
    args = parser.parse_args()
    scene = json.loads((OUT / 'scene.json').read_text(encoding='utf-8'))
    assert set(scene['poses']) == {'rear', 'rear_flat', 'front', 'transport'}
    html_preview(scene)
    print('Final stand HTML preview generated.', flush=True)
    if args.verify_browser:
        verify_browser()


if __name__ == '__main__':
    main()
