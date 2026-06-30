import neopixel

from .board import LED_INDEX_PER_SWITCH, NEOPIXEL_COUNT, NEOPIXEL_PIN


def parse_hex(color):
    """Parse '#rrggbb' into an (r, g, b) tuple. Anything malformed returns (0,0,0)."""
    if not isinstance(color, str) or len(color) != 7 or color[0] != "#":
        return (0, 0, 0)
    try:
        return (
            int(color[1:3], 16),
            int(color[3:5], 16),
            int(color[5:7], 16),
        )
    except ValueError:
        return (0, 0, 0)


_LATCHED_OFF_DIM_FACTOR = 4


def _color_for(binding, latched_on):
    led = binding.get("led", {})
    mode = binding.get("mode", "tap")
    on_rgb = parse_hex(led.get("on", "#000000"))
    if mode == "latched":
        if latched_on:
            return on_rgb
        # Latched-off state. A stompbox-style switch should stay VISIBLE when
        # the effect is off (dim) rather than go black, so you can still see
        # which switch does what. So: honour led.off only if it is a real,
        # non-black colour; if it's absent OR black (#000000 - the editor's
        # default, and what makes an off effect "disappear"), dim the on
        # colour instead. This is what gives "full when on, dimmed when off".
        off_rgb = parse_hex(led["off"]) if led.get("off") is not None else None
        if off_rgb is not None and off_rgb != (0, 0, 0):
            return off_rgb
        return (on_rgb[0] // _LATCHED_OFF_DIM_FACTOR,
                on_rgb[1] // _LATCHED_OFF_DIM_FACTOR,
                on_rgb[2] // _LATCHED_OFF_DIM_FACTOR)
    return on_rgb


class Leds:
    def __init__(self, brightness=0.25):
        self.strip = neopixel.NeoPixel(
            NEOPIXEL_PIN,
            NEOPIXEL_COUNT,
            brightness=brightness,
            auto_write=False,
            pixel_order=neopixel.GRB,
        )

    def clear(self):
        self.strip.fill((0, 0, 0))
        self.strip.show()

    def idle_pattern(self):
        for name in LED_INDEX_PER_SWITCH:
            for idx in LED_INDEX_PER_SWITCH[name]:
                self.strip[idx] = (4, 4, 4)
        self.strip.show()

    def render_patch(self, patch, switches):
        """Repaint every switch's LED ring from its binding's led + mode + latched state."""
        self.strip.fill((0, 0, 0))
        bindings_by_switch = {}
        for b in (patch or {}).get("bindings", []):
            sw = b.get("switch")
            if sw:
                bindings_by_switch[sw] = b
        for sw in switches:
            binding = bindings_by_switch.get(sw.name)
            if not binding:
                continue
            rgb = _color_for(binding, sw.latched_on)
            for idx in LED_INDEX_PER_SWITCH.get(sw.name, ()):
                self.strip[idx] = rgb
        self.strip.show()

    def set_switch_state(self, switch_name, binding, latched_on):
        """Repaint a single switch - used after a latched toggle so we don't
        re-walk all 10 bindings on every press."""
        rgb = _color_for(binding, latched_on)
        for idx in LED_INDEX_PER_SWITCH.get(switch_name, ()):
            self.strip[idx] = rgb
        self.strip.show()

