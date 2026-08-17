"""Synthetic invoice corpus — data layer.

Every invoice is internally consistent by construction: each line amount is
quantity x unit_price exact to the cent, the subtotal is the exact sum of line
amounts, and subtotal + tax = total. Phase 1's deterministic validation
re-checks exactly these rules against extraction output, so this module is the
contract for what "consistent" means.

Everything derives from one master seed and a fixed reference date, so the
corpus is fully reproducible across machines and days — the eval harness
depends on that for a stable golden-set split.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from faker import Faker

LAYOUTS = ("classic", "modern", "euro")
SCAN_FRACTION = 0.3
CENT = Decimal("0.01")

# Corpus dates are relative to this fixed day, never to "today".
REFERENCE_DATE = date(2026, 8, 1)

US_TAX_RATES = ("0", "0.045", "0.0625", "0.075", "0.08875", "0.095")
EU_VAT_RATES = ("0.19", "0.20", "0.21")

EN_ITEMS = (
    "Consulting services",
    "Cloud hosting",
    "Software license renewal",
    "On-site training day",
    "Design retainer",
    "Support plan (Silver)",
    "Support plan (Gold)",
    "Data migration services",
    "API integration work",
    "Quarterly maintenance",
    "Hardware: USB-C dock",
    'Hardware: 27" monitor',
    "Annual subscription — Pro tier",
    "Security audit",
    "Content writing (per article)",
    "SEO optimization package",
    "Translation services (per 1k words)",
    "Server backup service",
    "Domain renewal (.com)",
    "SSL certificate (1 yr)",
    "Custom report development",
    "Staff augmentation — engineering",
    "Workshop facilitation",
    "Equipment rental — projector",
)

DE_ITEMS = (
    "Beratungsleistungen",
    "Cloud-Hosting",
    "Softwarelizenz (Jahresabo)",
    "Schulungstag vor Ort",
    "Wartungspauschale",
    "Support-Paket Silber",
    "Support-Paket Gold",
    "Datenmigration",
    "API-Integration",
    "Quartalswartung",
    "Hardware: USB-C Dock",
    'Hardware: Monitor 27"',
    "Sicherheitsaudit",
    "Übersetzungsleistungen",
    "Serversicherung",
    "Domainverlängerung",
    "SSL-Zertifikat (1 Jahr)",
    "Individuelle Berichte",
    "Projektmanagement",
    "Workshop-Moderation",
)

EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip

DE_MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)  # fmt: skip


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: int
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Party:
    name: str
    address_lines: tuple[str, ...]
    vat_id: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class Invoice:
    doc_id: str
    layout: str
    scanned: bool
    currency: str
    vendor: Party
    bill_to: Party
    invoice_number: str
    invoice_date: date
    due_date: date
    line_items: tuple[LineItem, ...]
    subtotal: Decimal
    tax_rate: Decimal
    tax: Decimal
    total: Decimal
    payment_note: str


def _money(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def _unit_price(rng: random.Random) -> Decimal:
    # Mix of cheap goods and expensive services, roughly log-distributed.
    bracket = rng.random()
    if bracket < 0.25:
        return _money(rng.randint(450, 4_000))  # $4.50-$40
    if bracket < 0.75:
        return _money(rng.randint(4_000, 40_000))  # $40-$400
    return _money(rng.randint(40_000, 250_000))  # $400-$2,500


def _line_items(rng: random.Random, layout: str) -> tuple[LineItem, ...]:
    # The modern layout is the designated multi-row stress case.
    counts = {"classic": (1, 5), "modern": (6, 12), "euro": (2, 7)}
    n = rng.randint(*counts[layout])
    pool = DE_ITEMS if layout == "euro" else EN_ITEMS
    months = DE_MONTHS if layout == "euro" else EN_MONTHS
    descriptions = rng.sample(pool, k=min(n, len(pool)))
    items = []
    for i in range(n):
        description = descriptions[i % len(descriptions)]
        if rng.random() < 0.3:
            description += f" — {rng.choice(months)}"
        quantity = rng.randint(1, 6) if rng.random() < 0.8 else rng.randint(7, 12)
        unit_price = _unit_price(rng)
        amount = (quantity * unit_price).quantize(CENT)
        items.append(LineItem(description, quantity, unit_price, amount))
    return tuple(items)


def _us_party(fake: Faker, with_contact: bool, rng: random.Random) -> Party:
    return Party(
        name=fake.company(),
        address_lines=(
            fake.street_address(),
            f"{fake.city()}, {fake.state_abbr()} {fake.zipcode()}",
        ),
        email=fake.company_email() if with_contact else None,
    )


def _de_party(fake: Faker, with_contact: bool, rng: random.Random) -> Party:
    return Party(
        name=fake.company(),
        address_lines=(fake.street_address(), f"{fake.postcode()} {fake.city()}"),
        vat_id=f"DE{rng.randint(100_000_000, 999_999_999)}",
        email=fake.company_email() if with_contact else None,
    )


def _invoice_number(rng: random.Random, layout: str, issued: date) -> str:
    if layout == "classic":
        return f"INV-{issued.year}-{rng.randint(1_000, 99_999):05d}"
    if layout == "modern":
        return f"{issued.year}{issued.month:02d}-{rng.randint(100, 9_999):04d}"
    return f"RE-{issued.year}/{rng.randint(100, 9_999):04d}"


def generate_invoice(index: int, rng: random.Random, fake_us: Faker, fake_de: Faker) -> Invoice:
    layout = rng.choice(LAYOUTS)
    scanned = rng.random() < SCAN_FRACTION

    invoice_date = REFERENCE_DATE - timedelta(days=rng.randint(0, 700))
    due_date = invoice_date + timedelta(days=rng.choice((14, 30, 45, 60)))

    if layout == "euro":
        currency = "EUR"
        vendor = _de_party(fake_de, with_contact=True, rng=rng)
        bill_to = _de_party(fake_de, with_contact=False, rng=rng)
        tax_rate = Decimal(rng.choice(EU_VAT_RATES))
        payment_note = f"IBAN: {fake_de.iban()}"
    else:
        currency = "USD"
        vendor = _us_party(fake_us, with_contact=True, rng=rng)
        bill_to = _us_party(fake_us, with_contact=False, rng=rng)
        tax_rate = Decimal(rng.choice(US_TAX_RATES))
        payment_note = f"Please reference invoice number. Checks payable to {vendor.name}."

    line_items = _line_items(rng, layout)
    subtotal = sum((item.amount for item in line_items), Decimal("0")).quantize(CENT)
    tax = (subtotal * tax_rate).quantize(CENT, rounding=ROUND_HALF_UP)
    total = (subtotal + tax).quantize(CENT)

    return Invoice(
        doc_id=f"inv-{index:05d}",
        layout=layout,
        scanned=scanned,
        currency=currency,
        vendor=vendor,
        bill_to=bill_to,
        invoice_number=_invoice_number(rng, layout, invoice_date),
        invoice_date=invoice_date,
        due_date=due_date,
        line_items=line_items,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax=tax,
        total=total,
        payment_note=payment_note,
    )


def generate_corpus(count: int, seed: int) -> list[Invoice]:
    rng = random.Random(seed)
    fake_us = Faker("en_US")
    fake_de = Faker("de_DE")
    fake_us.seed_instance(rng.getrandbits(32))
    fake_de.seed_instance(rng.getrandbits(32))
    return [generate_invoice(i + 1, rng, fake_us, fake_de) for i in range(count)]


def ground_truth_record(invoice: Invoice, s3_key: str) -> dict:
    """Canonical labels: ISO dates, plain decimal strings, no currency symbols.

    The PDFs display localized formats (03/05/2025, 1.234,56 €); the eval
    harness normalizes extractions back to this canonical form before comparing.
    """
    return {
        "doc_id": invoice.doc_id,
        "file": f"{invoice.doc_id}.pdf",
        "layout": invoice.layout,
        "scanned": invoice.scanned,
        "currency": invoice.currency,
        "s3_key": s3_key,
        "fields": {
            "vendor": invoice.vendor.name,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "subtotal": str(invoice.subtotal),
            "tax_rate": str(invoice.tax_rate),
            "tax": str(invoice.tax),
            "total": str(invoice.total),
            "line_items": [
                {
                    "description": item.description,
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price),
                    "amount": str(item.amount),
                }
                for item in invoice.line_items
            ],
        },
    }
