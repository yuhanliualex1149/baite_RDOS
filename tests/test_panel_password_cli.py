"""Password recovery with a real terminal and an isolated existing Panel DB."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pty
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


CODE = Path(__file__).resolve().parents[1]
OLD_PASSWORD = "cli-bootstrap-password-only"
NEW_PASSWORD = "cli-recovered-password-only"
PRELOAD = """
import fastapi, fastapi.staticfiles, httpx
import anyio._backends._asyncio
import anyio._core._fileio
from server import lab_panel, lab_routes
"""


class PanelPasswordCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rdos-password-cli-")
        self.root = Path(self.temporary.name).resolve()
        self.code = self.root / "code"
        self.code.mkdir()
        for name in ("server", "lab"):
            shutil.copytree(CODE / name, self.code / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(CODE / "labctl.py", self.code / "labctl.py")
        for path in CODE.glob("rdos*.py"):
            shutil.copy2(path, self.code / path.name)
        for name in ("config", "runtime", "runtime/tmp", "nodes"):
            (self.root / name).mkdir(exist_ok=True)
        cfg = {"schema_version":1,"environment":"rdos_local_lab","env_id":"rdos2_lab_123456abcdef",
            "root":str(self.root),"port":18761,"backends":{"documents":"local_mock","model":"local_mock"},
            "external_resources":{"enabled":False,"folder":"","documents":[],"task_table":"","feedback":"","recipient":""}}
        (self.root / "config/lab.json").write_text(json.dumps(cfg))
        (self.root / "runtime/instance.json").write_text(json.dumps({"env_id":cfg["env_id"],"generation":"1"*32}))
        (self.root / "runtime/credentials.json").write_text(json.dumps({"admin_username":"cli_admin",
            "admin_password":OLD_PASSWORD,"session_secret":"cli-test-session-secret-only-000000","node_tokens":{}}))
        (self.root / "runtime/credentials.json").chmod(0o600)
        (self.root / "runtime/PANEL_ACCESS.txt").write_text("初始密码："+OLD_PASSWORD)
        (self.root / "nodes/sentinel.txt").write_text("节点文件不应被恢复命令改写")
        self.env = {"PATH":os.environ.get("PATH","/usr/bin:/bin"),"LANG":"en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE":"1","PYTHONPATH":str(self.code),"TMPDIR":str(self.root / "runtime/tmp"),
            "RDOS_LAB_MODE":"1","RDOS_LAB_ROOT":str(self.root),"RDOS_LAB_RUNTIME":str(self.root / "runtime"),
            "RDOS_LAB_ENV_ID":cfg["env_id"],"RDOS_LAB_GENERATION":"1"*32,
            "BAITE_DB_PATH":str(self.root / "runtime/panel.sqlite3"),"BAITE_PUBLIC_URL":"http://127.0.0.1:18761",
            "BAITE_ADMIN_USERNAME":"cli_admin","BAITE_ADMIN_PASSWORD":OLD_PASSWORD,
            "BAITE_SESSION_SECRET":"cli-test-session-secret-only-000000"}
        initialized = self.run_python("""
from lab import guard,config
from server import db,central_store as store
guard.install(config.ROOT,18761)
db.init_db();store.init()
store.create_user('second_admin','另一位管理员',['admin'],'second-admin-password')
store.create_user('member_only','研发人员',['researcher'],'member-only-password')
""")
        self.assertEqual(initialized.returncode,0,initialized.stdout+initialized.stderr)

    def tearDown(self):
        self.temporary.cleanup()

    def run_python(self, body):
        return subprocess.run([sys.executable,"-B","-c",PRELOAD+body],cwd=self.code,env=self.env,
                              text=True,capture_output=True,timeout=30)

    def cli_command(self, arguments):
        wrapper = PRELOAD + "\nimport runpy,sys\nsys.argv=['labctl.py',*sys.argv[1:]]\nrunpy.run_path('labctl.py',run_name='__main__')\n"
        return [sys.executable,"-B","-c",wrapper,*arguments]

    def terminal(self, arguments, passwords=()):
        master, slave = pty.openpty()
        proc = subprocess.Popen(self.cli_command(arguments),cwd=self.code,env=self.env,
            stdin=slave,stdout=slave,stderr=slave,close_fds=True)
        os.close(slave)
        output = bytearray()
        prompts = ["新密码（至少 10 个字符）：".encode(),"再次输入新密码：".encode()]
        sent = 0
        deadline = time.monotonic()+25
        try:
            while time.monotonic()<deadline:
                ready,_,_ = select.select([master],[],[],0.1)
                if ready:
                    try:chunk = os.read(master,65536)
                    except OSError as exc:
                        if exc.errno==errno.EIO:break
                        raise
                    if not chunk:break
                    output.extend(chunk)
                if sent<len(passwords) and prompts[sent] in output:
                    os.write(master,passwords[sent].encode()+b"\n")
                    sent+=1
                if proc.poll() is not None and not ready:break
            else:
                self.fail("interactive recovery did not finish")
            return proc.wait(timeout=3),output.decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.kill();proc.wait(timeout=3)
            os.close(master)

    def database(self, query):
        with sqlite3.connect(self.root / "runtime/panel.sqlite3") as conn:
            return conn.execute(query).fetchall()

    def test_terminal_recovery_preserves_records_and_removes_obsolete_plaintext_note(self):
        before_users = self.database("SELECT id FROM central_users ORDER BY id")
        before_projects = self.database("SELECT id,payload FROM central_records WHERE kind='project' ORDER BY id")
        credentials = (self.root / "runtime/credentials.json").read_bytes()
        status, output = self.terminal(["reset-password"],[NEW_PASSWORD,NEW_PASSWORD])
        self.assertEqual(status,0,output)
        self.assertNotIn(NEW_PASSWORD,output)
        self.assertNotIn(OLD_PASSWORD,output)
        self.assertIn("密码已重置",output)
        self.assertEqual(self.database("SELECT id FROM central_users ORDER BY id"),before_users)
        self.assertEqual(self.database("SELECT id,payload FROM central_records WHERE kind='project' ORDER BY id"),before_projects)
        self.assertEqual((self.root / "runtime/credentials.json").read_bytes(),credentials)
        note=(self.root / "runtime/PANEL_ACCESS.txt").read_text()
        self.assertIn("初始密码已失效",note)
        self.assertNotIn(OLD_PASSWORD,note)
        self.assertNotIn(NEW_PASSWORD,note)
        self.assertEqual((self.root / "runtime/PANEL_ACCESS.txt").stat().st_mode & 0o777,0o600)
        self.assertEqual((self.root / "nodes/sentinel.txt").read_text(),"节点文件不应被恢复命令改写")
        checked=self.run_python("""
from lab import config,guard
from server import db,central_store as store
guard.install(config.ROOT,18761)
assert store.login('cli_admin','cli-recovered-password-only')
assert not store.login('cli_admin','cli-bootstrap-password-only')
assert db.authenticate_admin('cli_admin','cli-recovered-password-only')
assert store.get('state','bootstrap_password_status')['access_note_updated'] is True
""")
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)

    def test_nonterminal_and_password_arguments_are_rejected_without_echo(self):
        before=self.database("SELECT password_hash FROM central_users ORDER BY id")
        result=subprocess.run(self.cli_command(["reset-password"]),cwd=self.code,env=self.env,
            input=NEW_PASSWORD+"\n"+NEW_PASSWORD+"\n",text=True,capture_output=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("终端交互",result.stderr)
        self.assertNotIn(NEW_PASSWORD,result.stdout+result.stderr)
        result=subprocess.run(self.cli_command(["reset-password","--new-password",NEW_PASSWORD]),cwd=self.code,
            env=self.env,text=True,capture_output=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn(NEW_PASSWORD,result.stdout+result.stderr)
        self.assertEqual(self.database("SELECT password_hash FROM central_users ORDER BY id"),before)

    def test_mismatch_invalid_target_and_missing_database_do_not_initialize_or_change_passwords(self):
        before=self.database("SELECT password_hash FROM central_users ORDER BY id")
        status,output=self.terminal(["reset-password"],[NEW_PASSWORD,"different-confirmation-password"])
        self.assertNotEqual(status,0)
        self.assertIn("两次输入",output)
        self.assertNotIn(NEW_PASSWORD,output)
        self.assertEqual(self.database("SELECT password_hash FROM central_users ORDER BY id"),before)
        status,output=self.terminal(["reset-password","--username","member_only"])
        self.assertNotEqual(status,0)
        self.assertIn("未找到启用的管理员",output)
        self.assertEqual(self.database("SELECT password_hash FROM central_users ORDER BY id"),before)
        database=self.root / "runtime/panel.sqlite3"
        database.unlink()
        status,output=self.terminal(["reset-password"])
        self.assertNotEqual(status,0)
        self.assertIn("数据库不存在",output)
        self.assertFalse(database.exists())

    def test_explicit_existing_admin_target_does_not_replace_bootstrap_note(self):
        original=(self.root / "runtime/PANEL_ACCESS.txt").read_bytes()
        status,output=self.terminal(["reset-password","--username","second_admin"],[NEW_PASSWORD,NEW_PASSWORD])
        self.assertEqual(status,0,output)
        self.assertEqual((self.root / "runtime/PANEL_ACCESS.txt").read_bytes(),original)
        self.assertNotIn(NEW_PASSWORD,output)
        checked=self.run_python("""
from lab import config,guard
from server import central_store as store
guard.install(config.ROOT,18761)
assert store.login('second_admin','cli-recovered-password-only')
assert store.login('cli_admin','cli-bootstrap-password-only')
assert store.get('state','bootstrap_password_status') is None
""")
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)


if __name__ == '__main__':
    unittest.main()
