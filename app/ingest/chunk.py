"""Turn structured pages into overlapping, page-aware chunks."""
from .extract import Page

TARGET_WORDS = 350
MIN_WORDS_BEFORE_HEADING_BREAK = 80
OVERLAP_WORDS = 50
MAX_PARAGRAPH_WORDS = 450


def _words(s: str) -> int:
    return len(s.split())


def _split_long(text: str) -> list[str]:
    words = text.split()
    if len(words) <= MAX_PARAGRAPH_WORDS:
        return [text]
    return [" ".join(words[i:i + TARGET_WORDS]) for i in range(0, len(words), TARGET_WORDS)]


def chunk_pages(pages: list[Page]) -> list[dict]:
    chunks: list[dict] = []
    buf: list[tuple[int, str]] = []  # (page_no, text)
    heading: str | None = None
    buf_heading: str | None = None
    buf_lang: str | None = None

    def flush(keep_overlap: bool):
        nonlocal buf, buf_heading
        if not buf:
            return
        body = "\n".join(t for _, t in buf).strip()
        if body:
            chunks.append({
                "page_start": buf[0][0],
                "page_end": buf[-1][0],
                "heading": buf_heading,
                "lang": buf_lang,
                "text": body,
            })
        tail: list[tuple[int, str]] = []
        if keep_overlap:
            n = 0
            for p, t in reversed(buf):
                if n + _words(t) > OVERLAP_WORDS:
                    break
                tail.insert(0, (p, t))
                n += _words(t)
        buf = tail
        buf_heading = heading

    for page in pages:
        if buf and buf_lang != page.lang:
            flush(keep_overlap=False)
        buf_lang = page.lang
        # a chunk may span at most two consecutive pages
        if buf and page.page_no > buf[0][0] + 1:
            flush(keep_overlap=True)
            if buf and page.page_no > buf[0][0] + 1:
                buf = []
        for para in page.paragraphs:
            if para.is_heading:
                current = sum(_words(t) for _, t in buf)
                if current >= MIN_WORDS_BEFORE_HEADING_BREAK:
                    heading = para.text
                    flush(keep_overlap=False)
                    buf_heading = heading
                else:
                    heading = para.text
                    if not buf:
                        buf_heading = heading
                buf.append((page.page_no, para.text))
                continue
            for piece in _split_long(para.text):
                if buf and sum(_words(t) for _, t in buf) + _words(piece) > TARGET_WORDS:
                    flush(keep_overlap=True)
                    if buf and page.page_no > buf[0][0] + 1:
                        buf = []
                if not buf:
                    buf_heading = heading
                buf.append((page.page_no, piece))
    flush(keep_overlap=False)
    # drop chunks that are only overlap-free headings with no content
    return [c for c in chunks if _words(c["text"]) >= 3]
