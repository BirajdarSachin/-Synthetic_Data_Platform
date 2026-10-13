"""Milestone 3b tests: multi-parent FKs and business rules (ADR-0015/0016)."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal

import pytest

from src.generators.customer import CustomerGenerator
from src.generators.employee import EmployeeGenerator
from src.generators.order import OrderGenerator
from src.generators.order_item import OrderItemGenerator
from src.generators.payment import PaymentGenerator
from src.generators.product import ProductGenerator
from src.generators.review import ReviewGenerator
from src.models.order import ALLOWED_STATUSES

REF = date(2026, 1, 15)
HISTORY = 365
N_CUSTOMERS, N_PRODUCTS, N_EMPLOYEES = 30, 40, 6
N_ORDERS, N_ITEMS, N_PAYMENTS = 50, 120, 10


def _build_domain():
    """Build one small consistent domain, mirroring main.py wiring."""
    customers = list(CustomerGenerator(
        seed=1, reference_date=REF, history_days=HISTORY).generate(N_CUSTOMERS))
    products = list(ProductGenerator(seed=2, supplier_count=5).generate(N_PRODUCTS))
    employees = list(EmployeeGenerator(
        seed=3, reference_date=REF, history_days=HISTORY).generate(N_EMPLOYEES))
    orders = list(OrderGenerator(
        seed=4, reference_date=REF,
        customers=[(c.customer_id, c.registration_date) for c in customers],
        employee_count=N_EMPLOYEES).generate(N_ORDERS))
    items = list(OrderItemGenerator(
        seed=5, order_count=N_ORDERS,
        products=[(p.product_id, p.unit_price) for p in products]).generate(N_ITEMS))
    completed = [(o.order_id, o.order_date) for o in orders if o.status == "completed"]
    totals: dict[int, Decimal] = {}
    for it in items:
        totals[it.order_id] = (
            totals.get(it.order_id, Decimal("0.00")) + it.quantity * it.unit_price
        )
    payments = list(PaymentGenerator(
        seed=6, reference_date=REF, completed_orders=completed,
        totals=totals).generate(N_PAYMENTS))
    return customers, products, employees, orders, items, payments


@pytest.fixture(scope="module")
def domain():
    return _build_domain()


def test_employee_deterministic_and_sequential():
    a = list(EmployeeGenerator(
        seed=3, reference_date=REF, history_days=HISTORY).generate(10))
    b = list(EmployeeGenerator(
        seed=3, reference_date=REF, history_days=HISTORY).generate(10))
    # Frozen dataclasses have VALUE equality: a == b compares field by field,
    # so no asdict() is needed for the comparison.
    assert a == b
    # a holds real Employee objects - attribute access works.
    assert [e.employee_id for e in a] == list(range(1, 11))


def test_employee_hire_date_within_history(domain):
    _, _, employees, _, _, _ = domain
    earliest = REF.toordinal() - HISTORY
    assert all(earliest <= e.hire_date.toordinal() <= REF.toordinal() for e in employees)


def test_order_deterministic(domain):
    _, _, _, orders, _, _ = domain
    again = list(OrderGenerator(
        seed=4, reference_date=REF,
        customers=[(c.customer_id, c.registration_date) for c in _build_domain()[0]],
        employee_count=N_EMPLOYEES).generate(N_ORDERS))
    assert [asdict(o) for o in orders] == [asdict(o) for o in again]


def test_order_customer_fk_valid(domain):
    _, _, _, orders, _, _ = domain
    valid = set(range(1, N_CUSTOMERS + 1))
    assert all(o.customer_id in valid for o in orders)


def test_order_employee_fk_valid(domain):
    _, _, _, orders, _, _ = domain
    valid = set(range(1, N_EMPLOYEES + 1))
    assert all(o.sales_rep_employee_id in valid for o in orders)


def test_order_status_in_allowed_set(domain):
    _, _, _, orders, _, _ = domain
    assert all(o.status in ALLOWED_STATUSES for o in orders)


def test_order_date_not_before_customer_registration(domain):
    """THE temporal business rule: orders never precede registration (ADR-0016)."""
    customers, _, _, orders, _, _ = domain
    reg = {c.customer_id: c.registration_date for c in customers}
    assert all(o.order_date >= reg[o.customer_id] for o in orders)


def test_every_order_has_at_least_one_item(domain):
    _, _, _, orders, items, _ = domain
    orders_with_items = {it.order_id for it in items}
    assert all(o.order_id in orders_with_items for o in orders)


def test_order_item_fks_valid(domain):
    _, _, _, orders, items, _ = domain
    valid_orders = {o.order_id for o in orders}
    valid_products = set(range(1, N_PRODUCTS + 1))
    assert all(it.order_id in valid_orders for it in items)
    assert all(it.product_id in valid_products for it in items)


def test_order_item_price_matches_product(domain):
    _, products, _, _, items, _ = domain
    price = {p.product_id: p.unit_price for p in products}
    assert all(it.unit_price == price[it.product_id] for it in items)


def test_order_item_quantity_in_range(domain):
    _, _, _, _, items, _ = domain
    assert all(1 <= it.quantity <= 5 for it in items)


def test_reviews_only_cover_unique_completed_purchases_and_follow_purchase_date(domain):
    _, _, _, orders, items, _ = domain
    completed_customer_by_order = {
        order.order_id: (order.customer_id, order.order_date)
        for order in orders
        if order.status == "completed"
    }
    purchases = [
        (customer_id, item.product_id, order_date)
        for item in items
        if item.order_id in completed_customer_by_order
        for customer_id, order_date in [completed_customer_by_order[item.order_id]]
    ]
    latest_purchase_by_pair = {}
    for customer_id, product_id, purchase_date in purchases:
        pair = (customer_id, product_id)
        latest_purchase_by_pair[pair] = max(
            purchase_date, latest_purchase_by_pair.get(pair, purchase_date)
        )
    eligible_pairs = {
        pair for pair, purchase_date in latest_purchase_by_pair.items()
        if purchase_date < REF
    }

    reviews = list(ReviewGenerator(
        seed=7, reference_date=REF, purchases=purchases
    ).generate(len(eligible_pairs)))

    reviewed_pairs = [(review.customer_id, review.product_id) for review in reviews]
    assert set(reviewed_pairs) == eligible_pairs
    assert len(reviewed_pairs) == len(set(reviewed_pairs))
    assert all(
        review.review_date > latest_purchase_by_pair[
            (review.customer_id, review.product_id)
        ]
        for review in reviews
    )
    assert all(1 <= review.rating <= 5 for review in reviews)


def test_payment_only_for_completed_orders(domain):
    _, _, _, orders, _, payments = domain
    completed = {o.order_id for o in orders if o.status == "completed"}
    assert all(p.order_id in completed for p in payments)


def test_payment_one_per_order(domain):
    _, _, _, _, _, payments = domain
    ids = [p.order_id for p in payments]
    assert len(ids) == len(set(ids))


def test_payment_date_not_before_order_date(domain):
    _, _, _, orders, _, payments = domain
    od = {o.order_id: o.order_date for o in orders}
    assert all(p.payment_date >= od[p.order_id] for p in payments)


def test_payment_amount_equals_order_total(domain):
    _, _, _, _, items, payments = domain
    totals: dict[int, Decimal] = {}
    for it in items:
        totals[it.order_id] = (
            totals.get(it.order_id, Decimal("0.00")) + it.quantity * it.unit_price
        )
    assert all(p.amount == totals[p.order_id] for p in payments)


def test_payment_generator_rejects_excess_count():
    completed = [(1, date(2025, 1, 1))]
    gen = PaymentGenerator(seed=1, reference_date=REF,
                           completed_orders=completed, totals={1: Decimal("10.00")})
    with pytest.raises(ValueError):
        list(gen.generate(2))
