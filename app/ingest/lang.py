from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0
MIN_CHARS = 60


def detect_lang(text: str) -> str | None:
    letters = sum(c.isalpha() for c in text)
    if letters < MIN_CHARS:
        return None
    try:
        return detect(text[:3000])
    except LangDetectException:
        return None
