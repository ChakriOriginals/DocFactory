"""Synthetic purchase-order corpus — data layer.

The second document type, and the point of it: a purchase order is not a
renamed invoice. It carries a PO number and an order/delivery date pair rather
than an invoice number and a due date, it is issued by the *buyer* rather than
the vendor, and its arithmetic is its own — line amounts sum to a subtotal,
subtotal plus freight reconciles to the order total, with no tax rate anywhere.
Everything the pipeline needs to know about those differences is declared in
config/pipelines/purchase_order_v1.json; nothing here is special-cased in the
extraction, scoring or routing code.

Same determinism contract as the invoice corpus: one master seed, one fixed
reference date, internally consistent by construction — each line amount is
quantity x unit_price to the cent, and the totals reconcile exactly.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from faker import Faker
from invoices import (
    CENT,
    REFERENCE_DATE,
    LineItem,
    Party,
    _de_party,
    _money,
    _unit_price,
    _us_party,
)

LAYOUTS = ("standard", "euro")
SCAN_FRACTION = 0.25
TEMPLATE_VAR = "po"

# Printed on the euro layout ("Lieferung innerhalb von 14 Tagen"), which pins
# the order-to-delivery interval the same way an invoice's payment term pins
# invoice-to-due. The standard layout states no term, so nothing is inferred.
DELIVERY_TERMS = (7, 14, 21, 30)
FREIGHT_TIERS = ("0", "24.50", "48.00", "95.00", "180.00")

# Goods rather than services: a purchase order buys things.
EN_GOODS = (
    "USB-C docking station",
    'Monitor, 27" 4K',
    "Ergonomic task chair",
    "Standing desk frame",
    "Laser printer toner (black)",
    "Network switch, 24-port",
    "Rack shelf, 1U",
    "Cat6 patch cable, 3m",
    "Wireless keyboard and mouse set",
    "Conference speakerphone",
    "Label printer",
    "Warehouse pallet rack beam",
    "Safety gloves (box of 50)",
    "Cleanroom wipes (case)",
    "Forklift battery charger",
    "Steel shelving unit",
    "Barcode scanner, handheld",
    "Thermal receipt paper (carton)",
    "First-aid cabinet, wall-mounted",
    "LED high-bay luminaire",
)

DE_GOODS = (
    "USB-C Dockingstation",
    'Monitor, 27" 4K',
    "Bürodrehstuhl ergonomisch",
    "Steh-Sitz-Tischgestell",
    "Tonerkartusche schwarz",
    "Netzwerk-Switch, 24 Ports",
    "Rackboden, 1HE",
    "Patchkabel Cat6, 3m",
    "Funktastatur-Set",
    "Konferenz-Freisprecheinrichtung",
    "Etikettendrucker",
    "Palettenregal-Traverse",
    "Schutzhandschuhe (50 Stück)",
    "Reinraumtücher (Karton)",
    "Ladegerät für Staplerbatterie",
    "Stahlregal",
    "Handscanner",
    "Thermopapier (Karton)",
    "Verbandschrank",
    "LED-Hallenstrahler",
)


@dataclass(frozen=True)
class PurchaseOrder:
    doc_id: str
    layout: str
    scanned: bool
    currency: str
    buyer: Party
    vendor: Party
    ship_to: tuple[str, ...]
    po_number: str
    order_date: date
    delivery_date: date
    delivery_days: int | None
    line_items: tuple[LineItem, ...]
    subtotal: Decimal
    shipping: Decimal
    total: Decimal
    note: str


def _line_items(rng: random.Random, layout: str) -> tuple[LineItem, ...]:
    n = rng.randint(2, 8)
    pool = DE_GOODS if layout == "euro" else EN_GOODS
    descriptions = rng.sample(pool, k=min(n, len(pool)))
    items = []
    for index in range(n):
        quantity = rng.randint(1, 9) if rng.random() < 0.75 else rng.randint(10, 40)
        unit_price = _unit_price(rng)
        items.append(
            LineItem(
                description=descriptions[index % len(descriptions)],
                quantity=quantity,
                unit_price=unit_price,
                amount=(quantity * unit_price).quantize(CENT),
            )
        )
    return tuple(items)


def _po_number(rng: random.Random, layout: str, ordered: date) -> str:
    if layout == "euro":
        return f"BA-{ordered.year}/{rng.randint(1_000, 9_999):04d}"
    return f"PO-{ordered.year}-{rng.randint(1_000, 99_999):05d}"


def generate_purchase_order(
    index: int, rng: random.Random, fake_us: Faker, fake_de: Faker
) -> PurchaseOrder:
    layout = rng.choice(LAYOUTS)
    scanned = rng.random() < SCAN_FRACTION
    order_date = REFERENCE_DATE - timedelta(days=rng.randint(0, 700))

    if layout == "euro":
        currency = "EUR"
        # The buyer issues the order; the vendor is the supplier it goes to.
        buyer = _de_party(fake_de, with_contact=True, rng=rng)
        vendor = _de_party(fake_de, with_contact=False, rng=rng)
        delivery_days = rng.choice(DELIVERY_TERMS)
        note = "Bitte Bestellnummer auf allen Lieferpapieren angeben."
    else:
        currency = "USD"
        buyer = _us_party(fake_us, with_contact=True, rng=rng)
        vendor = _us_party(fake_us, with_contact=False, rng=rng)
        delivery_days = None
        note = "Reference this PO number on all packing slips and invoices."

    if delivery_days is None:
        delivery_date = order_date + timedelta(days=rng.randint(5, 45))
    else:
        # The euro layout prints the term, so the printed interval must be it.
        delivery_date = order_date + timedelta(days=delivery_days)

    line_items = _line_items(rng, layout)
    subtotal = sum((item.amount for item in line_items), Decimal("0")).quantize(CENT)
    shipping = _money(int(Decimal(rng.choice(FREIGHT_TIERS)) * 100))
    total = (subtotal + shipping).quantize(CENT)

    return PurchaseOrder(
        doc_id=f"po-{index:05d}",
        layout=layout,
        scanned=scanned,
        currency=currency,
        buyer=buyer,
        vendor=vendor,
        ship_to=buyer.address_lines,
        po_number=_po_number(rng, layout, order_date),
        order_date=order_date,
        delivery_date=delivery_date,
        delivery_days=delivery_days,
        line_items=line_items,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
        note=note,
    )


def generate_corpus(count: int, seed: int) -> list[PurchaseOrder]:
    # Seeded independently of the invoice corpus so neither moves the other.
    rng = random.Random(f"purchase_order:{seed}")
    fake_us = Faker("en_US")
    fake_de = Faker("de_DE")
    fake_us.seed_instance(rng.getrandbits(32))
    fake_de.seed_instance(rng.getrandbits(32))
    return [generate_purchase_order(i + 1, rng, fake_us, fake_de) for i in range(count)]


def template_for(order: PurchaseOrder) -> str:
    return f"po_{order.layout}.html.j2"


def render_context(order: PurchaseOrder) -> dict:
    return {TEMPLATE_VAR: order}


def ground_truth_record(order: PurchaseOrder, s3_key: str) -> dict:
    """Canonical labels: ISO dates, plain decimal strings, no currency symbols."""
    return {
        "doc_id": order.doc_id,
        "file": f"{order.doc_id}.pdf",
        "doc_type": "purchase_order",
        "layout": order.layout,
        "scanned": order.scanned,
        "s3_key": s3_key,
        "fields": {
            "buyer": order.buyer.name,
            "vendor": order.vendor.name,
            "po_number": order.po_number,
            "order_date": order.order_date.isoformat(),
            "delivery_date": order.delivery_date.isoformat(),
            "currency": order.currency,
            "subtotal": str(order.subtotal),
            "shipping": str(order.shipping),
            "total": str(order.total),
            "line_items": [
                {
                    "description": item.description,
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price),
                    "amount": str(item.amount),
                }
                for item in order.line_items
            ],
        },
    }
