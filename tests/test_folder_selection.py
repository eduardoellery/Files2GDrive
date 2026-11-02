import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from files2gdrive.database import Database
from files2gdrive.engine import Engine,RETENTION
from files2gdrive.folder_selection import FolderSelection
from files2gdrive.remote import SimulatedRemote


class FolderSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.home=self.base/'home';self.home.mkdir()
        self.docs=self.home/'Documentos';self.docs.mkdir()
        self.project=self.docs/'Projetos';self.project.mkdir()
        self.personal=self.docs/'Pessoal';self.personal.mkdir()
        (self.docs/'file.txt').write_text('document')
        (self.project/'config').write_text('project')
        (self.personal/'notes').write_text('personal')
        self.state=self.home/'.local/state/files2gdrive'
        self.db=Database(self.state/'state.sqlite3')
        self.remote=SimulatedRemote(self.base/'remote')
        self.now=1000
        self.engine=Engine(self.db,self.remote,clock=lambda:self.now,settle_seconds=0)
        self.manager=FolderSelection(self.db,home=self.home,clock=lambda:self.now)
    def tearDown(self):self.db.close();self.temp.cleanup()
    def rules(self,*items):return [dict(path=str(path),included=value) for path,value in items]
    def apply(self,rules):
        revision=self.manager.snapshot()['revision']
        digest,result=self.manager.preview(rules,revision)
        self.manager.apply(rules,revision,digest)
        return result
    def files(self):return [dict(row) for row in self.db.connection.execute('SELECT * FROM files')]
    def test_new_parent_selection_backups_contents_and_subfolders(self):
        self.apply(self.rules((self.docs,True)));self.engine.tick()
        self.assertEqual(len(self.files()),3)
        self.assertTrue(all(self.remote.read(row['id'])['state']=='active' for row in self.files()))
    def test_uncheck_subfolder_retains_only_its_files_and_reinclude_cancels(self):
        self.apply(self.rules((self.docs,True)));self.engine.tick()
        original=next(row for row in self.files() if row['path']=='Projetos/config')
        changed=self.rules((self.docs,True),(self.project,False))
        preview=self.apply(changed)
        self.assertEqual(preview['retention_count'],1)
        row=next(row for row in self.files() if row['id']==original['id'])
        self.assertEqual(row['state'],'excluded');self.assertEqual(row['deleted_at'],self.now)
        self.engine.tick();self.assertEqual(self.remote.read(row['id'])['state'],'excluded')
        self.now+=100;self.apply(self.rules((self.docs,True)));self.engine.tick()
        restored=next(row for row in self.files() if row['id']==original['id'])
        self.assertEqual(restored['state'],'active');self.assertIsNone(restored['deleted_at'])
        self.assertEqual(self.remote.read(row['id'])['state'],'active')
    def test_deep_reinclusion_overrides_checkbox_exception(self):
        sub=self.project/'Only';sub.mkdir();(sub/'file').write_text('included')
        self.apply(self.rules((self.docs,True),(self.project,False),(sub,True)));self.engine.tick()
        self.assertEqual({row['path'] for row in self.files()},{'file.txt','Pessoal/notes','Projetos/Only/file'})
    def test_filters_still_veto_checked_directory(self):
        self.engine.set_filters(['config'])
        self.apply(self.rules((self.docs,True)));self.engine.tick()
        self.assertNotIn('Projetos/config',{row['path'] for row in self.files()})
    def test_selecting_parent_preserves_existing_child_ids_without_duplicates(self):
        old=self.engine.add_root(self.project,'Meu projeto');self.engine.tick()
        file_id=self.files()[0]['id']
        preview=self.apply(self.rules((self.docs,True)))
        self.assertTrue(any('destinos atuais' in message for message in preview['warnings']))
        self.engine.tick()
        rows=self.files();self.assertEqual(len(rows),3)
        existing=next(row for row in rows if row['id']==file_id)
        self.assertEqual(existing['root_id'],old);self.assertEqual(existing['path'],'config')
        self.assertEqual(existing['state'],'active')
        self.assertEqual(len(list(self.remote.directory.glob('*.json'))),3)
    def test_selected_child_uses_existing_parent_root(self):
        root_id=self.engine.add_root(self.docs);self.engine.tick()
        ids={row['path']:row['id'] for row in self.files()}
        self.apply(self.rules((self.project,True)));self.engine.tick()
        roots=self.db.connection.execute('SELECT * FROM roots').fetchall()
        self.assertEqual(len(roots),1);self.assertEqual(roots[0]['id'],root_id)
        row=next(row for row in self.files() if row['path']=='Projetos/config')
        self.assertEqual(row['id'],ids['Projetos/config']);self.assertEqual(row['state'],'active')
    def test_dormant_old_child_does_not_steal_live_parent_files(self):
        old=self.engine.add_root(self.project);self.engine.tick();self.engine.remove_root(old);self.engine.tick()
        # Legacy dormant roots do not claim ownership until explicitly reused.
        with self.db.connection:self.db.connection.execute('UPDATE roots SET selection_partition=0 WHERE id=?',(old,))
        parent=self.engine.add_root(self.docs);self.engine.tick()
        row=next(row for row in self.files() if row['root_id']==parent and row['path']=='Projetos/config')
        downloads=self.home/'Downloads';downloads.mkdir()
        self.apply(self.rules((self.docs,True),(downloads,True)));self.engine.tick()
        self.assertEqual(next(item for item in self.files() if item['id']==row['id'])['state'],'active')
        self.assertFalse(self.db.connection.execute('SELECT enabled FROM roots WHERE id=?',(old,)).fetchone()[0])
    def test_reserved_child_reappears_without_reassigning_identity(self):
        child=self.engine.add_root(self.project);self.engine.tick();identifier=self.files()[0]['id']
        self.apply(self.rules((self.docs,True),(self.project,False)));self.engine.tick()
        self.now+=100
        self.apply(self.rules((self.docs,True)));self.engine.tick()
        selected=next(row for row in self.files() if row['id']==identifier)
        self.assertEqual(selected['root_id'],child);self.assertEqual(selected['state'],'active')
    def test_existing_retention_deadline_not_restarted_when_removing_parent(self):
        self.apply(self.rules((self.docs,True)));self.engine.tick()
        (self.project/'config').unlink();self.engine.tick()
        row=next(item for item in self.files() if item['path']=='Projetos/config');deleted=row['deleted_at']
        self.now+=100;self.apply([])
        self.assertEqual(next(item for item in self.files() if item['id']==row['id'])['deleted_at'],deleted)
    def test_preview_and_apply_do_not_upload_and_respect_manual_pause(self):
        self.engine.pause()
        rules=self.rules((self.docs,True));revision=self.manager.snapshot()['revision']
        digest,_=self.manager.preview(rules,revision)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM roots').fetchone()[0],0)
        self.manager.apply(rules,revision,digest);self.engine.tick()
        self.assertEqual(self.files(),[]);self.assertTrue(self.engine.paused)
    def test_changed_files_or_configuration_invalidate_review(self):
        rules=self.rules((self.docs,True));revision=self.manager.snapshot()['revision']
        digest,_=self.manager.preview(rules,revision)
        (self.docs/'new').write_text('new')
        with self.assertRaises(ValueError):self.manager.apply(rules,revision,digest)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM roots').fetchone()[0],0)
        self.engine.set_filters(['*.tmp'])
        with self.assertRaises(ValueError):self.manager.preview(rules,revision)
    def test_private_state_is_pruned_when_parent_selected(self):
        (self.state/'secret.json').write_text('must not leave state')
        self.apply(self.rules((self.home/'.local',True)));self.engine.tick()
        self.assertEqual(self.files(),[])
        self.assertEqual(self.manager.browse(self.home,True)['folders'][0]['name'],'.local')
        with self.assertRaises(ValueError):self.apply(self.rules((self.state,True)))
    def test_scope_hidden_and_symlinks(self):
        hidden=self.home/'.config';hidden.mkdir()
        (self.home/'link').symlink_to(self.docs,target_is_directory=True)
        visible=self.manager.browse(self.home,False)
        self.assertNotIn('.config',{row['name'] for row in visible['folders']})
        shown=self.manager.browse(self.home,True)
        self.assertIn('.config',{row['name'] for row in shown['folders']})
        self.assertNotIn('link',{row['name'] for row in shown['folders']})
        for path in (self.base,self.home/'link'):
            with self.assertRaises((OSError,ValueError)):self.manager.browse(path)
        with self.assertRaises((OSError,ValueError)):self.apply(self.rules((self.home/'link',True)))
    def test_external_legacy_roots_remain_selected(self):
        external=self.base/'external';external.mkdir();(external/'file').write_text('outside')
        root=self.engine.add_root(external);self.engine.tick()
        self.apply(self.rules((self.docs,True)));self.engine.tick()
        self.assertTrue(self.db.connection.execute('SELECT enabled FROM roots WHERE id=?',(root,)).fetchone()[0])
        self.assertTrue(next(row for row in self.files() if row['root_id']==root)['state']=='active')
