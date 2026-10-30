"""Installed institutional Skill updates; explicit mappings, no directory discovery."""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import tempfile


def _digest(files):
    content=[[name,hashlib.sha256(data).hexdigest()] for name,data in sorted(files.items())]
    return hashlib.sha256(json.dumps(content,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _safe_name(name):
    if not isinstance(name,str) or not name or name in ('.','..') or '/' in name or '\\' in name or '\x00' in name:
        raise ValueError('Skill 来源包含不安全文件名')
    return name


class SharedSkillsMixin:
    def _skills_error(self,code,message):
        raise self.error_type(code,message)

    def _skill_source(self,folder):
        if not isinstance(folder,str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,200}',folder):self._skills_error('invalid_identifier','Skill 来源标识无效')
        root=(self.manifest.get('shared_skills') or {}).get('root_folder_id')
        if not root:self._skills_error('shared_skills_not_registered','当前接入配置未登记机构 Shared Skills 来源')
        package=self.transport.skill_package(root,folder)
        files=package.get('files')
        if not isinstance(files,dict) or not files or not any(PurePosixPath(name).name.casefold()=='skill.md' for name in files):
            self._skills_error('incomplete_skill_package','Skill 包未完整读取或缺少 SKILL.md')
        total=0
        for name,data in files.items():
            parts=PurePosixPath(name).parts
            if not parts or PurePosixPath(name).is_absolute() or any(_safe_name(part)!=part for part in parts) or not isinstance(data,bytes):
                self._skills_error('invalid_skill_package','Skill 包路径或内容无效')
            total+=len(data)
        if len(files)>2000 or total>32_000_000:self._skills_error('skill_package_too_large','Skill 包超过本次更新容量')
        return {**package,'content_hash':_digest(files)}

    def _local_files(self,directory):
        path=Path(directory).expanduser()
        if path.is_symlink() or not path.is_dir() or path.resolve() in (Path('/'),Path.home().resolve(),Path.cwd().resolve()):
            self._skills_error('invalid_skill_directory','请指定已经安装的单个 Skill 文件夹')
        files={}
        for current in path.rglob('*'):
            if current.is_symlink():self._skills_error('local_skill_conflict','Skill 内含链接，未跟随或覆盖')
            if current.is_file():
                relative=current.relative_to(path).as_posix()
                if len(files)>=2000 or current.stat().st_size>8_000_000:
                    self._skills_error('local_skill_conflict','本地 Skill 包超过检查容量')
                files[relative]=current.read_bytes()
        return path.resolve(),files

    def link_shared_skill(self,folder,directory):
        """Map one known installed package, never search or install source folders."""
        source=self._skill_source(folder);path,files=self._local_files(directory)
        if not any(PurePosixPath(name).name.casefold()=='skill.md' for name in files):
            self._skills_error('invalid_skill_directory','指定文件夹没有已安装的 SKILL.md')
        selected={name:files[name] for name in source['files'] if name in files}
        clean=selected==source['files']
        binding={'folder_id':folder,'name':source.get('name',folder),'directory':str(path),
            'source_hash':source['content_hash'],'base_files':{name:hashlib.sha256(data).hexdigest() for name,data in selected.items()},
            'initial_conflict':not clean,'pending':None}
        with self.state.locked():
            existing=self.state.get('operations','shared_skill:'+folder)
            if existing:
                if existing['directory']!=str(path):self._skills_error('skill_mapping_conflict','该来源已经关联其他本地 Skill，未覆盖关联')
                return {'linked':True,'name':existing['name'],'already_linked':True}
            self.state.put('operations','shared_skill:'+folder,binding)
        return {'linked':True,'name':binding['name'],'local_modified':not clean,'source_hash':source['content_hash']}

    def check_shared_skills(self):
        entries=[]
        rows=self.state.db.execute("SELECT payload FROM operations WHERE id LIKE 'shared_skill:%'").fetchall()
        for row in rows:
            binding=json.loads(row[0]);folder=binding['folder_id']
            try:
                source=self._skill_source(folder);_,local=self._local_files(binding['directory'])
                modified=binding.get('initial_conflict',False) or any(
                    name not in local or hashlib.sha256(local[name]).hexdigest()!=digest
                    for name,digest in binding['base_files'].items())
                current=source['content_hash']
                if current==binding['source_hash']:
                    entries.append({'folder_id':folder,'name':binding['name'],'status':'current','local_modified':modified});continue
                pending=binding.get('pending') or {}
                fresh=pending.get('content_hash')!=current
                if fresh:
                    pending={'content_hash':current,'decision':'pending','prompted':True}
                    with self.state.locked():self.state.put('operations','shared_skill:'+folder,{**binding,'pending':pending})
                entries.append({'folder_id':folder,'name':binding['name'],'status':'update_available',
                    'content_hash':current,'local_modified':modified,'decision':pending['decision'],
                    'needs_prompt':fresh,'message':'机构 Skill 有更新，请询问本节点用户是否更新该版本。' if fresh else ''})
            except (ValueError,OSError) as exc:
                entries.append({'folder_id':folder,'name':binding.get('name',folder),'status':'check_failed',
                    'error_code':getattr(exc,'code','source_unavailable'),'needs_prompt':False})
        return {'status':'not_linked' if not entries else 'partial' if any(x['status']=='check_failed' for x in entries) else 'succeeded',
                'checked_count':len(entries),'skills':entries,
                'message':'尚未关联本机已有的机构 Skill；没有据此判断其他包是否最新。' if not entries else ''}

    def decide_shared_skill(self,folder,content_hash,decision):
        if decision not in ('approve','decline'):self._skills_error('invalid_decision','请选择同意或暂不更新')
        with self.state.locked():
            binding=self.state.get('operations','shared_skill:'+folder)
            pending=(binding or {}).get('pending') or {}
            if pending.get('content_hash')!=content_hash:
                self._skills_error('skill_version_conflict','请先检查当前来源版本，再确认更新')
            pending={**pending,'decision':decision}
            self.state.put('operations','shared_skill:'+folder,{**binding,'pending':pending})
        return {'name':binding['name'],'decision':decision,'content_hash':content_hash}

    def _finish_skill_update(self,folder,binding,source,backup):
        content_hash=source['content_hash']
        result={'updated':True,'name':binding['name'],'content_hash':content_hash,'readback_verified':True,
                'message':'本地包已更新并核对；后续会话实际调用另行确认。'}
        with self.state.locked():
            self.state.put('operations','shared_skill:'+folder,{**binding,'source_hash':content_hash,
                'base_files':{name:hashlib.sha256(data).hexdigest() for name,data in source['files'].items()},
                'initial_conflict':False,'pending':None,'last_backup':str(backup),'last_update_hash':content_hash})
            self.state.put('operations','shared_skill_update:'+folder,{'state':'verified','content_hash':content_hash,'result':result})
        return result

    def apply_shared_skill(self,folder,content_hash):
        binding=self.state.get('operations','shared_skill:'+folder)
        pending=(binding or {}).get('pending') or {}
        prior=self.state.get('operations','shared_skill_update:'+folder) or {}
        if prior.get('state')=='verified' and prior.get('content_hash')==content_hash:
            source=self._skill_source(folder);_,actual=self._local_files(binding['directory'])
            if source['content_hash']==content_hash and all(actual.get(name)==data for name,data in source['files'].items()):return prior['result']
            self._skills_error('skill_version_conflict','已更新内容发生变化，请重新检查')
        if pending.get('decision')!='approve'  or pending.get('content_hash')!=content_hash:
            self._skills_error('skill_update_not_approved','尚未获得本节点用户对该版本的更新同意')
        source=self._skill_source(folder)
        if source['content_hash']!=content_hash:self._skills_error('skill_version_conflict','来源已变化，请重新检查并询问新版本')
        if prior.get('state')=='prepared' and prior.get('content_hash')==content_hash:
            destination=Path(binding['directory'])
            stage=Path(prior['stage'])
            if stage.parent!=destination.parent or not stage.name.startswith('.rdos-skill-'):
                self._skills_error('invalid_skill_directory','更新检查点位置无效')
            if not destination.exists() and stage.is_dir():
                _,staged=self._local_files(stage)
                if _digest(staged)!=prior.get('stage_hash') or not all(staged.get(name)==data for name,data in source['files'].items()):self._skills_error('skill_readback_failed','待恢复内容或受保护配置与原检查点不符')
                stage.rename(destination)
            if destination.is_dir():
                _,actual=self._local_files(destination)
                if all(actual.get(name)==data for name,data in source['files'].items()):
                    if _digest(actual)!=prior.get('stage_hash'):self._skills_error('local_skill_conflict','恢复期间本地配置有变化，保留当前内容')
                    return self._finish_skill_update(folder,binding,source,Path(prior['backup']))
        path,files=self._local_files(binding['directory'])
        if binding.get('initial_conflict') or any(name not in files or hashlib.sha256(files[name]).hexdigest()!=digest for name,digest in binding['base_files'].items()):
            self._skills_error('local_skill_conflict','本地 Skill 有修改，保留原包；请先决定如何处理冲突')
        for name in source['files']:
            if name in files and name not in binding['base_files'] and files[name]!=source['files'][name]:
                self._skills_error('local_skill_conflict','新版文件与本地配置冲突，未覆盖')
        # Only a known single package is copied; no code is executed.
        backup_root=self.state.path.parent/'skill-backups';backup_root.mkdir(mode=0o700,exist_ok=True)
        if backup_root.is_symlink():self._skills_error('invalid_skill_directory','备份目录不能是链接')
        backup=backup_root/(folder+'-'+binding['source_hash'][:16])
        if not backup.exists():shutil.copytree(path,backup)
        staging=Path(tempfile.mkdtemp(prefix='.rdos-skill-',dir=path.parent))
        rollback=None
        try:
            shutil.copytree(path,staging,dirs_exist_ok=True)
            for name in set(binding['base_files'])-set(source['files']):
                (staging/name).unlink(missing_ok=True)
            for name,data in source['files'].items():
                target=staging/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            # Recheck local package before the directory swap.
            if self._local_files(path)[1]!=files:self._skills_error('local_skill_conflict','更新期间本地内容已变化，未覆盖')
            rollback=path.with_name(path.name+'.rdos-previous-'+content_hash[:12])
            if rollback.exists():self._skills_error('skill_update_pending','上次更新尚待核对，保留当前目录')
            stage_hash=_digest(self._local_files(staging)[1])
            with self.state.locked():self.state.put('operations','shared_skill_update:'+folder,
                {'state':'prepared','content_hash':content_hash,'stage':str(staging),'backup':str(backup),'stage_hash':stage_hash})
            path.rename(rollback);staging.rename(path)
            actual=self._local_files(path)[1]
            if _digest(actual)!=stage_hash or any(actual.get(name)!=data for name,data in source['files'].items()):
                self._skills_error('skill_readback_failed','安装内容未完整核对，原包仍有备份')
            return self._finish_skill_update(folder,binding,source,backup)
        finally:
            # Keep staged content if the original directory was moved but the
            # replacement has not been installed: the next call resumes it.
            if staging.exists() and path.exists():shutil.rmtree(staging)
