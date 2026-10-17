-- Target Application Database Schema
-- Separate from SurveyOps internal databases

CREATE TABLE IF NOT EXISTS inventory (
    item_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    stock INT NOT NULL CHECK (stock >= 0),
    price NUMERIC(10, 2) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    status VARCHAR(32) NOT NULL DEFAULT 'succeeded',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    item_id VARCHAR(64) NOT NULL,
    quantity INT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'confirmed',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Seed initial inventory
INSERT INTO inventory (item_id, name, stock, price)
VALUES 
    ('item-101', 'Wireless Ergonomic Keyboard', 100000, 49.99),
    ('item-102', 'Precision Gaming Mouse', 100000, 29.99),
    ('item-103', '4K Ultra-Wide Monitor 34"', 50000, 399.99),
    ('item-104', 'Noise-Cancelling Headphones', 50000, 149.99)
ON CONFLICT (item_id) DO NOTHING;
