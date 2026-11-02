from pathlib import Path
import sqlite3
import tempfile
import unittest
from files2gdrive.database import Database


class DatabaseMigrationTests(unittest.TestCase):
    def test_upgrade_legacy_jobs_preserves_data_and_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db'
            con=sqlite3.connect(path)
            con.executescript('''
                CREATE TABLE jobs(file_id TEXT PRIMARY KEY,operation TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,error TEXT);
                INSERT INTO jobs VALUES('file','upload',3,99,'offline');
                PRAGMA user_version=2;
            '''); con.close()
            db=Database(path)
            try:
                row=db.connection.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['attempts'],3); self.assertEqual(row['error'],'offline')
                self.assertIsNone(row['error_category'])
                self.assertEqual(db.connection.execute('PRAGMA user_version').fetchone()[0],4)
            finally: db.close()


    def test_upgrade_v3_preserves_identities_deadlines_and_partition_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db'
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE roots(id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,name TEXT NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'pending',
                  patterns TEXT NOT NULL DEFAULT '[]',ignore_case INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE files(id TEXT PRIMARY KEY,root_id TEXT NOT NULL REFERENCES roots(id),path TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,observed_at REAL NOT NULL,remote_fingerprint TEXT,
                  state TEXT NOT NULL DEFAULT 'active',deleted_at REAL,device INTEGER,inode INTEGER,
                  UNIQUE(root_id,path));
                CREATE TABLE jobs(file_id TEXT PRIMARY KEY REFERENCES files(id),operation TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,error TEXT,error_category TEXT);
                CREATE TABLE drive_objects(object_key TEXT PRIMARY KEY,drive_id TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0);
                INSERT INTO settings VALUES('computer_id','original-computer');
                INSERT INTO settings VALUES('paused','1');
                INSERT INTO roots VALUES('active-root','/home/user/docs','Docs',1,'ready','["*.tmp"]',1);
                INSERT INTO roots VALUES('removed-root','/home/user/old','Old',0,'removed','[]',0);
                INSERT INTO files VALUES('active-file','active-root','file','[4,123,420]',10,'[4,123,420]','active',NULL,7,9);
                INSERT INTO files VALUES('retained-file','removed-root','old','[3,456,384]',20,'[3,456,384]','excluded',12345.5,7,10);
                INSERT INTO jobs VALUES('retained-file','retain',2,23456,'offline','unknown');
                INSERT INTO drive_objects VALUES('key','same-drive-id',1);
                PRAGMA user_version=3;
            """)
            before_roots = con.execute('SELECT * FROM roots ORDER BY id').fetchall()
            before_files = con.execute('SELECT * FROM files ORDER BY id').fetchall()
            before_jobs = con.execute('SELECT * FROM jobs').fetchall()
            before_objects = con.execute('SELECT * FROM drive_objects').fetchall()
            con.close()
            db = Database(path)
            try:
                roots = db.connection.execute('SELECT * FROM roots ORDER BY id').fetchall()
                self.assertEqual([tuple(row)[:-1] for row in roots], before_roots)
                self.assertEqual({row['id']: row['selection_partition'] for row in roots},
                                 {'active-root': 1, 'removed-root': 0})
                self.assertEqual([tuple(row) for row in db.connection.execute('SELECT * FROM files ORDER BY id')], before_files)
                self.assertEqual([tuple(row) for row in db.connection.execute('SELECT * FROM jobs')], before_jobs)
                self.assertEqual([tuple(row) for row in db.connection.execute('SELECT * FROM drive_objects')], before_objects)
                self.assertEqual(db.get('computer_id'), 'original-computer')
                self.assertEqual(db.get('paused'), '1')
                self.assertEqual(db.connection.execute('PRAGMA user_version').fetchone()[0], 4)
                # A subsequently disabled partition remains reserved when reopened.
                with db.connection:
                    db.connection.execute("UPDATE roots SET enabled=0 WHERE id='active-root'")
            finally:
                db.close()
            reopened = Database(path)
            try:
                self.assertEqual(reopened.connection.execute(
                    "SELECT selection_partition FROM roots WHERE id='active-root'").fetchone()[0], 1)
            finally:
                reopened.close()
