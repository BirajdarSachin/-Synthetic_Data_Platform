from datetime import date

import pytest

from src.generators.review import ReviewGenerator

REFERENCE_DATE = date(2026, 1, 15)


def test_review_pairs_are_unique_and_reviews_follow_latest_purchase():
    purchases = [
        (1, 10, date(2026, 1, 2)),
        (1, 10, date(2026, 1, 8)),
        (2, 10, date(2026, 1, 4)),
        (3, 11, REFERENCE_DATE),
    ]

    reviews = list(ReviewGenerator(17, REFERENCE_DATE, purchases).generate(2))

    reviewed_pairs = [(review.customer_id, review.product_id) for review in reviews]
    assert len(reviewed_pairs) == len(set(reviewed_pairs))
    assert [review.review_id for review in reviews] == [1, 2]
    assert all(review.customer_id in {1, 2, 3} for review in reviews)
    assert all(review.product_id in {10, 11} for review in reviews)
    assert all(1 <= review.rating <= 5 for review in reviews)
    latest_purchase = {(1, 10): date(2026, 1, 8), (2, 10): date(2026, 1, 4)}
    assert all(
        review.review_date > latest_purchase[(review.customer_id, review.product_id)]
        for review in reviews
    )
    assert (3, 11) not in reviewed_pairs


def test_review_generator_is_deterministic():
    purchases = [
        (1, 10, date(2026, 1, 2)),
        (2, 11, date(2026, 1, 4)),
    ]

    first = list(ReviewGenerator(17, REFERENCE_DATE, purchases).generate(2))
    second = list(ReviewGenerator(17, REFERENCE_DATE, purchases).generate(2))

    assert first == second


def test_review_generator_rejects_more_reviews_than_eligible_pairs():
    purchases = [
        (1, 10, date(2026, 1, 2)),
        (1, 10, date(2026, 1, 8)),
        (2, 10, date(2026, 1, 4)),
        (3, 11, REFERENCE_DATE),
    ]

    with pytest.raises(ValueError, match="eligible unique purchases \\(2\\)"):
        list(ReviewGenerator(17, REFERENCE_DATE, purchases).generate(3))


def test_review_generator_rejects_purchase_after_reference_date():
    with pytest.raises(ValueError, match="after reference date"):
        ReviewGenerator(
            17,
            REFERENCE_DATE,
            [(1, 10, date(2026, 1, 16))],
        )