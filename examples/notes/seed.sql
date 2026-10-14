-- The state every session starts from. Same schema as the server creates.
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE activity (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    note_id INTEGER,
    at TEXT NOT NULL
);
INSERT INTO notes (title, body, created_at) VALUES
    ('Groceries', 'Milk, eggs', '2026-01-01T09:00:00+00:00'),
    ('Ideas', 'A harness for MCP annotations', '2026-01-02T09:00:00+00:00');
