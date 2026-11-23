import os
import tempfile
from pathlib import Path

import pymupdf as fitz
import pytest

_DATA = Path(tempfile.mkdtemp(prefix="manualagerie-test-"))
os.environ.update({
    "DATA_DIR": str(_DATA),
    "APP_PASSWORD": "pw",
    "AUTH_DISABLED": "false",
    "START_WORKER": "false",
    "OPENROUTER_API_KEY": "",
    "SECRET_KEY": "test-secret",
})

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
db.init_storage()


DISHWASHER_PAGES = [
    [("INSTALLATION INSTRUCTIONS", 20), ("Bosch 800 Series Dishwasher SHPM88Z75N", 12),
     ("Read all instructions before installing the dishwasher. Keep these instructions for the local inspector.", 10)],
    [("WATER SUPPLY CONNECTION", 16),
     ("Connect the water inlet using a 3/8 inch compression fitting. The supply line must provide 20-120 psi. "
      "Use a braided stainless steel supply line.", 10)],
    [("DRAIN HOSE", 16),
     ("The drain hose fits 5/8 inch to 1 inch connections at the sink drain or garbage disposal. "
      "Secure with the supplied hose clamp. Create a high loop of at least 20 inches.", 10)],
    [("INSTRUCTIONS DE SÉCURITÉ", 16),
     ("Veuillez lire toutes les instructions avant d'installer le lave-vaisselle. Le tuyau de vidange doit être "
      "raccordé correctement à l'évier ou au broyeur à déchets avec le collier fourni.", 10)],
    [],  # blank / scanned-like page
]


def make_pdf(path: Path, pages=DISHWASHER_PAGES) -> Path:
    doc = fitz.open()
    for blocks in pages:
        page = doc.new_page()
        y = 72
        for text, size in blocks:
            rect = fitz.Rect(72, y, 540, y + 200)
            page.insert_textbox(rect, text, fontsize=size, fontname="helv")
            y += size * 1.6 * (1 + len(text) // 80) + 14
    doc.save(path)
    doc.close()
    return path


TEST_USER, TEST_PASSWORD = "tester", "pw"


def ensure_user(username: str = TEST_USER, password: str = TEST_PASSWORD, *, is_admin: bool = True,
                display_name: str | None = None) -> int:
    """Create the account if it doesn't exist yet (enabled, with this password). Returns its id."""
    from app.repo import users as users_repo
    with db.get_conn() as c:
        u = users_repo.get_by_username(c, username)
        if u is not None:
            return u["id"]
        return users_repo.create(c, username, display_name or username.title(), password, is_admin=is_admin)


def login(client, username: str = TEST_USER, password: str = TEST_PASSWORD, *, is_admin: bool = True):
    """Log `client` in as `username`, creating that user first if needed. Returns the /login response."""
    ensure_user(username, password, is_admin=is_admin)
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def pdf_path(tmp_path):
    return make_pdf(tmp_path / "dishwasher.pdf")
