"""Persistent local state. One engine owns the database at a time."""
import sqlite3
from pathlib import Path
from uuid import uuid4


class Database:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA journal_mode=WAL')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version > 4:
            raise RuntimeError('Database was created by a newer application')
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS roots(
          id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,name TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'pending',
          patterns TEXT NOT NULL DEFAULT '[]',ignore_case INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY,root_id TEXT NOT NULL REFERENCES roots(id),path TEXT NOT NULL,
          fingerprint TEXT NOT NULL,observed_at REAL NOT NULL,remote_fingerprint TEXT,
          state TEXT NOT NULL DEFAULT 'active',deleted_at REAL,UNIQUE(root_id,path));
        CREATE TABLE IF NOT EXISTS jobs(
          file_id TEXT PRIMARY KEY REFERENCES files(id),operation TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,error TEXT,error_category TEXT);

        ''')
        if version < 2:
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(files)')}
            for column in ('device', 'inode'):
                if column not in columns:
                    self.connection.execute(f'ALTER TABLE files ADD COLUMN {column} INTEGER')
            self.connection.execute('PRAGMA user_version=2')
            self.connection.commit()
        if version < 3:
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(jobs)')}
            if 'error_category' not in columns:
                self.connection.execute('ALTER TABLE jobs ADD COLUMN error_category TEXT')
            self.connection.execute('PRAGMA user_version=3')
            self.connection.commit()
        if version < 4:
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(roots)')}
            if 'selection_partition' not in columns:
                self.connection.execute('ALTER TABLE roots ADD COLUMN selection_partition INTEGER NOT NULL DEFAULT 0')
            self.connection.execute('UPDATE roots SET selection_partition=1 WHERE enabled=1')
            self.connection.execute('PRAGMA user_version=4')
            self.connection.commit()
        self.path = path.absolute()
        path.chmod(0o600)
        if self.get('computer_id') is None:
            self.set('computer_id', str(uuid4()))

    def get(self, key, default=None):
        row = self.connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, str(value)))

    def close(self):
        self.connection.close()
