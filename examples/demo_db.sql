-- Run in an existing PostgreSQL database, using a role allowed to create a schema.
-- Reruns preserve existing rows; no DROP, TRUNCATE, or DELETE operations are used.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE SCHEMA IF NOT EXISTS nlsql_demo;

CREATE TABLE IF NOT EXISTS nlsql_demo.customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    country TEXT NOT NULL,
    joined_on DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS nlsql_demo.orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES nlsql_demo.customers (customer_id),
    ordered_on DATE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'pending', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS nlsql_demo.order_items (
    order_id INTEGER NOT NULL REFERENCES nlsql_demo.orders (order_id),
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(10, 2) NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (order_id, line_number)
);

COMMENT ON TABLE nlsql_demo.customers IS 'Fictional customers; includes one customer without orders.';
COMMENT ON TABLE nlsql_demo.orders IS 'Order headers. Only completed orders count toward revenue.';
COMMENT ON TABLE nlsql_demo.order_items IS 'Order lines. All prices are USD; line value is quantity * unit_price.';
COMMENT ON COLUMN nlsql_demo.order_items.unit_price IS 'Price per unit in USD, before multiplication by quantity.';

INSERT INTO nlsql_demo.customers (customer_id, customer_name, email, country, joined_on) VALUES
    (1, 'Avery Morgan', 'avery@example.com', 'United States', '2025-11-10'),
    (2, 'Lena Fischer', 'lena@example.com', 'Germany', '2025-11-15'),
    (3, 'Jan Kowalski', 'jan@example.com', 'Poland', '2025-12-01'),
    (4, 'Oliver Taylor', 'oliver@example.com', 'United Kingdom', '2025-12-12'),
    (5, 'Jordan Lee', 'jordan@example.com', 'United States', '2026-01-20'),
    (6, 'Sofia Rossi', 'sofia@example.com', 'Italy', '2026-02-01')
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO nlsql_demo.orders (order_id, customer_id, ordered_on, status) VALUES
    (1001, 1, '2026-01-05', 'completed'),
    (1002, 2, '2026-01-10', 'completed'),
    (1003, 3, '2026-01-18', 'completed'),
    (1004, 1, '2026-02-02', 'completed'),
    (1005, 4, '2026-02-12', 'cancelled'),
    (1006, 2, '2026-02-20', 'completed'),
    (1007, 5, '2026-03-03', 'completed'),
    (1008, 3, '2026-03-15', 'pending'),
    (1009, 4, '2026-03-21', 'completed'),
    (1010, 1, '2026-04-02', 'completed'),
    (1011, 5, '2026-04-08', 'pending'),
    (1012, 2, '2026-04-15', 'cancelled')
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO nlsql_demo.order_items
    (order_id, line_number, product_name, category, quantity, unit_price) VALUES
    (1001, 1, 'Keyboard', 'Electronics', 1, 80.00),
    (1001, 2, 'Mouse', 'Electronics', 2, 25.00),
    (1002, 1, 'Monitor', 'Electronics', 2, 250.00),
    (1002, 2, 'USB-C cable', 'Accessories', 2, 10.00),
    (1003, 1, 'Laptop', 'Electronics', 1, 1200.00),
    (1003, 2, 'Laptop bag', 'Accessories', 1, 60.00),
    (1004, 1, 'Dock', 'Accessories', 1, 150.00),
    (1004, 2, 'USB-C cable', 'Accessories', 3, 10.00),
    (1005, 1, 'Monitor', 'Electronics', 1, 250.00),
    (1005, 2, 'Mouse', 'Electronics', 1, 25.00),
    (1006, 1, 'Keyboard', 'Electronics', 2, 80.00),
    (1006, 2, 'Mouse', 'Electronics', 2, 25.00),
    (1007, 1, 'Laptop', 'Electronics', 2, 1200.00),
    (1007, 2, 'Dock', 'Accessories', 2, 150.00),
    (1008, 1, 'Monitor', 'Electronics', 1, 250.00),
    (1008, 2, 'USB-C cable', 'Accessories', 1, 10.00),
    (1009, 1, 'Monitor', 'Electronics', 1, 250.00),
    (1009, 2, 'Keyboard', 'Electronics', 1, 80.00),
    (1010, 1, 'Mouse', 'Electronics', 4, 25.00),
    (1010, 2, 'Keyboard', 'Electronics', 1, 80.00),
    (1011, 1, 'Laptop', 'Electronics', 1, 1200.00),
    (1011, 2, 'Laptop bag', 'Accessories', 1, 60.00),
    (1012, 1, 'Dock', 'Accessories', 1, 150.00),
    (1012, 2, 'USB-C cable', 'Accessories', 2, 10.00)
ON CONFLICT (order_id, line_number) DO NOTHING;

COMMIT;

-- Fresh installation: 6 customers, 12 orders, 24 lines, completed revenue USD 5510.00.
SELECT
    (SELECT COUNT(*) FROM nlsql_demo.customers) AS customers,
    (SELECT COUNT(*) FROM nlsql_demo.orders) AS orders,
    (SELECT COUNT(*) FROM nlsql_demo.order_items) AS order_items;

SELECT SUM(i.quantity * i.unit_price) AS completed_revenue_usd
FROM nlsql_demo.orders AS o
JOIN nlsql_demo.order_items AS i ON i.order_id = o.order_id
WHERE o.status = 'completed';
