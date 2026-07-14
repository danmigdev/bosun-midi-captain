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


# Off (dimmed) latched LED brightness, on the SAME 0-255 scale as the overall
# LED brightness (device.leds.brightness) so the two settings share one unit.
# It scales the on colour: 255 = as bright as on, 0 = off. 64 == the old fixed
# "divide by 4" (25%). User-configurable via device.leds.dim.
_LATCHED_OFF_DIM = 64


def _color_for(binding, latched_on, dim=_LATCHED_OFF_DIM):
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
        d = dim
        # Round, don't floor, the dim scaling. NeoPixel then applies the overall
        # brightness with a SECOND integer truncation, so a channel that floors
        # to 3 here becomes 0 on the strip (3 * 0.25 -> 0) while one that reaches
        # 4 survives (4 * 0.25 -> 1). Flooring therefore made some latched-off
        # colours vanish to black at low dim while others stayed lit - uneven,
        # colour-dependent quantization (magenta/yellow died, red/blue survived
        # at the same dim). Rounding lets the dominant channel reach 4 so every
        # colour degrades consistently. (+127 == round-half-up on the /255.)
        return ((on_rgb[0] * d + 127) // 255,
                (on_rgb[1] * d + 127) // 255,
                (on_rgb[2] * d + 127) // 255)
    return on_rgb


class Leds:
    def __init__(self, brightness=0.25, dim=_LATCHED_OFF_DIM):
        # Off (dimmed) latched LED brightness, 0-255 scaling of the on colour.
        self.dim = dim
        # Overall brightness (0.0-1.0). We drive the strip at 1.0 and scale each
        # colour ourselves in scale() - see there for why the library's own
        # brightness multiply is unusable at low dim.
        self._brightness = brightness
        self.strip = neopixel.NeoPixel(
            NEOPIXEL_PIN,
            NEOPIXEL_COUNT,
            brightness=1.0,
            auto_write=False,
            pixel_order=neopixel.GRB,
        )

    def set_brightness(self, brightness):
        """Set the overall LED brightness (0.0-1.0). Takes effect on the next
        repaint (the caller triggers one)."""
        self._brightness = brightness

    def scale(self, rgb):
        """Apply the overall brightness to a 0-255 colour, ROUNDING each channel.
        We keep the NeoPixel strip at brightness=1.0 and scale here instead,
        because the library's brightness multiply FLOORS: at a low effective
        level a channel of 0.75 drops to 0, so a dimmed colour loses its
        secondary channels and its hue collapses (dimmed magenta -> pure red,
        dimmed yellow -> red). Rounding keeps the hue - 0.75 rounds up to 1 -
        so the dimmed LED stays the same colour as when it is on, just fainter."""
        b = self._brightness
        return (int(rgb[0] * b + 0.5),
                int(rgb[1] * b + 0.5),
                int(rgb[2] * b + 0.5))

    def clear(self):
        self.strip.fill((0, 0, 0))
        self.strip.show()

    def idle_pattern(self):
        c = self.scale((4, 4, 4))
        for name in LED_INDEX_PER_SWITCH:
            for idx in LED_INDEX_PER_SWITCH[name]:
                self.strip[idx] = c
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
            rgb = self.scale(_color_for(binding, sw.latched_on, self.dim))
            for idx in LED_INDEX_PER_SWITCH.get(sw.name, ()):
                self.strip[idx] = rgb
        self.strip.show()

    def set_switch_state(self, switch_name, binding, latched_on):
        """Repaint a single switch - used after a latched toggle so we don't
        re-walk all 10 bindings on every press."""
        rgb = self.scale(_color_for(binding, latched_on, self.dim))
        for idx in LED_INDEX_PER_SWITCH.get(switch_name, ()):
            self.strip[idx] = rgb
        self.strip.show()

