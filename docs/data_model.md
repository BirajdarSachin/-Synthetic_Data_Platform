# Data Model

Entity schemas are the platform data contracts (ADR-0007). Any change to a
schema below must follow the SCHEMA_VERSION rules in ADR-0007.

Generation order follows FK dependencies (ADR-0014):
customer, supplier (independent) -> product (needs supplier).

## customer - schema v1.0

One row = one registered customer. Primary key: customer_id.

| Column            | Type | Rules                                                                   |
|-------------------|------|-------------------------------------------------------------------------|
| customer_id       | int  | Sequential, starts at 1, unique (ADR-0001)                              |
| first_name        | str  | Non-empty                                                               |
| last_name         | str  | Non-empty                                                               |
| email             | str  | Unique across the dataset                                               |
| phone             | str  | Faker-generated phone format                                            |
| city              | str  | Faker-generated city                                                    |
| country           | str  | Faker-generated country                                                 |
| registration_date | date | ISO-8601; within [reference_date - history, reference_date] (ADR-0012)  |

## supplier - schema v1.0

One row = one supplier. Primary key: supplier_id. Parent of product.

| Column         | Type | Rules                        |
|----------------|------|------------------------------|
| supplier_id    | int  | Sequential, starts at 1      |
| supplier_name  | str  | Company name                 |
| contact_name   | str  | Person name                  |
| contact_email  | str  | Company-style email          |
| phone          | str  | Faker-generated phone format |
| city           | str  | Faker-generated city         |
| country        | str  | Faker-generated country      |

## product - schema v1.0

One row = one product. Primary key: product_id. FK: supplier_id.

| Column       | Type    | Rules                                                        |
|--------------|---------|--------------------------------------------------------------|
| product_id   | int     | Sequential, starts at 1                                      |
| product_name | str     | Two-word descriptive name                                    |
| category     | str     | One of 7 fixed categories (see product generator)            |
| unit_price   | decimal | > 0, exactly 2 decimal places; CSV as string "1234.56" (ADR-0002) |
| supplier_id  | int FK  | Must be an existing supplier_id: 1..supplier.records (ADR-0014) |

Relationships:
- product.supplier_id -> supplier.supplier_id (many products per supplier)

Integrity rule: changing datasets.supplier.records changes which supplier a
product gets - that is a config change, not a bug; the manifest records it.

Planned (Milestone 3b): employee, order (FK customer + employee),
order_item (FK order + product), payment (FK order).
