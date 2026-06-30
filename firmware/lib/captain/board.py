import board

# Footswitch -> GPIO mapping. Order here is the physical NeoPixel chain
# order: switch 1 has pixels 0..2, switch 2 has 3..5, ..., down has 27..29.
# Verified from PySwitch content/lib/pyswitch/hardware/devices/
# pa_midicaptain_10.py - note `up` sits between `4` and `A` in the chain,
# NOT after `D` as alphabetical/grouping order would suggest. The wrong
# order here makes every NeoPixel address from `up` onward off-by-3.
FOOTSWITCHES = {
    "1":    board.GP1,
    "2":    board.GP25,
    "3":    board.GP24,
    "4":    board.GP23,
    "up":   board.GP20,
    "A":    board.GP9,
    "B":    board.GP10,
    "C":    board.GP11,
    "D":    board.GP18,
    "down": board.GP19,
}

# Verified peripherals (see memory/hardware_paint_audio_midi_captain_std.md).
NEOPIXEL_PIN = board.GP7
NEOPIXEL_COUNT = 30          # 3 per footswitch * 10

TFT_PWM   = board.GP8
TFT_DC    = board.GP12
TFT_CS    = board.GP13
SPI_CLK   = board.GP14
SPI_MOSI  = board.GP15

UART_TX   = board.GP16       # DIN MIDI out
UART_RX   = board.GP17       # DIN MIDI in

EXP1_ADC  = board.GP27
EXP2_ADC  = board.GP28

# Additional peripherals PySwitch documents on the same board.
# Not used yet - define here so they're not redefined elsewhere.
WHEEL_BUTTON_PIN   = board.GP0   # rotary-encoder push
WHEEL_ENCODER_A    = board.GP2
WHEEL_ENCODER_B    = board.GP3
BATTERY_LED_PIN    = board.GP4
CHARGING_DETECT    = board.GP6

# Confirmed unused after the inventory above:
FREE_GPIOS = ("GP5", "GP21", "GP22", "GP26")

# NeoPixel indices per switch. HARDCODED - do NOT derive these from
# `enumerate(FOOTSWITCHES.keys())`: CircuitPython on this hardware does
# not preserve dict insertion order (verified empirically via LED_DUMP +
# physical LED_PROBE), so the comprehension produced a randomised
# mapping where the firmware thought e.g. switch A lived at LED 27-29
# while physically LED 15 is A and 27 is down.
#
# Tuples follow PySwitch's pa_midicaptain_10.py - note A/B/C/D/down
# list the middle pixel last (e.g. A is 15, 17, 16). We paint all three
# pixels of a switch the same colour, so the ordering inside the tuple
# is cosmetic until we need per-pixel control (animation, gradients).
LED_INDEX_PER_SWITCH = {
    "1":    (0, 1, 2),
    "2":    (3, 4, 5),
    "3":    (6, 7, 8),
    "4":    (9, 10, 11),
    "up":   (12, 13, 14),
    "A":    (15, 17, 16),
    "B":    (18, 20, 19),
    "C":    (21, 23, 22),
    "D":    (24, 26, 25),
    "down": (27, 29, 28),
}
