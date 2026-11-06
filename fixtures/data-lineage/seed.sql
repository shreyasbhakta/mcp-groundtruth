-- {marker:ssn} is replaced by a fresh synthetic token on every run.
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, ssn TEXT NOT NULL);
CREATE TABLE drafts (customer_id INTEGER PRIMARY KEY, ssn TEXT NOT NULL);
CREATE TABLE reports (customer_id INTEGER PRIMARY KEY, line TEXT NOT NULL);
INSERT INTO customers (id, name, ssn) VALUES (1, 'Ada', '{marker:ssn}');
