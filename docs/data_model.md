# Data Model

Entity schemas are the platform data contracts (ADR-0007). Any change must
follow the SCHEMA_VERSION rules in ADR-0007.

Generation order follows FK dependencies (ADR-0014):
customer, supplier, employee -> product, order -> order_item -> review, payment.

## customer - schema v1.0
PK customer_id (sequential from 1). Columns: first_name, last_name, email
(unique), phone, city, country, registration_date (within history window,
ADR-0012). Parent of order.

## supplier - schema v1.0
PK supplier_id. Columns: supplier_name, contact_name, contact_email, phone,
city, country. Parent of product.

## product - schema v1.0
PK product_id. FK supplier_id -> supplier (1..supplier.records, ADR-0014).
Columns: product_name, category (7 fixed values), unit_price
(Decimal > 0, 2dp; CSV "1234.56", ADR-0002). Parent of order_item.

## employee - schema v1.0
PK employee_id. Columns: first_name, last_name, email (unique), job_title
(6 fixed sales titles), hire_date (within history window), city, country.
Parent of order (sales rep).

## order - schema v1.0
PK order_id. FKs: customer_id -> customer, sales_rep_employee_id -> employee.
Columns: order_date, status in {completed, pending, cancelled}
(~70/20/10 deterministic split).
Business rules (ADR-0016): order_date >= that customer's registration_date
(ADR-0015 context). Parent of order_item and payment.

## order_item - schema v1.0
PK order_item_id. FKs: order_id -> order, product_id -> product.
Columns: quantity (1..5), unit_price (= referenced product's unit_price).
Business rules: every order has >= 1 item (order_item.records >=
order.records enforced in main.py).

## payment - schema v1.0
PK payment_id. FK order_id -> order. Columns: payment_date, amount
(Decimal, 2dp), payment_method in {credit_card, bank_transfer, paypal, cash}.
Business rules: at most one payment per order; completed orders only;
payment_date >= order_date; amount = sum(quantity * unit_price) of the
order's items.

## review - schema v1.0
PK review_id. FKs: customer_id -> customer, product_id -> product. Columns:
rating (1..5), review_date, review_text.
Business rules: reviews are only for products purchased by that customer in a
completed order; at most one review per customer/product pair. If the pair
has multiple purchases, the latest purchase is the anchor: a review must follow
the customer's most recent experience with that product, rather than an
earlier purchase. review_date is strictly after the anchor date (purchase date
+ 1 day minimum) and no later than the reference date. A pair whose latest
purchase is on the reference date is ineligible because no valid review date
can be both after that purchase and on or before the reference date. Therefore,
a prior purchase does not make a pair eligible if it was repurchased on the
reference date.

## Relationship map
supplier -> product -> order_item <- order -> payment, review
customer -> order <- employee (sales rep)

Note: all records land in one file per entity today; per-window partitioned
output arrives in Phase 5.
