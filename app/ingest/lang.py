from langdetect import DetectorFactory, LangDetectException, detect, detect_langs

from .extract import Page

DetectorFactory.seed = 0
MIN_CHARS = 60
# Only drop a paragraph when langdetect is this sure it's in another language.
STRIP_MIN_PROB = 0.9


def _letters(text: str) -> int:
    return sum(c.isalpha() for c in text)


def detect_lang(text: str) -> str | None:
    if _letters(text) < MIN_CHARS:
        return None
    try:
        return detect(text[:3000])
    except LangDetectException:
        return None


def _confident_lang(text: str) -> str | None:
    if _letters(text) < MIN_CHARS:
        return None
    try:
        best = detect_langs(text[:3000])[0]
    except (LangDetectException, IndexError):
        return None
    return best.lang if best.prob >= STRIP_MIN_PROB else None


def strip_other_languages(pages: list[Page], keep: set[str]) -> int:
    """Drop paragraphs written in languages outside `keep`, in place.

    Multilingual manuals repeat the same content in EN/FR/ES/...; keeping only the
    reader's languages avoids duplicate search hits and wasted LLM context. Detection
    is per paragraph, so pages with side-by-side language columns are handled too.

    Errs toward keeping text: a paragraph is dropped only if it is long enough for
    reliable detection and confidently non-`keep`. Short paragraphs (headings, labels)
    take the language of their neighbours: a heading follows the paragraph after it,
    other short text the one before it. Text with no confident neighbour is kept.
    If no paragraph in the whole manual is confidently in a `keep` language
    (e.g. a German-only manual), nothing is stripped. Returns paragraphs dropped.
    """
    if not keep:
        return 0
    per_page = [[_confident_lang(p.text) for p in page.paragraphs] for page in pages]
    if not any(lang in keep for langs in per_page for lang in langs):
        return 0

    dropped = 0
    for page, langs in zip(pages, per_page):
        resolved: list[str | None] = []
        for i, (para, lang) in enumerate(zip(page.paragraphs, langs)):
            if lang is None and _letters(para.text) >= 3:
                after = next((l for l in langs[i + 1:] if l), None)
                before = next((l for l in reversed(resolved) if l), None)
                lang = (after or before) if para.is_heading else (before or after)
            resolved.append(lang)
        kept = [p for p, lang in zip(page.paragraphs, resolved) if lang is None or lang in keep]
        if len(kept) != len(page.paragraphs):
            dropped += len(page.paragraphs) - len(kept)
            page.paragraphs = kept
            page.text = "\n\n".join(p.text for p in kept)
    return dropped
