"""Pure navigation helpers for preset preview (no CircuitPython deps).

Kept free of hardware imports so it can be unit-tested on the host
(tools/nav_preview_test.py) and reused by app.py's preview state machine.

The "preview" model: the user scrolls a cursor across the existing patches
WITHOUT loading any of them (no on_enter/on_exit, no device MIDI), then commits
to jump. These helpers only compute WHERE the cursor lands; the side effects
live in app.py.
"""


def patch_order(patch_list):
    """Return the ordered list of (bank, slot) for every patch that exists,
    sorted by bank then slot. `patch_list` is what PatchStore.list() yields:
    an iterable of dicts with "bank" and "slot" keys."""
    pairs = [(int(p["bank"]), int(p["slot"])) for p in patch_list]
    pairs.sort()
    return pairs


def step_index(order, start, delta, scope="patch"):
    """Advance the preview cursor from `start` (a (bank, slot) tuple) by `delta`
    positions and return the new (bank, slot). Wraps around the ends.

    scope "patch": step through every existing patch in `order`.
    scope "bank":  step whole banks (keeping the slot when the target bank has
                   it, else the nearest lower slot, else the bank's first slot).

    Returns `start` unchanged when there is nowhere to go (empty/single order,
    or delta 0). `start` need not itself be present in `order` - we snap to the
    nearest sensible position first.
    """
    if not order:
        return start
    try:
        d = int(delta)
    except (TypeError, ValueError):
        return start
    if d == 0:
        return start

    if scope == "bank":
        return _step_bank(order, start, d)
    return _step_patch(order, start, d)


def _step_patch(order, start, delta):
    n = len(order)
    if n == 1:
        return order[0]
    if start in order:
        i = order.index(start)
    else:
        # Snap to the first patch at or after `start`, else the last one.
        i = 0
        while i < n and order[i] < start:
            i += 1
        if i >= n:
            i = n - 1
    return order[(i + delta) % n]


def _step_bank(order, start, delta):
    banks = sorted({b for (b, _s) in order})
    if not banks:
        return start
    start_bank = start[0]
    if start_bank in banks:
        j = banks.index(start_bank)
    else:
        j = 0
        while j < len(banks) and banks[j] < start_bank:
            j += 1
        if j >= len(banks):
            j = len(banks) - 1
    new_bank = banks[(j + delta) % len(banks)]
    slots = sorted(s for (b, s) in order if b == new_bank)
    if not slots:
        return start
    target_slot = start[1]
    if target_slot in slots:
        return (new_bank, target_slot)
    # Nearest slot at or below the current one, else the bank's first slot.
    lower = [s for s in slots if s <= target_slot]
    return (new_bank, lower[-1] if lower else slots[0])
