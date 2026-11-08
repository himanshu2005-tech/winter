# -*- coding: utf-8 -*-
"""
soul.py -- VANTA's presence: a small living light.

Not a logo and not a spinner. A few soft lobes of colour drift inside a dark
glass sphere, overlapping additively where they meet, the way light does. At
rest it moves slowly enough to read as breathing. When VANTA listens it opens
and brightens and answers the loudness of the voice; when it thinks the lobes
swirl together and warm; when it speaks it pulses with the speech.

The animation is a continuous system, not a set of frames: each state is just a
set of target parameters (speed, energy, spread, palette) that the live values
glide toward, so a change of state is a change of mood rather than a cut.

Pure numpy for the maths and PIL for the final resample; nothing here touches
Tk, so the look is testable (and screenshot-able) without a window.
"""

import math

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard dependency elsewhere
    np = None

# Palettes: five lobes each, RGB 0..1. Cool and quiet at rest, never a rainbow.
PALETTES = {
    "idle": ((0.36, 0.55, 1.00), (0.55, 0.42, 1.00), (0.25, 0.80, 0.95),
             (0.42, 0.50, 0.95), (0.62, 0.48, 0.98)),
    "listening": ((0.30, 0.62, 1.00), (0.35, 0.90, 1.00), (0.62, 0.55, 1.00),
                  (0.45, 0.80, 1.00), (0.85, 0.92, 1.00)),
    "thinking": ((0.60, 0.42, 1.00), (0.95, 0.45, 0.80), (1.00, 0.68, 0.40),
                 (0.50, 0.50, 1.00), (0.90, 0.55, 0.95)),
    "speaking": ((0.25, 0.85, 0.80), (0.35, 0.62, 1.00), (0.55, 0.95, 0.70),
                 (0.30, 0.75, 1.00), (0.60, 0.88, 0.95)),
    "loading": ((0.45, 0.50, 0.70), (0.50, 0.50, 0.75), (0.40, 0.55, 0.70),
                (0.48, 0.48, 0.72), (0.52, 0.52, 0.78)),
}

# speed: how fast lobes orbit. energy: brightness. spread: how far from the
# centre they roam. swirl: extra rotation of the whole system. breath: size
# oscillation amplitude.
MOODS = {
    "idle": dict(speed=0.30, energy=0.98, spread=0.54, swirl=0.04, breath=0.085, size=1.04),
    "loading": dict(speed=0.30, energy=0.45, spread=0.30, swirl=0.10, breath=0.03, size=0.96),
    "listening": dict(speed=0.55, energy=1.00, spread=0.55, swirl=0.00, breath=0.08, size=1.06),
    "thinking": dict(speed=0.85, energy=0.95, spread=0.50, swirl=0.55, breath=0.04, size=1.02),
    "speaking": dict(speed=0.60, energy=0.95, spread=0.52, swirl=0.10, breath=0.10, size=1.04),
}

# The two faces of rest. When the camera has matched the owner the soul is
# alive: bright, saturated, drifting and visibly breathing (the idle mood
# above). When it has not -- someone unmatched at the desk, or no usable
# reading -- it is dull: a small, grey, dim sphere that barely moves, so the
# difference is readable across the room, not just on close inspection.
DULL = ((0.44, 0.46, 0.52), (0.50, 0.50, 0.54), (0.41, 0.45, 0.50),
        (0.46, 0.47, 0.51), (0.50, 0.50, 0.54))
DULL_IDLE = dict(speed=0.05, energy=0.26, spread=0.18, swirl=0.00, breath=0.010, size=0.88)
DULL_STRENGTH = {"idle": 1.0, "loading": 0.6}       # working moods keep their colour
DULL_DEFAULT = 0.30

LOBES = 5
_SIGMA = (0.31, 0.27, 0.25, 0.29, 0.23)
_RATE = (1.00, -0.83, 0.67, -1.21, 0.93)
_PHASE = (0.0, 1.9, 3.4, 4.6, 5.8)
_ORBIT = (0.80, 0.62, 0.95, 0.55, 0.72)


_GEOMETRY = {}


def _ease(current, target, dt, rate=3.2):
    """Glide `current` toward `target`; frame-rate independent."""
    return current + (target - current) * (1.0 - math.exp(-rate * max(0.0, dt)))


def hex_to_rgb(value):
    value = str(value).lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class Soul:
    def __init__(self, state="idle"):
        self.t = 0.0
        self.angle = 0.0
        self.level = 0.0          # smoothed voice level, 0..1
        self._level_target = 0.0
        mood = MOODS.get(state, MOODS["idle"])
        self.params = dict(mood)
        self.palette = [list(c) for c in PALETTES.get(state, PALETTES["idle"])]
        self.state = state
        self.guarded = False      # True = dull (owner not matched)
        self.guard = 0.0          # eased 0..1: how far toward the dull look
        self._pulse = 0.0

    def set_state(self, state):
        self.state = state if state in MOODS else "idle"

    def set_guarded(self, guarded):
        """True while the person at the desk is not confirmed as the owner."""
        self.guarded = bool(guarded)

    def pulse(self, strength=0.8):
        """A single soft swell, e.g. when the owner is recognised."""
        self._pulse = max(self._pulse, max(0.0, min(1.0, float(strength))))

    def set_level(self, level):
        """Loudness 0..1 of whatever voice is active (mic or speech)."""
        self._level_target = max(0.0, min(1.0, float(level or 0.0)))

    def step(self, dt):
        """Advance time by dt seconds and glide toward the current mood."""
        dt = max(0.0, min(float(dt), 0.25))
        mood = MOODS[self.state]
        self.guard = _ease(self.guard, 1.0 if self.guarded else 0.0, dt, 1.4)
        for key, target in mood.items():
            if self.state == "idle":
                target = target + (DULL_IDLE[key] - target) * self.guard
            self.params[key] = _ease(self.params[key], target, dt)
        mix = self.guard * DULL_STRENGTH.get(self.state, DULL_DEFAULT)
        palette = PALETTES[self.state]
        for lobe in range(LOBES):
            for channel in range(3):
                target = palette[lobe][channel] * (1.0 - mix) + DULL[lobe][channel] * mix
                self.palette[lobe][channel] = _ease(
                    self.palette[lobe][channel], target, dt, 2.4)
        self._pulse = max(0.0, self._pulse - dt * 1.4)
        self.level = _ease(self.level, max(self._level_target, self._pulse), dt, 9.0)
        self.t += dt * self.params["speed"]
        self.angle += dt * self.params["swirl"]

    @staticmethod
    def _geometry(size, background, supersample):
        """Everything about the picture that never changes between frames.

        Computed once per (size, background): the pixel grid, the depth shading,
        the highlight, the edge mask and the halo falloff. Per frame only the
        five moving lobes are evaluated.
        """
        key = (size, background, supersample)
        cached = _GEOMETRY.get(key)
        if cached is not None:
            return cached
        n = int(size * supersample)
        axis = (np.arange(n, dtype="float32") + 0.5) / n * 2.0 - 1.0
        x, y = np.meshgrid(axis, axis)
        radius = np.sqrt(x * x + y * y)
        rim = np.clip((radius - 0.62) / 0.32, 0.0, 1.0) ** 2
        depth = (0.80 + 0.20 * (1.0 - np.clip(radius, 0, 1) ** 2))[..., None]
        spec = (np.exp(-(((x + 0.30) ** 2 + (y + 0.52) ** 2) / 0.035)) * 0.07)[..., None]
        edge = 0.94
        soft = 1.5 / n * 2
        alpha = np.clip((edge - radius) / (soft * 2.2) + 0.5, 0.0, 1.0)[..., None]
        halo = np.clip(1.0 - (radius - edge) / 0.06, 0.0, 1.0) * (radius > edge)
        bg = np.asarray(hex_to_rgb(background), dtype="float32")[None, None, :]
        geometry = dict(axis=axis, n=n, rim=rim[..., None] * 0.16, depth=depth, spec=spec,
                        alpha=alpha, one_minus_alpha=1.0 - alpha, halo=halo[..., None],
                        bg=bg, base=bg * 0.30)
        if len(_GEOMETRY) > 8:
            _GEOMETRY.clear()
        _GEOMETRY[key] = geometry
        return geometry

    def render(self, size=64, background="#161618", supersample=2):
        """A PIL RGB image of the orb on `background`, `size` px square."""
        from PIL import Image
        if np is None:
            return Image.new("RGB", (size, size), background)
        g = self._geometry(size, background, supersample)
        n, axis = g["n"], g["axis"]
        p = self.params
        gain = p["energy"] * (1.0 + 0.55 * self.level)
        reach = p["spread"] * (1.0 + 0.35 * self.level)
        scale = p["size"] * (1.0 + p["breath"] * math.sin(self.t * 2.1) + 0.10 * self.level)
        # A round lobe is separable: exp(-(dx^2+dy^2)/2s^2) = exp(-dx^2/2s^2) * exp(-dy^2/2s^2).
        # Two 1-D curves and an outer product replace a per-pixel exponential.
        gx = np.empty((LOBES, n), dtype="float32")
        gy = np.empty((LOBES, n), dtype="float32")
        for i in range(LOBES):
            a = self.t * _RATE[i] * 2.0 + _PHASE[i] + self.angle
            # A second, slower component makes the path organic, not circular.
            wobble = 0.35 * math.sin(self.t * 1.3 * _RATE[i] + _PHASE[i] * 1.7)
            r = reach * _ORBIT[i] * (1.0 + wobble)
            cx, cy = r * math.cos(a), r * math.sin(a * 0.93 + 0.6)
            sigma = _SIGMA[i] * scale * (1.0 + 0.18 * math.sin(self.t * 1.7 + i))
            k = -1.0 / (2.0 * sigma * sigma)
            gx[i] = np.exp((axis - cx) ** 2 * k)
            gy[i] = np.exp((axis - cy) ** 2 * k)
        lobes = gy[:, :, None] * gx[:, None, :]                      # (LOBES, n, n)
        acc = np.einsum("ihw,ic->hwc", lobes, np.asarray(self.palette, dtype="float32"))
        light = 1.0 - np.exp(-(gain * 1.05) * acc)
        light *= g["depth"]
        light += g["rim"] * (acc / (1.0 + acc.max()))
        light += g["spec"]
        np.clip(light, 0.0, 1.0, out=light)
        bg, alpha = g["bg"], g["alpha"]
        sphere = g["base"] + (1.0 - g["base"]) * light
        mean = light.mean(axis=2, keepdims=True)
        # A thin halo just outside the sphere takes a little of the light.
        halo = (g["halo"] * (0.10 * gain)) * mean
        out = bg + (sphere - bg) * alpha + mean * halo * g["one_minus_alpha"]
        np.clip(out, 0.0, 1.0, out=out)
        image = Image.fromarray((out * 255.0 + 0.5).astype("uint8"), "RGB")
        if supersample != 1:
            image = image.resize((size, size), Image.BOX if supersample == 2 else Image.LANCZOS)
        return image
