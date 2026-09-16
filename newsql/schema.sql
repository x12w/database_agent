-- ============================================================
-- E-Commerce Test Database Schema
-- Tables: customers, categories, suppliers, products,
--         inventory, orders, order_items, payments, reviews
-- ============================================================

DROP DATABASE IF EXISTS ecommerce_db;
CREATE DATABASE ecommerce_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ecommerce_db;

-- ============================================================
-- 1. customers — 客户信息表
-- ============================================================
CREATE TABLE customers (
    id          INT             AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(100)    NOT NULL,
    email       VARCHAR(150)    NOT NULL UNIQUE,
    phone       VARCHAR(30)     DEFAULT NULL,
    city        VARCHAR(80)     DEFAULT NULL,
    country     VARCHAR(60)     DEFAULT 'China',
    vip_level   ENUM('bronze','silver','gold','platinum') DEFAULT 'bronze',
    created_at  DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_customers_city (city),
    INDEX idx_customers_country (country),
    INDEX idx_customers_vip (vip_level)
) ENGINE=InnoDB;

-- ============================================================
-- 2. categories — 商品分类表
-- ============================================================
CREATE TABLE categories (
    id          INT             AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(80)     NOT NULL,
    parent_id   INT             DEFAULT NULL,
    sort_order  INT             DEFAULT 0,
    created_at  DATETIME        DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL,
    INDEX idx_categories_parent (parent_id)
) ENGINE=InnoDB;

-- ============================================================
-- 3. suppliers — 供应商表
-- ============================================================
CREATE TABLE suppliers (
    id          INT             AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(120)    NOT NULL,
    contact     VARCHAR(100)    DEFAULT NULL,
    phone       VARCHAR(30)     DEFAULT NULL,
    email       VARCHAR(150)    DEFAULT NULL,
    city        VARCHAR(80)     DEFAULT NULL,
    rating      DECIMAL(2,1)   DEFAULT 0.0 CHECK (rating >= 0 AND rating <= 5),
    active      TINYINT(1)     DEFAULT 1,
    created_at  DATETIME        DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ============================================================
-- 4. products — 商品表
-- ============================================================
CREATE TABLE products (
    id              INT             AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200)    NOT NULL,
    description     TEXT            DEFAULT NULL,
    category_id     INT             DEFAULT NULL,
    supplier_id     INT             DEFAULT NULL,
    sku             VARCHAR(60)     NOT NULL UNIQUE,
    unit_price      DECIMAL(12,2)   NOT NULL CHECK (unit_price >= 0),
    cost_price      DECIMAL(12,2)   DEFAULT NULL CHECK (cost_price >= 0),
    weight_kg       DECIMAL(8,3)   DEFAULT NULL,
    status          ENUM('draft','active','discontinued') DEFAULT 'draft',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL,
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL,
    INDEX idx_products_category (category_id),
    INDEX idx_products_supplier (supplier_id),
    INDEX idx_products_status (status),
    INDEX idx_products_price (unit_price)
) ENGINE=InnoDB;

-- ============================================================
-- 5. inventory — 库存表
-- ============================================================
CREATE TABLE inventory (
    id              INT             AUTO_INCREMENT PRIMARY KEY,
    product_id      INT             NOT NULL,
    warehouse       VARCHAR(60)     NOT NULL DEFAULT 'main',
    quantity        INT             NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    safety_stock    INT             NOT NULL DEFAULT 10,
    last_restock_at DATETIME        DEFAULT NULL,
    updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    UNIQUE KEY uk_product_warehouse (product_id, warehouse),
    INDEX idx_inventory_low_stock (quantity, safety_stock)
) ENGINE=InnoDB;

-- ============================================================
-- 6. orders — 订单表
-- ============================================================
CREATE TABLE orders (
    id              INT             AUTO_INCREMENT PRIMARY KEY,
    order_no        VARCHAR(32)     NOT NULL UNIQUE,
    customer_id     INT             NOT NULL,
    total_amount    DECIMAL(14,2)   NOT NULL DEFAULT 0.00,
    discount_amount DECIMAL(14,2)   NOT NULL DEFAULT 0.00,
    final_amount    DECIMAL(14,2)   NOT NULL DEFAULT 0.00,
    status          ENUM('pending','confirmed','shipped','delivered','cancelled','refunded')
                                    DEFAULT 'pending',
    shipping_addr   TEXT            DEFAULT NULL,
    notes           TEXT            DEFAULT NULL,
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT,
    INDEX idx_orders_customer (customer_id),
    INDEX idx_orders_status (status),
    INDEX idx_orders_date (created_at),
    INDEX idx_orders_order_no (order_no)
) ENGINE=InnoDB;

-- ============================================================
-- 7. order_items — 订单明细表
-- ============================================================
CREATE TABLE order_items (
    id              INT             AUTO_INCREMENT PRIMARY KEY,
    order_id        INT             NOT NULL,
    product_id      INT             NOT NULL,
    quantity        INT             NOT NULL CHECK (quantity > 0),
    unit_price      DECIMAL(12,2)   NOT NULL,
    subtotal        DECIMAL(14,2)   GENERATED ALWAYS AS (quantity * unit_price) STORED,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT,
    INDEX idx_items_order (order_id),
    INDEX idx_items_product (product_id)
) ENGINE=InnoDB;

-- ============================================================
-- 8. payments — 支付记录表
-- ============================================================
CREATE TABLE payments (
    id              INT             AUTO_INCREMENT PRIMARY KEY,
    order_id        INT             NOT NULL,
    amount          DECIMAL(14,2)   NOT NULL CHECK (amount > 0),
    method          ENUM('wechat','alipay','credit_card','bank_transfer','cash')
                                    NOT NULL,
    transaction_id  VARCHAR(100)    DEFAULT NULL,
    status          ENUM('pending','success','failed','refunded') DEFAULT 'pending',
    paid_at         DATETIME        DEFAULT NULL,
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE RESTRICT,
    INDEX idx_payments_order (order_id),
    INDEX idx_payments_status (status),
    INDEX idx_payments_date (paid_at)
) ENGINE=InnoDB;

-- ============================================================
-- 9. reviews — 商品评价表
-- ============================================================
CREATE TABLE reviews (
    id              INT             AUTO_INCREMENT PRIMARY KEY,
    product_id      INT             NOT NULL,
    customer_id     INT             NOT NULL,
    order_id        INT             DEFAULT NULL,
    rating          TINYINT         NOT NULL CHECK (rating >= 1 AND rating <= 5),
    title           VARCHAR(200)    DEFAULT NULL,
    content         TEXT            DEFAULT NULL,
    is_verified     TINYINT(1)     DEFAULT 0,
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL,
    UNIQUE KEY uk_customer_product (customer_id, product_id),
    INDEX idx_reviews_product (product_id),
    INDEX idx_reviews_rating (rating)
) ENGINE=InnoDB;
