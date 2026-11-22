-- Which method produced each page's text: 'text' (the PDF's own text layer),
-- 'tesseract' (local OCR) or 'vision' (the LLM read the rendered page image).
-- The manual page shows e.g. "12 pages read with vision".
ALTER TABLE pages ADD COLUMN ocr_method TEXT NOT NULL DEFAULT 'text';
UPDATE pages SET ocr_method = 'tesseract' WHERE ocr = 1;
