from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _status_label(m) -> str:
    if m["status"] == "processing" and m["page_count"]:
        return f"processing {m['pages_done']}/{m['page_count']}"
    return m["status"]


templates.env.globals["status_label"] = _status_label
