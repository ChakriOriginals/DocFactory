"""Tier-A text extraction: digital PDFs only.

Shared by the worker's parse stage and the eval harness so "how much text does
this document yield" is answered identically everywhere. Scanned (image-only)
PDFs yield empty text here by design — the OCR tier is a later phase.
"""

import io

import pdfplumber


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Raises pdfminer/pdfplumber exceptions on corrupt input — callers decide policy."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
