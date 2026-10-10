# Data Model

Entity schemas are the platform data contracts (ADR-0007). Any change must
follow the SCHEMA_VERSION rules in ADR-0007.

Generation order follows FK dependencies (ADR-0014):
customer, supplier, employee -> product, order -> order_item -> payment.

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

## Relationship map
supplier -> product -> order_item <- order -> payment
customer -> order <- employee (sales rep)

Note: all records land in one file per entity today; per-window partitioned
output arrives in Phase 5.
