CREATE DATABASE shop CHARACTER SET utf8mb4;
USE shop;

CREATE TABLE customers (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(100) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE orders (
  id INT PRIMARY KEY AUTO_INCREMENT,
  customer_id INT NOT NULL,
  total DECIMAL(10, 2) NOT NULL,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
) ENGINE=InnoDB;

CREATE TABLE hist_sales (
  id INT PRIMARY KEY AUTO_INCREMENT,
  amount DECIMAL(10, 2) NOT NULL
) ENGINE=InnoDB;

INSERT INTO customers (name) VALUES ('Ana'), ('Luis'), ('Marta');
INSERT INTO orders (customer_id, total) VALUES (1, 10), (1, 20), (2, 30), (3, 40), (3, 50);
INSERT INTO hist_sales (amount) VALUES (1), (2), (3), (4), (5), (6), (7);

CREATE VIEW v_orders AS SELECT o.id, c.name, o.total FROM orders o JOIN customers c ON c.id = o.customer_id;

CREATE TRIGGER trg_orders_total BEFORE INSERT ON orders
FOR EACH ROW SET NEW.total = GREATEST(NEW.total, 0);

CREATE PROCEDURE count_orders() SELECT COUNT(*) FROM orders;

-- Backup user with a password full of option-file special characters: p#ss "w\rd"
CREATE USER 'guardian'@'%' IDENTIFIED BY 'p#ss "w\\rd"';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES ON shop.* TO 'guardian'@'%';
