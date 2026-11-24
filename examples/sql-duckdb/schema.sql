CREATE TABLE customers (
    id      INTEGER PRIMARY KEY,
    name    VARCHAR NOT NULL,
    country VARCHAR NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    amount      DECIMAL(10, 2) NOT NULL,
    status      VARCHAR NOT NULL,
    ordered_at  DATE NOT NULL
);
