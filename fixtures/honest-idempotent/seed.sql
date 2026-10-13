CREATE TABLE items (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    key TEXT NOT NULL,
    at TEXT NOT NULL,
    request_id TEXT NOT NULL
);
INSERT INTO items (key, value) VALUES ('a', '1'), ('b', '2');
