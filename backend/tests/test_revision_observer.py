"""Logical revisions change for writes/restores, not ordinary read traffic."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import runtime
from app.db import connect,init_db


class RevisionObserverTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(runtime._close_revision_observers)
        self.path=Path(self.folder.name)/'finance.db'
        self.make(self.path)

    def make(self,path):
        conn=init_db(path)
        conn.execute("INSERT INTO accounts(id,bank,account_type) VALUES('bank','Bank','current')")
        conn.commit();conn.close()

    def revision(self,path=None):
        response,status=runtime.database_revision(path or self.path)
        self.assertEqual(status,200)
        self.assertEqual(len(response['revision']),24)
        return response['revision']

    def test_read_connections_and_timestamps_do_not_change_revision(self):
        before=self.revision()
        for _ in range(4):
            conn=connect(self.path)
            conn.execute('SELECT COUNT(*) FROM accounts').fetchone()
            conn.close()
            os.utime(self.path,None)
            self.assertEqual(self.revision(),before)
        observer=next(iter(runtime._revision_observers.values()))
        self.assertFalse(observer['connection'].in_transaction)
        with self.assertRaises(sqlite3.OperationalError):
            observer['connection'].execute("UPDATE accounts SET bank='Forbidden'")

    def test_other_process_commit_is_seen_and_uncommitted_write_is_not(self):
        before=self.revision()
        writer=connect(self.path)
        writer.execute("UPDATE accounts SET bank='Pending edit'")
        self.assertEqual(self.revision(),before)
        writer.rollback();writer.close()
        subprocess.run([sys.executable,'-c',"import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE accounts SET bank='External edit'\"); c.commit(); c.close()",str(self.path)],check=True)
        after=self.revision()
        self.assertNotEqual(before,after)
        self.assertEqual(after,self.revision())

    def test_atomic_restore_and_symlink_retarget_invalidate_old_observers(self):
        before=self.revision()
        replacement=Path(self.folder.name)/'replacement.db';self.make(replacement)
        # A restore can contain identical rows; it still requires re-reading.
        replacement.replace(self.path)
        restored=self.revision()
        self.assertNotEqual(before,restored)
        link=Path(self.folder.name)/'current.db';link.symlink_to(self.path)
        linked=self.revision(link)
        another=Path(self.folder.name)/'another.db';self.make(another)
        link.unlink();link.symlink_to(another)
        self.assertNotEqual(linked,self.revision(link))

    def test_observer_eviction_closes_handles_and_restart_invalidates(self):
        before=self.revision()
        old=next(iter(runtime._revision_observers.values()))['connection']
        with patch.object(runtime,'_REVISION_OBSERVER_LIMIT',2):
            for index in range(3):
                path=Path(self.folder.name)/f'extra-{index}.db';self.make(path);self.revision(path)
            self.assertEqual(len(runtime._revision_observers),2)
        with self.assertRaises(sqlite3.ProgrammingError):old.execute('SELECT 1')
        self.assertNotEqual(before,self.revision())
        before=self.revision();runtime._close_revision_observers()
        self.assertNotEqual(before,self.revision())

    def test_concurrent_pollers_share_one_stable_revision(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            values=list(pool.map(lambda _:self.revision(),range(30)))
        self.assertEqual(len(set(values)),1)
        self.assertEqual(len(runtime._revision_observers),1)

    def test_missing_or_corrupt_replacement_closes_observer(self):
        self.revision()
        self.path.unlink()
        self.assertEqual(runtime.database_revision(self.path)[1],503)
        self.assertFalse(runtime._revision_observers)
        self.path.write_bytes(b'not sqlite')
        self.assertEqual(runtime.database_revision(self.path)[1],503)
        self.assertFalse(runtime._revision_observers)


if __name__=='__main__':unittest.main()
