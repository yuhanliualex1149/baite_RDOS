from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from server.feishu_tools import (
    ADDED_TASK_FIELDS, BASE_TOKEN, FEEDBACK_DOC, ONBOARDING_DOCS,
    RAG_FOLDER, SKILL_FOLDER, TASK_FIELDS, FeishuError, FeishuTools, _plain,
)


class FakeCli:
    """Stateful remote stand-in; tests verify read-back after uncertain writes."""

    def __init__(self):
        self.fields = [name for name in TASK_FIELDS.values() if name not in ADDED_TASK_FIELDS]
        self.records = [{"record_id": "rec-existing", "fields": {
            "任务ID": "ONB-1", "任务说明": "保留既有要求", "版本": "1",
            "发布状态": ["已发布"], "阶段": ["第一阶段"], "任务性质": ["每次接入执行"],
        }}]
        self.docs = {FEEDBACK_DOC: "<title>反馈</title><p>其他成员的反馈</p>",
                     **{token: "<title>接入</title><p>保留的要求</p>" for token in ONBOARDING_DOCS}}
        self.markdown = {"md-method": "# 方法\n\n保留上文\n旧规则\n保留下文\n"}
        self.folders = {
            RAG_FOLDER: [{"token": "md-method", "type": "file", "name": "method.md", "parent_token": RAG_FOLDER}],
            SKILL_FOLDER: [{"token": "sub-one", "type": "folder", "name": "Skill"}],
            "sub-one": [{"token": "sub-two", "type": "folder", "name": "References"}],
            "sub-two": [{"token": "sub-three", "type": "folder", "name": "Too deep"}],
        }
        self.calls = []
        self.timeout_after_write = False
        self.read_failure_after_write = False
        self.written = False
        self.override_document_after_write = None

    @staticmethod
    def arg(args, key, default=None):
        return args[args.index(key) + 1] if key in args else default

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        command = args[1:3]
        if self.read_failure_after_write and self.written and command in (["docs", "+fetch"], ["base", "+record-list"]):
            return subprocess.CompletedProcess(args, 1, "sensitive document contents", "OAuth credential secret")
        payload = {"ok": True}
        mutation = False
        if command == ["auth", "status"]:
            payload = {"verified": False}
        elif command == ["base", "+field-list"]:
            payload["data"] = {"fields": [{"name": name} for name in self.fields], "total": len(self.fields)}
        elif command == ["base", "+field-create"]:
            field = json.loads(self.arg(args, "--json"))
            self.fields.append(field["name"])
            mutation = True
        elif command == ["base", "+record-list"]:
            offset = int(self.arg(args, "--offset", "0"))
            limit = int(self.arg(args, "--limit", "200"))
            rows = self.records[offset:offset + limit]
            payload["data"] = {"fields": self.fields, "record_id_list": [row["record_id"] for row in rows],
                               "data": [[row["fields"].get(name) for name in self.fields] for row in rows],
                               "has_more": offset + limit < len(self.records)}
        elif command == ["base", "+record-upsert"]:
            record_id = self.arg(args, "--record-id")
            if record_id:
                row = next(row for row in self.records if row["record_id"] == record_id)
                row["fields"].update(json.loads(self.arg(args, "--json")))
            else:
                self.records.append({"record_id": f"rec-{len(self.records)}", "fields": json.loads(self.arg(args, "--json"))})
            mutation = True
        elif command == ["docs", "+fetch"]:
            token = self.arg(args, "--doc")
            payload["data"] = {"document": {"content": self.docs[token], "revision_id": 7}}
        elif command == ["docs", "+create"]:
            token = f"project-doc-{len(self.docs)}"
            folder = self.arg(args, "--folder-token")
            content = (Path(kwargs.get("cwd") or ".") / self.arg(args, "--markdown")[1:]).read_text()
            self.docs[token] = "<title>" + self.arg(args, "--title") + "</title><p>" + content + "</p>"
            self.folders[folder].append({"token": token, "type": "docx", "name": self.arg(args, "--title"), "parent_token": folder})
            mutation = True
        elif command == ["docs", "+update"]:
            token = self.arg(args, "--doc")
            markdown = (Path(kwargs.get("cwd") or ".") / self.arg(args, "--content")[1:]).read_text()
            if self.arg(args, "--command") == "append":
                self.docs[token] += "<p>" + markdown + "</p>"
            else:
                pattern = self.arg(args, "--pattern")
                self.docs[token] = re.sub(pattern, lambda match: markdown, self.docs[token], count=1)
            if self.override_document_after_write is not None:
                self.docs[token] = self.override_document_after_write
            mutation = True
        elif command == ["drive", "files"]:
            folder_token = json.loads(self.arg(args, "--params"))["folder_token"]
            payload = {"code": 0, "data": {"files": self.folders[folder_token], "has_more": False}}
        elif command == ["markdown", "+fetch"]:
            token = self.arg(args, "--file-token")
            payload["data"] = {"content": self.markdown[token], "file_name": "method.md"}
        elif command == ["markdown", "+overwrite"]:
            token = self.arg(args, "--file-token")
            self.markdown[token] = (Path(kwargs.get("cwd") or ".") / self.arg(args, "--content")[1:]).read_text()
            mutation = True
        else:
            raise AssertionError(f"Unexpected CLI operation {command}")
        if mutation:
            self.written = True
            if self.timeout_after_write:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    def writes(self, command):
        return [args for args, _ in self.calls if args[1:3] == command]


class CentralFeishuTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "BAITE_SELECTED_RAG_FOLDER_TOKEN": RAG_FOLDER,
            "BAITE_CENTRAL_BASE_TOKEN": BASE_TOKEN,
            "BAITE_CENTRAL_FEEDBACK_DOC": FEEDBACK_DOC,
            "BAITE_CENTRAL_SKILL_FOLDER_TOKEN": SKILL_FOLDER,
        })
        self.env.start()
        self.fake = FakeCli()
        self.cli = patch("server.feishu_tools.subprocess.run", side_effect=self.fake)
        self.cli.start()
        self.which = patch("server.feishu_tools.shutil.which", return_value="/test-only/lark-cli")
        self.which.start()
        self.tools = FeishuTools(executable="test-only-cli", timeout=999)

    def tearDown(self):
        self.cli.stop()
        self.which.stop()
        self.env.stop()

    def test_actual_tabular_shape_and_pagination_are_normalized(self):
        self.fake.fields.extend(ADDED_TASK_FIELDS)
        self.fake.records.extend({"record_id": f"rec-extra-{i}", "fields": {"任务ID": str(i), "适用成员": "xiaofei, duoduo"}}
                                 for i in range(201))
        result = self.tools.list_tasks()
        self.assertEqual(len(result["items"]), 202)
        self.assertEqual(result["items"][0]["status"], "已发布")
        self.assertEqual(result["items"][1]["member_ids"], ["xiaofei", "duoduo"])
        self.assertIn(BASE_TOKEN, result["source_url"])

    def test_ensure_fields_only_adds_missing_metadata(self):
        original = json.dumps(self.fake.records, ensure_ascii=False)
        result = self.tools.ensure_task_fields()
        self.assertEqual(result["created"], list(ADDED_TASK_FIELDS))
        self.assertEqual(json.dumps(self.fake.records, ensure_ascii=False), original)
        self.assertEqual(self.tools.ensure_task_fields()["created"], [])
        self.assertEqual(len(self.fake.writes(["base", "+field-create"])), 5)

    def test_task_is_reread_and_duplicate_trigger_does_not_create_again(self):
        self.tools.ensure_task_fields()
        fields = {"task_id": "RDOS-1", "instructions": "核实真实反馈", "version": "1",
                  "project_id": "rdos", "member_ids": ["duoduo"], "status": "已发布"}
        result = self.tools.save_task(fields, "run-1.task-1")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["task"]["member_ids"], ["duoduo"])
        self.assertEqual(self.tools.save_task(fields, "run-1.task-1")["status"], "duplicate")
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)
        with self.assertRaises(FeishuError) as caught:
            self.tools.save_task({**fields, "instructions": "其他正文"}, "run-1.task-1")
        self.assertEqual(caught.exception.code, "operation_conflict")

    def test_task_update_preserves_existing_body_and_version(self):
        self.tools.ensure_task_fields()
        self.tools.save_task({"status": "停用"}, "withdraw-1", "rec-existing")
        self.assertEqual(self.fake.records[0]["fields"]["任务说明"], "保留既有要求")
        self.assertEqual(self.fake.records[0]["fields"]["版本"], "1")

    def test_uncertain_task_write_checks_remote_without_second_mutation(self):
        self.tools.ensure_task_fields()
        self.fake.timeout_after_write = True
        result = self.tools.save_task({"task_id": "RDOS-2", "instructions": "真实任务"}, "unknown-write")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)

    def test_task_first_readback_old_snapshot_second_readback_verifies(self):
        self.tools.ensure_task_fields()
        read_tasks = self.tools.list_tasks
        before = read_tasks()
        reads = []
        def delayed_read():
            reads.append(True)
            return before if len(reads) <= 2 else read_tasks()
        with patch.object(self.tools, "list_tasks", side_effect=delayed_read), patch("server.feishu_tools.time.sleep") as sleep:
            result = self.tools.save_task({"task_id": "RDOS-READ-LAG", "instructions": "完整新正文"}, "read-lag")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(reads), 3)  # Before mutation, first stale read, corrected read.
        sleep.assert_called_once_with(0.5)
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)

    def test_task_first_readback_partial_fields_second_readback_verifies(self):
        self.tools.ensure_task_fields()
        read_tasks = self.tools.list_tasks
        reads = []
        def partial_read():
            result = read_tasks()
            reads.append(True)
            if len(reads) == 2:
                task = result["items"][-1]
                task["fields"]["任务说明"] = "旧正文"
            return result
        with patch.object(self.tools, "list_tasks", side_effect=partial_read), patch("server.feishu_tools.time.sleep"):
            result = self.tools.save_task({"task_id": "RDOS-PARTIAL", "instructions": "完整新正文"}, "partial-read")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(reads), 3)
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)

    def test_task_readback_stays_stale_is_failed_after_bounded_reads(self):
        self.tools.ensure_task_fields()
        before = self.tools.list_tasks()
        with patch.object(self.tools, "list_tasks", return_value=before) as reads, patch("server.feishu_tools.time.sleep") as sleep:
            with self.assertRaises(FeishuError) as caught:
                self.tools.save_task({"task_id": "RDOS-NEVER-VISIBLE", "instructions": "完整新正文"}, "never-visible")
        self.assertEqual(caught.exception.code, "write_unverified")
        self.assertEqual(reads.call_count, 4)  # One pre-write read plus three checks.
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1])
        self.assertEqual(len(self.fake.writes(["base", "+record-upsert"])), 1)

    def test_feedback_appends_preserving_other_members_and_has_remote_marker(self):
        original = self.fake.docs[FEEDBACK_DOC]
        result = self.tools.append_feedback("成员 duoduo 本次运行 role-test-1\n已经实际读取任务", "feedback-1")
        self.assertEqual(result["status"], "verified")
        self.assertTrue(self.fake.docs[FEEDBACK_DOC].startswith(original))
        self.assertEqual(self.tools.append_feedback("已经实际读取任务", "feedback-1")["status"], "duplicate")
        self.assertEqual(len(self.fake.writes(["docs", "+update"])), 1)
        with self.assertRaises(FeishuError) as caught:
            self.tools.append_feedback("同标识不同内容", "feedback-1")
        self.assertEqual(caught.exception.code, "operation_conflict")

    def test_feedback_timeout_after_commit_is_verified_by_readback(self):
        self.fake.timeout_after_write = True
        self.assertEqual(self.tools.append_feedback("真实反馈", "feedback-timeout")["status"], "verified")
        self.assertEqual(len(self.fake.writes(["docs", "+update"])), 1)

    def test_failed_readback_is_uncertain_and_never_claimed_success(self):
        self.fake.read_failure_after_write = True
        with self.assertRaises(FeishuError) as caught:
            self.tools.append_feedback("实际反馈", "feedback-read-fail")
        self.assertEqual(caught.exception.code, "write_uncertain")
        self.assertNotIn("secret", str(caught.exception))

    def test_mismatched_readback_is_not_success(self):
        self.fake.override_document_after_write = "<p>[RDOS 写回标识:bad-write]</p>"
        with self.assertRaises(FeishuError) as caught:
            self.tools.append_feedback("不能丢失的实际反馈", "bad-write")
        self.assertEqual(caught.exception.code, "write_unverified")

    def test_scope_blocks_arbitrary_docs_even_if_remote_account_can_read(self):
        with self.assertRaises(FeishuError) as caught:
            self.tools.read_source("private-unrelated-doc", "docx")
        self.assertEqual(caught.exception.code, "out_of_scope")
        self.assertFalse(any(args[1:3] == ["docs", "+fetch"] for args, _ in self.fake.calls))
        self.fake.calls.clear()
        with self.assertRaises(FeishuError):
            self.tools.save_task({"arbitrary_field": "value"}, "task-unknown-field")
        self.assertEqual(self.fake.calls, [])

    def test_folder_discovery_registers_children_and_bounds_depth(self):
        with self.assertRaises(FeishuError):
            self.tools.read_markdown("md-method")
        item = self.tools.list_folder(RAG_FOLDER)["items"][0]
        self.assertEqual(item["id"], "md-method")
        self.assertEqual(self.tools.read_markdown(item["id"])["title"], "method.md")
        for folder in (SKILL_FOLDER, "sub-one", "sub-two"):
            self.tools.list_folder(folder)
        with self.assertRaises(FeishuError) as caught:
            self.tools.list_folder("sub-three")
        self.assertEqual(caught.exception.code, "read_limit")

    def test_saved_target_is_rediscovered_inside_scoped_roots_after_restart(self):
        result = self.tools.read_source("md-method", "file")
        self.assertEqual(result["object_id"], "md-method")
        self.assertTrue(any(args[1:3] == ["drive", "files"] for args, _ in self.fake.calls))

    def test_registered_project_directory_and_feedback_use_trusted_scope(self):
        self.fake.folders["project-folder"] = [{"token": "project-material", "type": "docx", "name": "真实项目资料", "parent_token": "project-folder"}]
        self.fake.docs["project-material"] = "<title>项目资料</title><p>具体工作依据</p>"
        self.fake.docs["project-feedback"] = "<title>项目反馈</title><p>其他成员已有反馈</p>"
        tools = FeishuTools(executable="test-only-cli", sources=[{"id": "project-folder", "kind": "folder"}], feedback_doc="project-feedback")
        self.assertEqual(tools.read_registered_source("project-material", "docx")["title"], "项目资料")
        original = self.fake.docs[FEEDBACK_DOC]
        tools.append_feedback("本项目实际反馈", "project-feedback-op")
        self.assertIn("本项目实际反馈", self.fake.docs["project-feedback"])
        self.assertEqual(self.fake.docs[FEEDBACK_DOC], original)
        self.assertIn("其他成员已有反馈", self.fake.docs["project-feedback"])
        with self.assertRaises(FeishuError):
            tools.read_registered_source("another-project-document", "docx")

    def test_project_feedback_creation_verifies_folder_and_body_then_deduplicates(self):
        self.fake.folders["project-folder"] = []
        tools = FeishuTools(executable="test-only-cli", sources=[{"id": "project-folder", "kind": "folder"}])
        created = tools.ensure_project_feedback("project-folder", "实际项目 · 工作反馈", "project:project-1")
        self.assertEqual(created["status"], "verified")
        self.assertIn("[RDOS 项目反馈标识:project:project-1]", created["content"])
        self.assertIn(created["object_id"], [item["token"] for item in self.fake.folders["project-folder"]])
        self.fake.docs[created["object_id"]] += "<p>人工补充的工作内容</p>"
        duplicate = tools.ensure_project_feedback("project-folder", "实际项目 · 工作反馈", "project:project-1")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertIn("人工补充", duplicate["content"])
        self.assertEqual(len(self.fake.writes(["docs", "+create"])), 1)

    def test_project_feedback_link_keeps_its_folder_feishu_domain(self):
        self.fake.folders["project-folder"] = []
        tools = FeishuTools(executable="test-only-cli", sources=[{"id": "project-folder", "kind": "folder",
            "source_url": "https://shbebetter.feishu.cn/drive/folder/project-folder"}])
        created = tools.ensure_project_feedback("project-folder", "项目反馈", "project:alias-folder")
        self.assertTrue(created["source_url"].startswith("https://shbebetter.feishu.cn/docx/"))

    def test_registered_file_link_does_not_require_user_to_supply_markdown_filename(self):
        tools = FeishuTools(executable="test-only-cli", sources=[{"id": "md-method", "kind": "file", "name": "共享资料"}])
        result = tools.read_registered_source("md-method", "file")
        self.assertEqual(result["content"], self.fake.markdown["md-method"])

    def test_project_feedback_uncertain_creation_reconciles_without_second_write(self):
        self.fake.folders["project-folder"] = []
        tools = FeishuTools(executable="test-only-cli", sources=[{"id": "project-folder", "kind": "folder"}])
        self.fake.timeout_after_write = True
        result = tools.ensure_project_feedback("project-folder", "项目反馈", "project:timeout")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(tools.ensure_project_feedback("project-folder", "项目反馈", "project:timeout")["status"], "duplicate")
        self.assertEqual(len(self.fake.writes(["docs", "+create"])), 1)

    def test_project_feedback_unreadable_result_is_uncertain(self):
        self.fake.folders["project-folder"] = []
        tools = FeishuTools(executable="test-only-cli", sources=[{"id": "project-folder", "kind": "folder"}])
        self.fake.read_failure_after_write = True
        with self.assertRaises(FeishuError) as caught:
            tools.ensure_project_feedback("project-folder", "项目反馈", "project:unreadable")
        self.assertEqual(caught.exception.code, "write_uncertain")
        self.assertEqual(len(self.fake.writes(["docs", "+create"])), 1)

    def test_project_feedback_creation_rejects_unregistered_folder(self):
        with self.assertRaises(FeishuError) as caught:
            self.tools.ensure_project_feedback("unrelated-folder", "项目反馈", "project:denied")
        self.assertEqual(caught.exception.code, "out_of_scope")
        self.assertEqual(self.fake.calls, [])

    def test_work_task_has_no_onboarding_stage_and_old_task_kind_is_explicit(self):
        self.tools.ensure_task_fields()
        created = self.tools.save_task({"task_id": "PROJECT-WORK-1", "task_kind": "work", "stage": "第三阶段", "instructions": "真实工作"}, "work-1")
        self.assertEqual(created["task"]["task_kind"], "work")
        self.assertEqual(created["task"]["stage"], "")
        self.assertIsNone(created["task"]["fields"]["阶段"])
        payload = json.loads(self.fake.arg(self.fake.writes(["base", "+record-upsert"])[-1], "--json"))
        self.assertNotIn("阶段", payload)
        self.fake.records.extend([
            {"record_id": "legacy-onboarding", "fields": {"任务ID": "LOCAL-CODEX-REFRESH-01", "阶段": "第二阶段"}},
            {"record_id": "legacy-work", "fields": {"任务ID": "RDOS-TRIAL-01", "阶段": "第三阶段"}},
        ])
        tasks = {task["task_id"]: task for task in self.tools.list_tasks()["items"]}
        self.assertEqual(tasks["LOCAL-CODEX-REFRESH-01"]["task_kind"], "onboarding")
        self.assertEqual(tasks["LOCAL-CODEX-REFRESH-01"]["stage"], "第二阶段")
        self.assertEqual(tasks["RDOS-TRIAL-01"]["task_kind"], "work")
        self.assertEqual(tasks["RDOS-TRIAL-01"]["stage"], "")
        self.assertEqual(tasks["RDOS-TRIAL-01"]["fields"]["阶段"], "第三阶段")

    def test_only_existing_onboarding_converted_to_work_clears_select_with_null(self):
        self.tools.ensure_task_fields()
        original = dict(self.fake.records[0]["fields"])
        result = self.tools.save_task({"task_kind": "work", "stage": "第三阶段"}, "convert-onboarding", "rec-existing")
        self.assertIsNone(result["task"]["fields"]["阶段"])
        self.assertEqual(result["task"]["stage"], "")
        for name, value in original.items():
            if name != "阶段":
                self.assertEqual(result["task"]["fields"][name], value)
        self.tools.save_task({"task_kind": "work", "stage": "第一阶段"}, "already-work", "rec-existing")
        payload = json.loads(self.fake.arg(self.fake.writes(["base", "+record-upsert"])[-1], "--json"))
        self.assertNotIn("阶段", payload)

    def test_markdown_patch_changes_only_selected_text_and_is_deduplicated(self):
        self.tools.list_folder(RAG_FOLDER)
        before = self.tools.read_markdown("md-method")
        result = self.tools.publish_patch("file", "md-method", before["content_hash"], "旧规则", "新规则", "method-1")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["content"], before["content"].replace("旧规则", "新规则\n\n[RDOS 写回标识:method-1]"))
        self.assertEqual(self.tools.publish_patch("file", "md-method", before["content_hash"], "旧规则", "新规则", "method-1")["status"], "duplicate")
        self.assertEqual(len(self.fake.writes(["markdown", "+overwrite"])), 1)

    def test_stale_method_source_or_ambiguous_patch_does_not_write(self):
        self.tools.list_folder(RAG_FOLDER)
        before = self.tools.read_markdown("md-method")
        self.fake.markdown["md-method"] += "人工改稿\n"
        with self.assertRaises(FeishuError) as caught:
            self.tools.publish_patch("file", "md-method", before["content_hash"], "旧规则", "新规则", "method-old")
        self.assertEqual(caught.exception.code, "source_changed")
        before = self.tools.read_markdown("md-method")
        with self.assertRaises(FeishuError) as caught:
            self.tools.publish_patch("file", "md-method", before["content_hash"], "不存在", "新规则", "method-missing")
        self.assertEqual(caught.exception.code, "patch_conflict")
        self.assertEqual(self.fake.writes(["markdown", "+overwrite"]), [])

    def test_document_patch_is_local_and_preserves_surrounding_content(self):
        token = ONBOARDING_DOCS[0]
        before = self.tools.read_document(token)
        after = self.tools.publish_patch("docx", token, before["content_hash"], "保留的要求", "新的要求", "doc-patch")
        self.assertEqual(after["status"], "verified")
        self.assertIn("<title>接入</title>", after["content"])
        self.assertIn("新的要求", after["content"])

    def test_confirmed_append_patch_preserves_all_existing_document_content(self):
        token = ONBOARDING_DOCS[0]
        before = self.tools.read_document(token)
        after = self.tools.publish_patch("docx", token, before["content_hash"], "", "新增接入说明", "doc-append")
        self.assertEqual(after["status"], "verified")
        self.assertTrue(after["content"].startswith(before["content"]))
        with self.assertRaises(FeishuError) as caught:
            self.tools.publish_patch("docx", token, before["content_hash"], "", "不同说明", "doc-append")
        self.assertEqual(caught.exception.code, "operation_conflict")

    def test_subprocess_is_bounded_and_no_shell_or_raw_errors_are_exposed(self):
        self.tools.read_document(FEEDBACK_DOC)
        args, kwargs = self.fake.calls[0]
        self.assertIsInstance(args, list)
        self.assertEqual(kwargs["timeout"], 45)
        self.assertNotIn("shell", kwargs)
        with patch("server.feishu_tools.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "sensitive正文", "OAuth secret-token")):
            with self.assertRaises(FeishuError) as caught:
                self.tools.read_document(FEEDBACK_DOC)
        self.assertEqual(caught.exception.code, "authorization_failed")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

    def test_expired_read_revalidates_existing_grant_and_retries_once(self):
        calls = []

        def expired_then_verified(args, **options):
            calls.append(args)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 1, "", '{"error":{"message":"user OAuth token expired"}}')
            if args[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(args, 0, '{"verified":true,"scope":"existing-scopes-only"}', "")
            return self.fake(args, **options)

        with patch("server.feishu_tools.subprocess.run", side_effect=expired_then_verified):
            document = self.tools.read_document(FEEDBACK_DOC)
        self.assertEqual(document["title"], "反馈")
        self.assertEqual([args[1:3] for args in calls], [["docs", "+fetch"], ["auth", "status"], ["docs", "+fetch"]])
        self.assertEqual(calls[1][1:], ["auth", "status", "--verify"])
        self.assertEqual(calls[0], calls[2])

    def test_failed_auth_verification_keeps_read_failed_without_retry(self):
        calls = []

        def invalid_grant(args, **options):
            calls.append(args)
            if args[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(args, 0, '{"verified":false,"userName":"private-name"}', "")
            return subprocess.CompletedProcess(args, 1, "", "OAuth token expired sensitive-secret")

        with patch("server.feishu_tools.subprocess.run", side_effect=invalid_grant):
            with self.assertRaises(FeishuError) as caught:
                self.tools.read_document(FEEDBACK_DOC)
        self.assertEqual(caught.exception.code, "authorization_failed")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("private-name", str(caught.exception))
        self.assertNotIn("sensitive-secret", str(caught.exception))

    def test_verified_grant_does_not_loop_when_retried_read_is_still_denied(self):
        calls = []

        def still_denied(args, **options):
            calls.append(args)
            if args[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(args, 0, '{"verified":true}', "")
            return subprocess.CompletedProcess(args, 1, "", "permission denied")

        with patch("server.feishu_tools.subprocess.run", side_effect=still_denied):
            with self.assertRaises(FeishuError) as caught:
                self.tools.list_tasks()
        self.assertEqual(caught.exception.code, "authorization_failed")
        self.assertEqual([args[1:3] for args in calls], [["base", "+record-list"], ["auth", "status"], ["base", "+record-list"]])

    def test_mutation_auth_error_is_not_refreshed_or_blindly_retried(self):
        calls = []

        def mutation_denied(args, **options):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "OAuth token expired")

        with patch("server.feishu_tools.subprocess.run", side_effect=mutation_denied):
            with self.assertRaises(FeishuError) as caught:
                self.tools._run(["base", "+record-upsert"], "task-table")
        self.assertEqual(caught.exception.code, "authorization_failed")
        self.assertEqual(len(calls), 1)

    def test_native_inline_link_preserves_adjacent_punctuation(self):
        plain = "任务来源https://example.test/table；原说明保留。"
        rendered = '任务来源<a href="https://example.test/table">https://example.test/table</a>；原说明保留。'
        self.assertEqual(_plain(plain), _plain(rendered))

    def test_doc_write_uses_current_v2_contract_and_relative_file(self):
        self.tools.append_feedback("实际反馈", "v2-contract")
        args, options = next((args, options) for args, options in self.fake.calls if args[1:3] == ["docs", "+update"])
        self.assertEqual(self.fake.arg(args, "--command"), "append")
        self.assertEqual(self.fake.arg(args, "--doc-format"), "markdown")
        self.assertEqual(self.fake.arg(args, "--revision-id"), "7")
        self.assertEqual(self.fake.arg(args, "--content"), "@content.md")
        self.assertTrue(options.get("cwd"))
        self.assertNotIn("--mode", args)

    def test_field_read_lag_does_not_repeat_creation(self):
        real_read = self.tools._field_names
        delayed = False

        def read():
            nonlocal delayed
            names = real_read()
            if "项目ID" in names and not delayed:
                delayed = True
                return names - {"项目ID"}
            return names

        with patch.object(self.tools, "_field_names", side_effect=read):
            self.tools.ensure_task_fields()
        self.assertEqual(self.fake.fields.count("项目ID"), 1)
        self.assertEqual(len(self.fake.writes(["base", "+field-create"])), 5)


if __name__ == "__main__":
    unittest.main()
