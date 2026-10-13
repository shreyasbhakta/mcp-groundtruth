CREATE TABLE items (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE lookup_log (key TEXT NOT NULL, at TEXT NOT NULL);
INSERT INTO items (key, value) VALUES ('a', '1'), ('b', '2');
