"""Reviewed folder inclusion changes, preserving historical root/file identities."""
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from .directory_rules import DirectoryRules
from .engine import Engine
from .filters import FilterSet
from .restore import _open_absolute_directory
from .scanner import scan_root


class FolderSelection:
    def __init__(self, database, *, home=None, clock=time.time):
        self.db=database; self.sql=database.connection
        self.home=Path(home or Path.home()).resolve()
        self.private=database.path.parent.resolve()
        self.clock=clock

    def _roots(self): return [dict(row) for row in self.sql.execute('SELECT * FROM roots ORDER BY path')]

    def _rules(self):
        encoded=self.db.get('directory_rules')
        if encoded is None:
            rules=[dict(path=row['path'],included=True) for row in self._roots() if row['enabled']]
        else: rules=json.loads(encoded)
        return DirectoryRules(rules).rules

    def _revision(self):
        core=dict(rules=self._rules(), roots=self._roots(),
                  global_patterns=self.db.get('global_patterns','[]'),
                  global_ignore_case=self.db.get('global_ignore_case','0'),
                  reconnect_pending=self.db.get('reconnect_pending'))
        # Transient scanner status is not a configuration edit.
        for root in core['roots']: root.pop('status',None)
        return hashlib.sha256(json.dumps(core,sort_keys=True).encode()).hexdigest()

    def snapshot(self):
        return dict(home=str(self.home),blocked_paths=[str(self.private)], rules=[rule for rule in self._rules()
                    if Path(rule['path']).is_relative_to(self.home) or self.home.is_relative_to(Path(rule['path']))], revision=self._revision(),scope='home',
                    legacy_roots=[row for row in self._roots() if not Path(row['path']).is_relative_to(self.home)])

    def _validated(self, rules):
        if self.db.get('reconnect_pending') is not None:
            raise ValueError('Conclua ou cancele a reconexão antes de alterar a seleção')
        if not isinstance(rules,list) or len(rules)>500:
            raise ValueError('Informe até 500 regras de diretório')
        current=self._rules()
        protected=[rule for rule in current if not Path(rule['path']).is_relative_to(self.home)
                   or Path(rule['path'])==self.home]
        known={rule['path']:rule['included'] for rule in current}
        edits=[]
        for rule in rules:
            if not isinstance(rule,dict) or set(rule)!={'path','included'} or type(rule['included']) is not bool:
                raise ValueError('Seleção de diretório inválida')
            # Validate lexical path before Path can erase '..'.
            DirectoryRules([rule])
            path=Path(rule['path'])
            if (path==self.home or not path.is_relative_to(self.home)) and known.get(str(path))==rule['included']:
                continue  # Legacy whole-home roots are preserved, not newly selectable.
            if path==self.home or not path.is_relative_to(self.home):
                raise ValueError('Selecione diretórios dentro da pasta pessoal')
            if path.is_relative_to(self.private):
                raise ValueError('Os dados internos do aplicativo não podem ser selecionados')
            if known.get(str(path))!=rule['included']:
                if path.exists():
                    with _open_absolute_directory(path): pass
                elif rule['included']:
                    raise ValueError('A pasta selecionada não existe ou ficou inacessível')
            edits.append(rule)
        return DirectoryRules([*protected,*edits],blocked_paths=[str(self.private)])

    def browse(self,path=None,show_hidden=False):
        path=Path(path or self.home).absolute()
        if not path.is_relative_to(self.home) or '..' in path.parts:
            raise ValueError('A navegação fica dentro da sua pasta pessoal')
        if path.is_relative_to(self.private):
            raise ValueError('Esta pasta contém dados internos do aplicativo')
        selection=DirectoryRules(self._rules(),blocked_paths=[str(self.private)])
        folders=[]
        with _open_absolute_directory(path) as descriptor:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False): continue
                    hidden=entry.name.startswith('.')
                    if hidden and not show_hidden: continue
                    absolute=path/entry.name
                    blocked=absolute.is_relative_to(self.private)
                    folders.append(dict(name=entry.name,path=str(absolute),hidden=hidden,
                        state=selection.state(str(absolute)),blocked=blocked,
                        reason='Dados internos do aplicativo' if blocked else ''))
        folders.sort(key=lambda item:item['name'].casefold())
        return dict(home=str(self.home),path=str(path),parent=None if path==self.home else str(path.parent),
                    folders=folders[:1000],truncated=len(folders)>1000)

    @staticmethod
    def _potential(selection,path): return selection.state(str(path))!='excluded'

    def _plan_roots(self,selection):
        roots=self._roots(); active=[]
        for root in roots:
            if root['selection_partition'] or root['enabled']:
                root['enabled']=int(self._potential(selection,root['path']))
                if root['enabled']: active.append(root)
        # Reuse dormant legacy roots only when an active ancestor does not own the area.
        dormant=sorted([r for r in roots if not r['selection_partition'] and not r['enabled']],
                       key=lambda r:len(Path(r['path']).parts),reverse=True)
        for root in dormant:
            path=Path(root['path'])
            if not self._potential(selection,path): continue
            if any(path.is_relative_to(Path(other['path'])) for other in active): continue
            needed=selection.includes(str(path)) or any(rule['included'] and Path(rule['path']).is_relative_to(path)
                    and not any(Path(rule['path']).is_relative_to(Path(other['path'])) for other in active)
                    for rule in selection.rules)
            if needed:
                root['enabled']=1;root['selection_partition']=1;active.append(root)
        new=[]
        for rule in selection.rules:
            path=Path(rule['path'])
            if not rule['included'] or not path.is_relative_to(self.home): continue
            if any(path.is_relative_to(Path(root['path'])) for root in active): continue
            row=dict(id='new:'+str(path),path=str(path),name=path.name,enabled=1,status='pending',
                     patterns='[]',ignore_case=0,selection_partition=1)
            roots.append(row);active.append(row);new.append(str(path))
        for root in roots:
            if root['enabled']: root['selection_partition']=1
        return roots,new

    @staticmethod
    def _owner(absolute,roots):
        candidates=[row for row in roots if row['selection_partition'] and Path(absolute).is_relative_to(Path(row['path']))]
        return max(candidates,key=lambda row:len(Path(row['path']).parts))['id'] if candidates else None

    def _build(self,rules,revision):
        if revision!=self._revision():
            raise ValueError('A configuração mudou. Recarregue a seleção e confira novamente.')
        selection=self._validated(rules); roots,new=self._plan_roots(selection)
        before=DirectoryRules(self._rules(),blocked_paths=[str(self.private)])
        paths={rule['path'] for rule in [*before.rules,*selection.rules]}
        additions=[path for path in sorted(paths) if selection.includes(path) and not before.includes(path)]
        removals=[path for path in sorted(paths) if before.includes(path) and not selection.includes(path)]
        scans={}; manifest=[]
        global_filter=FilterSet(json.loads(self.db.get('global_patterns','[]')),
                              ignore_case=self.db.get('global_ignore_case','0')=='1')
        for root in roots:
            if not root['enabled']: continue
            base=Path(root['path']); filters=FilterSet(json.loads(root['patterns']),ignore_case=bool(root['ignore_case']))
            def allowed(relative,is_dir):
                absolute=base/relative
                if absolute.is_relative_to(self.private): return False
                if self._owner(absolute,roots)!=root['id']: return False
                if is_dir and selection.state(str(absolute))=='excluded': return False
                if not is_dir and not selection.includes(str(absolute)): return False
                return not(global_filter.excludes(relative,is_dir=is_dir) or filters.excludes(relative,is_dir=is_dir))
            scan=scan_root(base,should_descend=lambda path:allowed(path,True),include_file=lambda path:allowed(path,False))
            # Untouched/unavailable existing roots do not become implicit removals.
            if not scan.complete:
                if root['id'].startswith('new:') or any(Path(change).is_relative_to(base) or base.is_relative_to(Path(change)) for change in additions):
                    raise ValueError('Não foi possível conferir a pasta selecionada. Verifique acesso e permissões.')
                scans[root['id']]=None
                continue
            scans[root['id']]=scan
            manifest.extend((root['id'],p,list(s.fingerprint),s.device,s.inode) for p,s in sorted(scan.files.items()))
        files=[dict(row) for row in self.sql.execute('SELECT * FROM files ORDER BY root_id,path')]
        retain=[]; restore=[]; uploads=0; examples=[]
        root_by_id={root['id']:root for root in roots}
        for row in files:
            root=root_by_id[row['root_id']]; absolute=str(Path(root['path'])/row['path'])
            eligible=root['enabled'] and selection.includes(absolute) and self._owner(absolute,roots)==root['id']
            filters=FilterSet(json.loads(root['patterns']),ignore_case=bool(root['ignore_case']))
            eligible=eligible and not(global_filter.excludes(row['path']) or filters.excludes(row['path']))
            if row['state']=='active' and not eligible:
                retain.append(row['id']);examples.append(absolute)
            scan=scans.get(root['id'])
            if row['state']!='active' and eligible and scan and row['path'] in scan.files:
                restore.append(row['id'])
        known={(row['root_id'],row['path']) for row in files}
        for root_id,scan in scans.items():
            if scan: uploads+=sum(1 for path in scan.files if (root_id,path) not in known)
        warnings=['Filtros globais e por pasta continuam valendo, mesmo para diretórios marcados.']
        if any(Path(root['path']).is_relative_to(Path(parent)) and root['path']!=parent and root['id'][:4]!='new:'
               for parent in new for root in roots if root['enabled']):
            warnings.append('Pastas já cadastradas manterão seus destinos atuais no Drive, sem recriar arquivos.')
        if any(Path(self.private).is_relative_to(Path(rule['path'])) and rule['included'] for rule in selection.rules):
            warnings.append('Os dados internos do aplicativo permanecem fora do backup.')
        content=dict(rules=selection.rules,roots=[{key:value for key,value in root.items() if key!='status'} for root in roots],
                     files=files,manifest=manifest,revision=revision)
        digest=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
        result=dict(revision=revision,additions=additions,removals=removals,retention_count=len(retain),
                    upload_count=uploads+len(restore),examples=examples[:30],warnings=warnings)
        return digest,result,selection,roots,files,retain,restore,scans

    def preview(self,rules,revision):
        built=self._build(rules,revision)
        return built[0],built[1]

    def apply(self,rules,revision,expected_digest):
        digest,result,selection,roots,files,retain,restore,scans=self._build(rules,revision)
        if digest!=expected_digest:
            raise ValueError('Os arquivos ou efeitos mudaram. Revise a seleção novamente.')
        engine=Engine(self.db,None,clock=self.clock); now=self.clock()
        with self.sql:
            self.sql.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('directory_rules',json.dumps(selection.rules)))
            for root in roots:
                if root['id'].startswith('new:'):
                    self.sql.execute('INSERT INTO roots(id,path,name,enabled,status,selection_partition) VALUES(?,?,?,?,?,1)',
                                     (str(uuid4()),root['path'],root['name'],1,'pending'))
                else:
                    self.sql.execute("UPDATE roots SET enabled=?,selection_partition=?,status=? WHERE id=?",
                        (root['enabled'],root['selection_partition'],'pending' if root['enabled'] else 'removed',root['id']))
            by_id={row['id']:row for row in files}
            for file_id in retain: engine._exclude(by_id[file_id],now)
            for file_id in restore:
                row=by_id[file_id]; snap=scans[row['root_id']].files[row['path']]
                self.sql.execute("UPDATE files SET state='active',deleted_at=NULL,fingerprint=?,observed_at=?,device=?,inode=? WHERE id=?",
                                 (json.dumps(list(snap.fingerprint)),now,snap.device,snap.inode,file_id))
                self.sql.execute('DELETE FROM jobs WHERE file_id=?',(file_id,))
                engine._queue(file_id,'upload')
        return dict(ok=True,revision=self._revision(),rules=self.snapshot()['rules'])
