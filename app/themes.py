"""Theme registry and per-request theme resolution.

Themes themselves are pure CSS: one ``[data-theme="<id>"]`` token block in
``static/themes.css``.  This module only knows their ids and display names, so
that the server can render the right theme into the first paint (no flash) and
the picker can be built generically.

Adding a theme = one token block in ``static/themes.css`` + one entry below.
"""
from fastapi import Request

# Keep this cookie name in sync with COOKIE in static/theme.js.
COOKIE_NAME = "theme"
DEFAULT_THEME = "auto"

# (id, label) — ids match the ``[data-theme="…"]`` blocks in static/themes.css.
THEMES: tuple[tuple[str, str], ...] = (
    ("auto", "Match system"),
    ("light", "Light"),
    ("sepia", "Sepia"),
    ("dark", "Dark"),
    ("parchment", "Parchment"),
    ("blueprint", "Blueprint"),
)
THEME_IDS = frozenset(theme_id for theme_id, _ in THEMES)


def normalize(value: str | None) -> str:
    """Return a known theme id, falling back to the default for anything else."""
    return value if value in THEME_IDS else DEFAULT_THEME


def current(request: Request) -> str:
    """The theme preference this visitor's cookie asks for."""
    return normalize(request.cookies.get(COOKIE_NAME))
