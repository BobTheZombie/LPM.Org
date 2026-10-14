"""SQLite-to-scheduler integration tests for build provenance and recovery."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from lpm import app, stage2


class Stage2DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dbpath = self.root / 'state.db'
        self.recipe = self.root / 'demo.lpmbuild'
        self.recipe.write_text('NAME=demo\nVERSION=1\nRELEASE=1\nARCH=noarch\n'
                              'STAGE_2_REQUIRES=(dep)\nbuild_stage2() { :; }\n')

    def installed(self):
        conn = app._open_state_db(self.dbpath)
        try:
            return app.db_installed(conn)
        finally:
            conn.close()

    def write(self, name, build_stage):
        conn = app._open_state_db(self.dbpath)
        app._store_installed_package(conn, app.PkgMeta(name, '1', arch='noarch', build_stage=build_stage), [], True)
        conn.commit()
        conn.close()

    def legacy(self, histories):
        conn = sqlite3.connect(self.dbpath)
        conn.executescript('''CREATE TABLE installed(name TEXT PRIMARY KEY,version TEXT,release TEXT,arch TEXT,
            provides TEXT,symbols TEXT,requires TEXT,manifest TEXT,explicit INTEGER,install_time INTEGER);
            CREATE TABLE history(id INTEGER PRIMARY KEY,ts INTEGER,action TEXT,name TEXT,
            from_ver TEXT,to_ver TEXT,details TEXT);''')
        conn.execute("INSERT INTO installed VALUES('demo','1','1','noarch','[]','[]','[]','[]',1,0)")
        for details in histories:
            conn.execute("INSERT INTO history(ts,action,name,details) VALUES(0,'install','demo',?)", (details,))
        conn.commit()
        conn.close()

    def test_write_and_read_stage_and_upgrade(self):
        self.write('demo', 1)
        self.assertEqual(self.installed()['demo']['build_stage'], 1)
        self.write('demo', 2)
        self.assertEqual(self.installed()['demo']['build_stage'], 2)

    def test_migrate_matching_history(self):
        self.legacy([json.dumps({'version':'1','release':'1','arch':'noarch','build_stage':1})])
        self.assertEqual(self.installed()['demo']['build_stage'], 1)
        self.assertEqual(self.installed()['demo']['build_stage'], 1)

    def test_migrate_without_history_is_unknown(self):
        self.legacy([])
        meta = self.installed()['demo']
        self.assertEqual(meta['build_stage'], 0)
        self.assertFalse(stage2._matches(meta, dict(version='1',release='1',arch='noarch'), 2))

    def test_migration_does_not_use_stale_history(self):
        self.legacy([json.dumps({'version':'1','release':'1','arch':'noarch','build_stage':2}),
                     json.dumps({'version':'1','release':'1','arch':'noarch'})])
        self.assertEqual(self.installed()['demo']['build_stage'], 0)

    def test_mismatch_or_malformed_history_is_unknown(self):
        self.legacy([json.dumps({'version':'2','release':'1','arch':'noarch','build_stage':2}), 'invalid'])
        self.assertEqual(self.installed()['demo']['build_stage'], 0)

    def test_missing_stage_is_not_final(self):
        self.assertFalse(stage2._matches({'version':'1','release':'1','arch':'noarch'},
                                        dict(version='1',release='1',arch='noarch'), 2))

    def drain(self):
        calls = []
        def build(script, outdir, build_stage, overrides):
            calls.append(build_stage)
            self.write('demo', build_stage)
            return 'built'
        stage2.drain(self.root, self.installed, build, lambda *args: None,
                     read_recipe=app._capture_lpmbuild_metadata)
        return calls

    def enqueue(self):
        stage2.enqueue(self.root, self.recipe, self.root,
                       app.PkgMeta('demo','1',arch='noarch',build_stage=1), ['dep'])

    def test_real_database_bootstrap_runs_final_then_clears_queue(self):
        self.write('demo',1)
        self.write('dep',2)
        self.enqueue()
        self.assertEqual(self.drain(), [2])
        self.assertEqual(self.installed()['demo']['build_stage'], 2)
        self.assertEqual(list((self.root/'stage2').glob('*.json')), [])
        self.assertEqual(self.drain(), [])

    def test_unknown_legacy_stage_is_rebuilt_not_discarded(self):
        self.legacy([])
        self.write('dep',2)
        self.enqueue()
        self.assertEqual(self.drain(), [2])

    def test_declined_final_keeps_queue(self):
        self.write('demo',1)
        self.write('dep',2)
        self.enqueue()
        stage2.drain(self.root,self.installed,lambda *args: None,lambda *args: None,
                     read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(len(list((self.root/'stage2').glob('*.json'))), 1)

    def test_transaction_rollback_preserves_bootstrap_stage(self):
        self.write('demo', 1)
        conn = app._open_state_db(self.dbpath)
        app._store_installed_package(conn,
            app.PkgMeta('demo', '1', arch='noarch', build_stage=2), [], True)
        conn.rollback()
        conn.close()
        self.assertEqual(self.installed()['demo']['build_stage'], 1)

    def test_failed_final_keeps_queue(self):
        self.write('demo', 1)
        self.write('dep', 2)
        self.enqueue()
        def fail(*args):
            raise RuntimeError('compile failed')
        with self.assertRaisesRegex(RuntimeError, 'compile failed'):
            stage2.drain(self.root, self.installed, fail, lambda *args: None,
                         read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(len(list((self.root/'stage2').glob('*.json'))), 1)
        self.assertEqual(self.installed()['demo']['build_stage'], 1)

    def test_explicit_recipe_recovery(self):
        self.write('demo',1)
        args = argparse.Namespace(recipe=[self.recipe],outdir=self.root,install_default='y',plan=False)
        with patch.object(app,'db',lambda: app._open_state_db(self.dbpath)), \
             patch.object(app,'_current_state_dir',lambda: self.root), \
             patch.object(app,'_run_stage2_queue',return_value={}) as drain:
            app.cmd_rebuild_stage2(args)
            drain.assert_called_once()
        jobs = list((self.root/'stage2').glob('*.json'))
        self.assertEqual(len(jobs),1)
        self.assertEqual(json.loads(jobs[0].read_text())['name'],'demo')


if __name__ == '__main__':
    unittest.main()
