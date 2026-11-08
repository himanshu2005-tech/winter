# -*- coding: utf-8 -*-
"""
theme.py -- one palette and one type scale for every VANTA surface.

Colours and font sizes used to be written inline at each widget, which is how
the Control Center ended up with five slightly different greys and seven font
sizes that meant nothing in particular. Everything visual now resolves through
this module, so the idle orb and the dashboard cannot drift apart.

The palette is macOS dark-mode's actual system colours rather than hand-picked
"nice" hex values, and the elevation ladder is the part that does the work:
surfaces get *lighter* as they come forward and darker as they recess, which
is what separates panels without needing a border on everything.

Accent colour is rationed. It appears on the state dot, the selected sidebar
row, the active mode chip and a bar that has crossed into trouble -- nowhere
else. Colour everywhere is colour nowhere, and it is what makes an assistant
UI read as a sci-fi mock-up instead of a tool.
"""

# --- transparency key ------------------------------------------------ #
MAGIC = "#010101"          # window transparency colour -- never use as paint

# --- elevation ladder ------------------------------------------------ #
BG_WINDOW = "#161618"      # the window itself, furthest back
BG_RAIL = "#1d1d1f"        # sidebar, one step forward
BG_CARD = "#232326"        # cards, the main reading surface
BG_CARD_HOVER = "#2b2b2f"  # a card or row under the pointer
BG_INSET = "#0f0f11"       # recessed wells: text areas, canvases, editors
BG_CONTROL = "#2c2c2e"     # buttons and fields sitting on a card

SURFACE = "#1c1c1e"        # legacy name kept for the notch and overlays
SURFACE_DEEP = "#0f0f11"
SEPARATOR = "#2e2e31"      # hairline rules
BORDER_SOFT = "#3a3a3c"

# --- labels ---------------------------------------------------------- #
LABEL_PRIMARY = "#f5f5f7"
LABEL_SECONDARY = "#98989d"
LABEL_TERTIARY = "#636366"

# --- system accents -------------------------------------------------- #
BLUE = "#0a84ff"
GREEN = "#30d158"
YELLOW = "#ffd60a"
ORANGE = "#ff9f0a"
RED = "#ff453a"
PURPLE = "#bf5af2"
TEAL = "#64d2ff"

# --- state -------------------------------------------------------- #
STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"

ACCENTS = {
    STATE_IDLE: "#8e8e93",      # systemGray: at rest there is no state to announce
    STATE_LOADING: "#8e8e93",
    STATE_LISTENING: BLUE,
    STATE_THINKING: YELLOW,
    STATE_SPEAKING: GREEN,
}

STATE_LABELS = {
    STATE_IDLE: "VANTA",
    STATE_LOADING: "Starting up",
    STATE_LISTENING: "Listening",
    STATE_THINKING: "Working",
    STATE_SPEAKING: "Speaking",
}


# --- type scale ------------------------------------------------------ #
# (family key, size, weight). Nine roles, deliberately: every extra size is
# one more thing that means nothing. Family keys resolve at runtime against
# whichever of SF Pro / Segoe UI Variable / Segoe UI is actually installed.
TYPE_SCALE = {
    "display":   ("display", 22, "bold"),      # the one big title per window
    "title":     ("display", 16, "bold"),      # page titles
    "card":      ("text", 13, "bold"),         # card headings
    "heading":   ("text", 11, "bold"),         # small caps section rules
    "body":      ("text", 14, "normal"),       # reading text
    "body_em":   ("text", 14, "bold"),
    "label":     ("text", 12, "normal"),       # control labels
    "caption":   ("text", 11, "normal"),       # supporting detail
    "metric":    ("display", 25, "bold"),      # the number on a stat tile
    "metric_sm": ("display", 17, "bold"),
    "mono":      ("mono", 11, "normal"),       # paths, IDs, telemetry
    "mono_body": ("mono", 12, "normal"),
}


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def blend(hex_a, hex_b, amount):
    """Linear interpolation between two colours, amount in [0, 1]."""
    a, b = hex_to_rgb(hex_a), hex_to_rgb(hex_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def dim(hex_color, amount=0.55, toward=SURFACE):
    """Pull a colour toward a surface so it reads as a tint, not a neon."""
    return blend(hex_color, toward, amount)


def load_ratio_color(ratio, charging=None):
    """Green / amber / red for a utilisation bar.

    Colour here is information, not decoration: a bar only earns an accent
    once it has something to say, which is why the normal range is neutral.
    """
    if charging:
        return GREEN
    if ratio is None:
        return LABEL_TERTIARY
    if ratio >= 0.90:
        return RED
    if ratio >= 0.75:
        return ORANGE
    return BLUE


class ThemedFonts:
    """Mixin giving a Tk-based class a cached, named type scale.

    Requires ``self.FONT_DISPLAY`` / ``FONT_TEXT`` / ``FONT_MONO`` to have
    been resolved first, which can only happen after Tk is up.
    """

    def font(self, role):
        cache = getattr(self, "_font_cache", None)
        if cache is None:
            cache = self._font_cache = {}
        if role in cache:
            return cache[role]

        import customtkinter as ctk

        family_key, size, weight = TYPE_SCALE.get(role, TYPE_SCALE["body"])
        family = {
            "display": getattr(self, "FONT_DISPLAY", "Segoe UI"),
            "text": getattr(self, "FONT_TEXT", "Segoe UI"),
            "mono": getattr(self, "FONT_MONO", "Consolas"),
        }[family_key]
        cache[role] = ctk.CTkFont(family=family, size=size, weight=weight)
        return cache[role]

    def tk_font(self, role):
        """The same role as a plain Tk font tuple, for classic widgets."""
        family_key, size, weight = TYPE_SCALE.get(role, TYPE_SCALE["body"])
        family = {
            "display": getattr(self, "FONT_DISPLAY", "Segoe UI"),
            "text": getattr(self, "FONT_TEXT", "Segoe UI"),
            "mono": getattr(self, "FONT_MONO", "Consolas"),
        }[family_key]
        return (family, size, "bold") if weight == "bold" else (family, size)
