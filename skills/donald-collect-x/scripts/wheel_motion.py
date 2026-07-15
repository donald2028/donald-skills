#!/usr/bin/env python3
"""Per-session, human-like mouse-wheel motion planning (pure, no I/O).

The previous design (`cdp_input.split_wheel_ticks`) shaped every scroll from
the SAME hardcoded distribution — 110±20 px ticks, 35–120 ms gaps, i.i.d.
Per-event jitter existed, but the *generating distribution* was a constant:
aggregate enough scrolls and the empirical shape converged to the identical
uniform box every session, which is itself a fingerprint (see
human-like scroll motion).

This module fixes that on two axes:

1. Per-session parameters. `new_wheel_profile()` draws a device archetype
   ("mouse" or "trackpad") and its characteristics ONCE per capture session,
   so the distribution itself differs run to run, not just the samples.
2. Real wheel structure. `plan_wheel_motion()` delivers a distance as one or
   more *flicks* (a burst of ticks, then a longer pause before the next
   flick); a mouse keeps a near-constant tick magnitude while a trackpad ramps
   down with momentum decay — instead of a flat i.i.d. tick stream.

Both functions are pure: they take an optional `random.Random` so tests can
seed them, and return data only. CDP dispatch + real sleeping stays in
`capture_thread.AgentBrowser.scroll()`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class WheelProfile:
    """A capture session's input-device characteristics.

    Drawn once per session by `new_wheel_profile()` and reused for every
    `plan_wheel_motion()` call in that session, the way a real user has one
    device for the whole sitting.
    """

    device: str                        # "mouse" | "trackpad"
    base_tick: float                   # characteristic first-tick magnitude (px)
    tick_noise: float                  # fractional per-tick magnitude noise (0..1)
    decay: float                       # per-tick magnitude multiplier within a flick (<=1)
    flick_span: tuple[int, int]        # px one flick covers before re-flicking
    intra_pause: tuple[float, float]   # seconds between ticks inside a flick
    inter_pause: tuple[float, float]   # seconds between flicks


# Common OS wheel line-step sizes (3 lines * line height, physical px).
_MOUSE_BASE_TICKS = (100.0, 114.0, 120.0, 133.0)

# Once a flick's decaying velocity drops below this, its momentum is spent and
# the flick ends — the way real inertial scroll stops at a threshold instead of
# crawling out the last pixels one at a time.
_FLICK_FLOOR_PX = 4.0


def new_wheel_profile(rng: random.Random | None = None) -> WheelProfile:
    """Draw one session's wheel characteristics.

    Picks a device archetype, then samples that archetype's parameters from
    ranges wide enough that two sessions rarely share a distribution shape.
    """
    rng = rng or random
    if rng.random() < 0.5:
        # Mouse wheel: discrete, near-constant ticks; no momentum; clear pauses.
        return WheelProfile(
            device="mouse",
            base_tick=rng.choice(_MOUSE_BASE_TICKS) * rng.uniform(0.92, 1.08),
            tick_noise=rng.uniform(0.04, 0.10),
            decay=1.0,
            flick_span=(rng.randint(280, 420), rng.randint(620, 900)),
            intra_pause=(rng.uniform(0.030, 0.050), rng.uniform(0.075, 0.130)),
            inter_pause=(rng.uniform(0.12, 0.20), rng.uniform(0.30, 0.55)),
        )
    # Trackpad: high-velocity start, exponential momentum decay; tighter gaps.
    return WheelProfile(
        device="trackpad",
        base_tick=rng.uniform(95.0, 170.0),
        tick_noise=rng.uniform(0.08, 0.16),
        decay=rng.uniform(0.74, 0.90),
        flick_span=(rng.randint(380, 560), rng.randint(900, 1400)),
        intra_pause=(rng.uniform(0.006, 0.012), rng.uniform(0.018, 0.032)),
        inter_pause=(rng.uniform(0.10, 0.18), rng.uniform(0.26, 0.48)),
    )


def plan_wheel_motion(
    total_delta: int,
    profile: WheelProfile,
    rng: random.Random | None = None,
) -> list[tuple[int, float]]:
    """Plan one scroll of `total_delta` px as `(delta_y, pause_after_s)` events.

    The distance is delivered as one or more flicks. Inside a flick the velocity
    starts at `profile.base_tick` and is scaled by `profile.decay` each tick
    (momentum), with `profile.tick_noise` jitter on each emitted tick. A flick
    ends when its velocity decays below `_FLICK_FLOOR_PX` (trackpad momentum is
    spent) or it has covered `profile.flick_span` px (a mouse keeps a constant
    velocity, so the span is what bounds the burst). The leftover from a flick
    is carried by the next flick, and the final tick is clamped to the exact
    remainder — so we never grind the tail out in 1px ticks. A pause follows
    every tick: `intra_pause` inside a flick, `inter_pause` before the next.

    Invariants: every delta is a positive int; deltas sum to exactly
    `total_delta`; every pause is >= 0; returns [] for a non-positive distance.
    """
    rng = rng or random
    if total_delta <= 0:
        return []

    events: list[tuple[int, float]] = []
    remaining = total_delta
    while remaining > 0:
        flick_cap = rng.randint(*profile.flick_span)
        flick_emitted = 0
        velocity = profile.base_tick
        flick_ticks: list[int] = []
        while True:
            mag = velocity * (1.0 + profile.tick_noise * rng.uniform(-1.0, 1.0))
            tick = max(1, int(round(mag)))
            if tick > remaining:
                tick = remaining  # never overshoot the whole motion
            flick_ticks.append(tick)
            remaining -= tick
            flick_emitted += tick
            if remaining <= 0:
                break
            velocity *= profile.decay
            if velocity < _FLICK_FLOOR_PX or flick_emitted >= flick_cap:
                break

        last_flick = remaining <= 0
        for i, tick in enumerate(flick_ticks):
            last_in_flick = i == len(flick_ticks) - 1
            if last_in_flick and not last_flick:
                pause = rng.uniform(*profile.inter_pause)
            else:
                pause = rng.uniform(*profile.intra_pause)
            events.append((tick, pause))
    return events
