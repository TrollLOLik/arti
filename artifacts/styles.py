"""Accessible text styles and shared semantic rose/burgundy design tokens.

The original seven-field StyleProfile contract is retained for saved artifacts.
Decorative fills deliberately live outside that text-color contract.
"""
from dataclasses import dataclass, asdict
import re
from materials.types import MaterialError


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))


def contrast(first, second):
    a, b = luminance(first), luminance(second)
    return (max(a, b) + .05) / (min(a, b) + .05)


@dataclass(frozen=True)
class StyleProfile:
    background: str = '#E4D8D8'
    foreground: str = '#300000'
    accent: str = '#840018'
    muted: str = '#784848'
    density: str = 'comfortable'
    illustration_tone: str = 'calm'
    font: str = 'DejaVuSans'

    def __post_init__(self):
        colors = (self.background, self.foreground, self.accent, self.muted)
        if (any(not isinstance(c, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', c) for c in colors)
                or self.density not in ('comfortable', 'compact')
                or self.illustration_tone not in ('calm', 'playful', 'formal')
                or self.font != 'DejaVuSans'):
            raise MaterialError('artifact_style_invalid')
        if any(contrast(self.background, c) < 4.5 for c in colors[1:]):
            raise MaterialError('artifact_contrast')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        try:
            return cls(**value)
        except TypeError:
            raise MaterialError('artifact_style_invalid') from None


def semantic_theme(style=None):
    """Return semantic tokens without extending persisted StyleProfile dictionaries.

    Old/custom palettes retain their text colors. A raised surface is used only
    when every text role remains WCAG-AA readable on it; otherwise it stays on
    the validated background. Series colors never double as text colors.
    """
    if style is None:
        style = StyleProfile()
    elif isinstance(style, dict):
        style = StyleProfile.from_dict(style)
    surface = '#FFF9F9' if luminance(style.background) > .35 else '#300000'
    if any(contrast(surface, color) < 4.5 for color in (style.foreground, style.accent, style.muted)):
        surface = style.background
    inverse = max(('#FFF9F9', '#300000'), key=lambda c: contrast(c, style.accent))
    return dict(background=style.background, surface=surface, soft_surface=style.background,
                text=style.foreground, heading=style.accent, secondary=style.muted,
                border='#C0A8A8', series='#D87890', series_alt='#B4606C',
                marker='#F03048', inverse=inverse)


STYLES = {
    'calm': StyleProfile(),
    'ink': StyleProfile('#FFF9F9', '#300000', '#840018', '#784848', 'compact', 'formal'),
    'night': StyleProfile('#300000', '#E4D8D8', '#D87890', '#C0A8A8'),
}
