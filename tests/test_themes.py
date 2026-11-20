from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import themes
from app.main import app
from app.themes import DEFAULT_THEME, THEME_IDS


def client():
    return TestClient(app)


def html_theme(response) -> str:
    """The data-theme attribute the server renders on <html>."""
    text = response.text
    start = text.index("<html")
    tag = text[start:text.index(">", start)]
    for attr in tag.split():
        if attr.startswith("data-theme="):
            return attr.split("=", 1)[1].strip('"')
    raise AssertionError(f"no data-theme on <html>: {tag}")


@pytest.mark.parametrize("value, expected", [
    ("light", "light"),
    ("dark", "dark"),
    ("auto", "auto"),
    ("", DEFAULT_THEME),
    ("solarized", DEFAULT_THEME),
    (None, DEFAULT_THEME),
])
def test_normalize(value, expected):
    assert themes.normalize(value) == expected


def test_default_theme_is_rendered_on_login():
    with client() as c:
        r = c.get("/login")
        assert r.status_code == 200
        assert html_theme(r) == DEFAULT_THEME
        assert "/static/themes.css" in r.text
        assert "/static/app.css" in r.text
        assert "/static/theme.js" in r.text


def test_cookie_selects_theme():
    with client() as c:
        c.cookies.set(themes.COOKIE_NAME, "dark")
        assert html_theme(c.get("/login")) == "dark"
        assert 'value="dark" selected' in c.get("/login").text
        c.cookies.set(themes.COOKIE_NAME, "light")
        assert html_theme(c.get("/login")) == "light"


def test_unknown_cookie_falls_back_to_default():
    with client() as c:
        c.cookies.set(themes.COOKIE_NAME, "hotdog-stand")
        assert html_theme(c.get("/login")) == DEFAULT_THEME


def test_picker_offers_every_registered_theme():
    with client() as c:
        c.post("/login", data={"password": "pw"})
        r = c.get("/manuals")
        assert r.status_code == 200
        assert "data-theme-picker" in r.text
        for theme_id, label in themes.THEMES:
            assert f'value="{theme_id}"' in r.text
            assert f">{label}<" in r.text


def test_every_theme_has_a_token_block():
    css = (Path(__file__).parent.parent / "app" / "static" / "themes.css").read_text()
    for theme_id in THEME_IDS - {"auto"}:
        assert f'[data-theme="{theme_id}"]' in css
    # "auto" is resolved by theme.js, so every theme needs a colour-scheme hint
    assert css.count("color-scheme:") >= len(THEME_IDS - {"auto"})


def test_app_css_has_no_hard_coded_colours():
    css = (Path(__file__).parent.parent / "app" / "static" / "app.css").read_text()
    for literal in ("#fff", "#f7f5f0", "#161714", "rgba(", "prefers-color-scheme"):
        assert literal not in css, f"{literal} belongs in themes.css, not app.css"
