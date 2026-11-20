from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import themes
from .repo import manuals as manuals_repo

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _status_label(m) -> str:
    if m["status"] == "processing" and m["page_count"]:
        return f"processing {m['pages_done']}/{m['page_count']}"
    return m["status"]


templates.env.globals["status_label"] = _status_label
templates.env.globals["themes"] = themes.THEMES
templates.env.globals["theme_of"] = themes.current
templates.env.globals["tag_list"] = manuals_repo.parse_tags
