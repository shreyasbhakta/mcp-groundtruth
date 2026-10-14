CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE boot_log (at TEXT NOT NULL);
INSERT INTO settings (key, value) VALUES ('mode', 'normal');
