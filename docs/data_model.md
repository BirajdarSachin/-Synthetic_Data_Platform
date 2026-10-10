# Data Model

Entity schemas are the platform data contracts (ADR-0007). Any change to a
schema below must follow the SCHEMA_VERSION rules in ADR-0007.

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

Generation rules:
- registration dates span generation.history (default '3y' = 1095 days)
  back from generation.reference_date.
- email uniqueness is enforced by faker.unique (per child seed).

Relationships: none yet. Phase 2 adds product, order, order_item and payment
with referential integrity to customer.
