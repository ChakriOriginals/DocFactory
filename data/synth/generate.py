"""Synthetic document corpus — render pipeline + CLI.

HTML (Jinja2) -> PDF (WeasyPrint). A quarter to a third of documents are
re-rasterized with mild rotation/noise/JPEG artifacts to simulate scans; those
PDFs have no text layer, so the Tier-A (digital text) parse path cannot read
them — that is intentional, they seed the later OCR tier and the DLQ story.

One document type per run (`--type`), each with its own seed, its own labels
file and its own doc_id prefix, so generating a second type cannot perturb the
first type's corpus or its golden split. A type is a module in this directory
exposing generate_corpus / template_for / render_context / ground_truth_record.

Run via `make seed`, or directly:
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \
        uv run python data/synth/generate.py --type invoice --count 500 --upload
"""

import argparse
import io
import json
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import invoices
import purchase_orders

# The document types this generator can produce. Adding one is a module here
# plus its templates — no change to the render pipeline below.
TYPES = {"invoice": invoices, "purchase_order": purchase_orders}

SYNTH_DIR = Path(__file__).resolve().parent
REPO_ROOT = SYNTH_DIR.parent.parent
DEFAULT_OUT = SYNTH_DIR / "out"
SAMPLES_DIR = REPO_ROOT / "docs" / "samples"

_jinja_env = None


def _env():
    global _jinja_env
    if _jinja_env is None:
        from datetime import date

        from jinja2 import Environment, FileSystemLoader

        def usd(value) -> str:
            return f"${value:,.2f}"

        def eur(value) -> str:
            # 1234.56 -> "1.234,56 €"
            s = f"{value:,.2f}".translate(str.maketrans(",.", ".,"))
            return f"{s} €"

        def pct(rate) -> str:
            return f"{rate * 100:.3f}".rstrip("0").rstrip(".") + "%"

        def date_us(value: date) -> str:
            return value.strftime("%m/%d/%Y")

        def date_long(value: date) -> str:
            return value.strftime("%b %-d, %Y")

        def date_eu(value: date) -> str:
            return value.strftime("%d.%m.%Y")

        _jinja_env = Environment(loader=FileSystemLoader(SYNTH_DIR / "templates"), autoescape=True)
        _jinja_env.filters.update(
            usd=usd, eur=eur, pct=pct, date_us=date_us, date_long=date_long, date_eu=date_eu
        )
    return _jinja_env


def render_pdf(document, module) -> bytes:
    try:
        import weasyprint
    except OSError as exc:  # missing native libs is the common failure on macOS
        raise SystemExit(
            f"WeasyPrint could not load native libraries ({exc}).\n"
            "On macOS: brew install pango, then run via `make seed` "
            "(it sets DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib)."
        ) from exc

    import logging

    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    template = _env().get_template(module.template_for(document))
    return weasyprint.HTML(string=html_of(template, module, document)).write_pdf()


def html_of(template, module, document) -> str:
    return template.render(**module.render_context(document))


def simulate_scan(pdf_bytes: bytes, rng: random.Random) -> bytes:
    """Rasterize to ~150dpi images with rotation/noise/JPEG artifacts, rewrap as PDF."""
    import numpy as np
    import pypdfium2 as pdfium
    from PIL import Image

    noise_rng = np.random.default_rng(rng.getrandbits(32))
    document = pdfium.PdfDocument(pdf_bytes)
    pages = []
    for page in document:
        image = page.render(scale=150 / 72).to_pil().convert("RGB")
        angle = rng.uniform(-1.4, 1.4)
        image = image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
        pixels = np.asarray(image).astype(np.int16)
        pixels = pixels + noise_rng.normal(0, 5.0, pixels.shape)
        # scanners rarely produce true black or white
        pixels = np.clip(pixels, 6, 250).astype(np.uint8)
        image = Image.fromarray(pixels)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=82)
        image = Image.open(buffer)
        image.load()
        pages.append(image)
    out = io.BytesIO()
    pages[0].save(out, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    return out.getvalue()


def build_document(document, module, master_seed: int) -> tuple[str, bytes]:
    pdf = render_pdf(document, module)
    if document.scanned:
        # Seed per-document so output is identical regardless of worker scheduling.
        # (String seeds hash via sha512 — stable across processes and runs.)
        rng = random.Random(f"{master_seed}:{document.doc_id}")
        pdf = simulate_scan(pdf, rng)
    return document.doc_id, pdf


def _worker(args: tuple[object, str, int]) -> tuple[str, bytes]:
    document, doc_type, master_seed = args
    return build_document(document, TYPES[doc_type], master_seed)


def write_previews(corpus: list, out_dir: Path, doc_type: str) -> list[Path]:
    """One PNG per layout (digital) plus one scanned example, for docs/."""
    import pypdfium2 as pdfium

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    # The invoice samples keep their original names; later types are prefixed,
    # since layout names (e.g. "euro") repeat across types.
    prefix = "" if doc_type == "invoice" else f"{doc_type}_"
    picks: dict[str, object] = {}
    for document in corpus:
        if not document.scanned and document.layout not in picks:
            picks[document.layout] = document
        if document.scanned and "scanned" not in picks:
            picks["scanned"] = document
    written = []
    for name, document in sorted(picks.items()):
        name = f"{prefix}{name}"
        pdf_path = out_dir / f"{document.doc_id}.pdf"
        page = pdfium.PdfDocument(pdf_path.read_bytes())[0]
        png_path = SAMPLES_DIR / f"{name}.png"
        page.render(scale=144 / 72).to_pil().save(png_path)
        written.append(png_path)
    return written


def upload_corpus(out_dir: Path, records: list[dict], tenant_id: str, doc_type: str) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    from docfactory_core.storage import ObjectStore

    store = ObjectStore()
    store.ensure_bucket()
    for record in records:
        pdf_path = out_dir / record["file"]
        store.put_object(record["s3_key"], pdf_path.read_bytes(), content_type="application/pdf")
    labels = labels_path(out_dir, doc_type)
    store.put_object(
        f"{tenant_id}/synth/{labels.name}",
        labels.read_bytes(),
        content_type="application/jsonl",
    )
    return len(records) + 1


def labels_path(out_dir: Path, doc_type: str) -> Path:
    """One labels file per document type — the eval splits within a type."""
    return out_dir / f"ground_truth_{doc_type}.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a synthetic document corpus")
    parser.add_argument("--type", dest="doc_type", choices=sorted(TYPES), default="invoice")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--upload", action="store_true", help="upload PDFs + labels to MinIO")
    parser.add_argument("--previews", action="store_true", help="write sample PNGs to docs/")
    parser.add_argument("--tenant", default="dev-tenant")
    args = parser.parse_args()

    module = TYPES[args.doc_type]
    started = time.monotonic()
    corpus = module.generate_corpus(args.count, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for doc_id, pdf in pool.map(
            _worker,
            ((document, args.doc_type, args.seed) for document in corpus),
            chunksize=4,
        ):
            (args.out / f"{doc_id}.pdf").write_bytes(pdf)
            total_bytes += len(pdf)
            done += 1
            if done % 50 == 0:
                print(f"  rendered {done}/{len(corpus)}", flush=True)

    records = [
        module.ground_truth_record(document, s3_key=f"{args.tenant}/synth/{document.doc_id}.pdf")
        for document in corpus
    ]
    with labels_path(args.out, args.doc_type).open("w") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    scanned = sum(1 for document in corpus if document.scanned)
    by_layout = {layout: sum(1 for d in corpus if d.layout == layout) for layout in module.LAYOUTS}
    print(
        f"generated {len(corpus)} {args.doc_type} docs in {time.monotonic() - started:.1f}s "
        f"({total_bytes / 1e6:.1f} MB) -> {args.out}"
    )
    print(f"  layouts: {by_layout}, scanned (no text layer): {scanned}")

    if args.previews:
        for path in write_previews(corpus, args.out, args.doc_type):
            print(f"  preview: {path.relative_to(REPO_ROOT)}")

    if args.upload:
        uploaded = upload_corpus(args.out, records, args.tenant, args.doc_type)
        print(f"  uploaded {uploaded} objects to s3://docfactory/{args.tenant}/synth/")


if __name__ == "__main__":
    main()
