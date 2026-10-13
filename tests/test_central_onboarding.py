"""Instruction access and onboarding evidence; external services explicitly stubbed."""
import logging
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_central_api import CentralApiTest
from server import central_onboarding as onb


class OnboardingTest(CentralApiTest):
    XIAOFEI_RECORD = """连接检查：潇斐／第 0 步
记录类型：connection_check
成员：潇斐
成员ID：mem_x_rLn9VBAe_z8xol
身份标记：role_test
本次运行标识：onb_ROizsqieaZZ1rNrs
接入阶段：0
接入路径：refresh
反馈ID：onb_ROizsqieaZZ1rNrs-stage0-20261012-143143
实际结果：复用现有飞书 CLI 用户连接；已读取指定本步材料（版本 6）；已读取指定反馈文档（写前版本 36），未发现本轮记录；追加本条最小连接检查。
尚未验证：后续接入阶段；角色独立飞书账号授权。本次 role_test 仅证明 Agent 借当前已授权用户连接执行。
[RDOS 写回标识:onb_ROizsqieaZZ1rNrs-stage0-20261012-143143]
读取来源：https://scn33386s7ui.feishu.cn/docx/A1FrdvwproG7axxh6jtcOnk5nPd
连接测试结果：通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。
检查时间：2026-10-12 14:31:43 +08:00
"""

    def begin(self, path="new", **options):
        response = self.client.post("/api/central/onboarding/start", json={"path": path, **options})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def record(self, state, stage=0, success=True, member=None):
        feedback_id = f"{state['session_id']}-stage{stage}-test"
        return {"content": f"""## 成员标识 {member or self.admin['id']} 本次运行标识 {state['session_id']}
### 连接检查
记录类型：{'connection_check' if stage == 0 else 'onboarding'}
成员ID：{member or self.admin['id']}
本次运行标识：{state['session_id']}
接入阶段：{stage}
接入路径：{state['choice']}
反馈ID：{feedback_id}
实际结果：已读取指定本步材料；已追加本条最小连接检查，并从线上读回核对正文及写回标识。
读取来源：{self.store.FEISHU_HOST}/docx/{self.store.ONBOARDING_IDS[0]}
连接测试结果：{'通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。' if success else '未通过，写入失败。'}
[RDOS 写回标识:{feedback_id}]
""", "source_url": self.store.FEISHU_HOST + "/docx/" + self.store.FEEDBACK_ID}

    def test_untitled_original_record_passes_only_its_current_step(self):
        document = {"content": self.XIAOFEI_RECORD, "source_url":
                    self.store.FEISHU_HOST + "/docx/" + self.store.FEEDBACK_ID}
        user = {"id": "mem_x_rLn9VBAe_z8xol", "identity_mode": "role_test"}
        item = {"run_label": "onb_ROizsqieaZZ1rNrs", "stage": 0, "path": "refresh"}
        matched = onb.matching_sections(document, user, item)
        self.assertEqual(matched, [self.XIAOFEI_RECORD.strip()])
        outcome = asyncio.run(onb.assess(user, item, matched, document))
        self.assertTrue(outcome["ready"])
        self.assertEqual(outcome["missing"], [])
        self.assertIn("独立账号授权", outcome["verification_scope"])

    def test_adjacent_untitled_records_cannot_lend_each_other_evidence(self):
        state = self.begin()
        current = self.record(state)["content"].replace("## 成员标识 " + self.admin["id"] +
            " 本次运行标识 " + state["session_id"] + "\n### 连接检查\n", "连接检查：本轮\n")
        current = current.replace("连接测试结果：通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。", "")
        current = current.replace("并从线上读回核对正文及写回标识。", "。")
        other = self.record(state, member=self.researcher["id"])["content"].replace(
            "## 成员标识 " + self.researcher["id"] + " 本次运行标识 " + state["session_id"] +
            "\n### 连接检查\n", "连接检查：另一成员\n")
        document = {"content": current + "\n" + other,
                    "source_url": self.store.FEISHU_HOST + "/docx/" + self.store.FEEDBACK_ID}
        sections = onb.matching_sections(document, self.admin, {"run_label": state["session_id"],
                                                              "stage": 0, "path": "new"})
        self.assertEqual(len(sections), 1)
        self.assertFalse(asyncio.run(onb.assess(self.admin, {"stage": 0, "path": "new"}, sections, document))["ready"])

    def test_marker_identity_and_remote_document_must_match(self):
        state = self.begin()
        item = {"run_label": state["session_id"], "stage": 0, "path": "new"}
        document = self.record(state)
        document["content"] = document["content"].replace(
            f"[RDOS 写回标识:{state['session_id']}-stage0-test]", "[RDOS 写回标识:another-feedback]")
        self.assertEqual(onb.matching_sections(document, self.admin, item), [])
        document["source_url"] = self.store.FEISHU_HOST + "/docx/unrelated"
        with self.assertRaisesRegex(ValueError, "其他资料"):
            onb.matching_sections(document, self.admin, item)

    def test_bare_pass_and_planned_readback_never_pass(self):
        state = self.begin()
        item = {"run_label": state["session_id"], "stage": 0, "path": "new"}
        document = self.record(state)
        document["content"] = document["content"].replace(
            "实际结果：已读取指定本步材料；已追加本条最小连接检查，并从线上读回核对正文及写回标识。",
            "实际结果：计划读取、写入和读回。").replace(
            "连接测试结果：通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。",
            "连接测试结果：通过")
        sections = onb.matching_sections(document, self.admin, item)
        self.assertFalse(asyncio.run(onb.assess(self.admin, item, sections, document))["ready"])

    def test_model_check_cannot_count_feedback_readback_as_stage_material_read(self):
        section = ("实际结果：已写入指定反馈文档并从线上读回。\n"
                   "连接测试结果：通过。已写入指定反馈文档并从线上读回。")
        with patch.object(onb, "CentralAgent") as agent:
            self.assertFalse(asyncio.run(onb._connection_model_check(section, 5)))
            agent.assert_not_called()
        denied = ("实际结果：没有读取阶段一材料，已写入指定反馈文档并从线上读回。\n"
                  "连接测试结果：通过。")
        self.assertTrue(onb._connection_evidence(denied)["failed_or_planned"])
        self.assertFalse(asyncio.run(onb._connection_model_check(denied, 5)))

    def test_connection_read_must_name_onboarding_material_not_feedback_document(self):
        state = self.begin()
        original = self.record(state)
        section = onb.matching_sections(original, self.admin, {
            "run_label": state["session_id"], "stage": 0, "path": "new"})[0]
        self.assertTrue(onb.connection_result_passed(section))

        # 读取来源 still points at stage one, but the reported action only
        # read the feedback document. It cannot prove the stage material read.
        changed = dict(original)
        changed["content"] = changed["content"].replace(
            "已读取指定本步材料", "已读取本步反馈文档").replace(
            "已真实读取本步材料", "已读取本步反馈文档")
        sections = onb.matching_sections(changed, self.admin, {
            "run_label": state["session_id"], "stage": 0, "path": "new"})
        self.assertEqual(len(sections), 1)
        self.assertFalse(onb.connection_result_passed(sections[0]))
        with patch.object(onb, "CentralAgent") as model:
            result = asyncio.run(onb.assess(self.admin, {"stage": 0, "path": "new"}, sections, changed))
            model.assert_not_called()
        self.assertFalse(result["ready"])
        self.assertIn("本步接入材料", result["missing"][0])

        unusual = "实际结果：阶段一反馈文档我已看过；反馈已写入并从线上回读。\n连接测试结果：通过。"
        with patch.object(onb, "CentralAgent") as model:
            self.assertFalse(asyncio.run(onb._connection_model_check(unusual, 5)))
            model.assert_not_called()

    def test_model_check_accepts_cited_unusual_material_read_wording(self):
        actual = "阶段一原文我已看过；反馈已写入并从线上回读。"
        section = f"实际结果：{actual}\n连接测试结果：通过。"
        with patch.object(onb, "CentralAgent") as agent:
            agent.return_value.max_seconds = 5
            agent.return_value.run = AsyncMock(return_value={"content": json.dumps({
                "read": "阶段一原文我已看过", "write": "反馈已写入", "readback": "从线上回读"
            }, ensure_ascii=False)})
            self.assertTrue(asyncio.run(onb._connection_model_check(section, 5)))

    def test_reusing_multiple_old_runs_shares_one_check_budget(self):
        old = [{"id": self.admin["id"], "run_label": f"old-{index}",
                "identity_mode": self.admin["identity_mode"], "path": "refresh",
                "history": [{"stage": 0}, {"stage": 1}]} for index in range(2)]
        budgets = []

        async def not_ready(_user, _item, _scope, _document, budget):
            budgets.append(budget)
            await asyncio.sleep(0.02)
            return {"ready": False}

        with (patch.object(onb.store, "items", return_value=old),
              patch.object(onb, "matching_sections", return_value=["record"]),
              patch.object(onb, "connection_result_passed", return_value=True),
              patch.object(onb, "assess", side_effect=not_ready)):
            outcome = asyncio.run(onb.reusable_history({"content": ""}, self.admin,
                {"path": "refresh", "stage": 0, "run_label": "current"}, 0.2))
        self.assertIsNone(outcome)
        self.assertEqual(len(budgets), 2)
        self.assertLess(budgets[1], budgets[0] - 0.01)

    def test_older_check_result_cannot_replace_advanced_step(self):
        state = self.begin()
        payload = onb.CheckContext(session_id=state["session_id"], expected_stage=0)
        claimed, token, _ = onb._claim_check(self.admin["id"], payload)
        advanced = self.store.get("onboarding_session", self.admin["id"])
        advanced.update(stage=1, checking=False, check_token="", summary="新版步骤")
        self.store.put("onboarding_session", self.admin["id"], advanced)
        self.assertFalse(onb._finish_check(self.admin["id"], claimed, token,
                                          {**claimed, "stage": 3, "summary": "旧响应"}, 0))
        self.assertEqual(self.store.get("onboarding_session", self.admin["id"])["summary"], "新版步骤")
        response = self.client.post("/api/central/onboarding/check", json={
            "session_id": state["session_id"], "expected_stage": 0})
        self.assertEqual(response.status_code, 409)

    def test_interrupted_check_can_resume_after_restart_without_old_result_winning(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(checking=True, check_token="old-check", check_boot="previous-process",
                    check_started_at=self.store.future(-10))
        self.store.put("onboarding_session", self.admin["id"], item)
        self.assertFalse(self.client.get("/api/central/onboarding/status").json()["checking"])
        claimed, token, _ = onb._claim_check(self.admin["id"], onb.CheckContext(
            session_id=state["session_id"], expected_stage=0))
        self.assertNotEqual(token, "old-check")
        self.assertFalse(onb._finish_check(self.admin["id"], claimed, "old-check",
                                          {**claimed, "summary": "旧检查"}, 0))
        self.assertTrue(onb._finish_check(self.admin["id"], claimed, token,
                                         {**claimed, "summary": "已恢复检查"}, 0))
        self.assertEqual(self.store.get("onboarding_session", self.admin["id"])["summary"], "已恢复检查")

    def test_institution_read_scope_does_not_depend_on_project_feedback(self):
        self.store.put("project", self.store.PROJECT_ID, {
            "id": self.store.PROJECT_ID, "name": "RDOS 自身改造",
            "feedback_id": "project-only-feedback", "member_ids": [self.admin["id"]]})
        with patch("server.feishu_tools.FeishuTools") as factory:
            onb.organization_feishu()
        settings = factory.call_args.kwargs
        self.assertEqual(settings["feedback_doc"], self.store.FEEDBACK_ID)
        self.assertEqual({source["id"] for source in settings["sources"]}, set(self.store.ONBOARDING_IDS))
        self.assertNotIn("project-only-feedback", {source["id"] for source in settings["sources"]})

    def test_all_roles_choose_path_and_get_own_instruction(self):
        for name, password in [("admin", "AdminPassword123!"), ("researcher", "MemberPassword123!"), ("dual", "MemberPassword123!")]:
            self.login(name, password)
            state = self.client.get("/api/central/onboarding/status").json()
            self.assertIsNone(state["choice"])
            self.assertEqual(self.client.post("/api/central/onboarding/instruction", json={}).status_code, 409)
            self.begin()
            link = self.client.post("/api/central/onboarding/instruction", json={}).json()
            self.assertNotIn("成员ID", link["prompt"])
            self.assertIn("http://testserver/api/central/instructions/", link["prompt"])
            self.client.post("/api/central/auth/logout")
            result = self.client.get(link["url"])
            self.assertEqual(result.status_code, 200)
            self.assertIn("no-store", result.headers["cache-control"])
            self.assertIn("成员ID：", result.text)
            self.assertNotIn("password", result.text)

    def test_researcher_status_has_current_action_without_admin_history(self):
        self.login("researcher")
        state = self.begin()
        self.assertEqual(state["stage"], 0)
        self.assertIn("next_action", state)
        self.assertNotIn("history", state)
        self.assertNotIn("technical_detail", state)
        self.assertNotIn("sources", state)
        self.login("dual")
        dual = self.begin()
        self.assertIn("history", dual)
        self.assertIn("technical_detail", dual)

    def test_instruction_is_limited_expiring_and_not_logged(self):
        self.begin()
        link = self.client.post("/api/central/onboarding/instruction", json={}).json()
        entry = self.store.items("instruction")[0]
        self.assertNotIn("token", entry)
        self.assertEqual(self.client.get(link["url"] + "wrong").status_code, 404)
        entry["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.store.put("instruction", entry["id"], entry)
        self.assertEqual(self.client.get(link["url"]).status_code, 404)
        log = logging.LogRecord("uvicorn.access", 20, "", 0, 'GET %s', (link["url"],), None)
        self.assertFalse(onb.PrivateInstructionLogFilter().filter(log))

    def test_personal_instruction_follows_latest_same_step_missing_items(self):
        self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=1, summary="原范围确认仍需说明", missing=["说明本人此前确认的来源"],
                    history=[{"stage": 0, "summary": "指定连接检查已完成", "raw_memory": "private-memory-body"},
                             {"stage": 2, "summary": "future-stage-private-result"}])
        self.store.put("onboarding_session", item["id"], item)
        self.store.put("onboarding_session", self.researcher["id"], {"summary": "other-member-private-result"})
        link = self.client.post("/api/central/onboarding/instruction", json={}).json()
        body = self.client.get(link["url"]).text
        self.assertIn("说明本人此前确认的来源", body)
        self.assertNotIn("private-memory-body", body)
        self.assertNotIn("future-stage-private-result", body)
        self.assertNotIn("other-member-private-result", body)
        item.update(summary="持久保存已核对，只缺本人范围", missing=["仅补本人范围的最终确认"])
        self.store.put("onboarding_session", item["id"], item)
        updated = self.client.get(link["url"]).text
        self.assertIn("仅补本人范围的最终确认", updated)
        self.assertNotIn("说明本人此前确认的来源", updated)
        self.assertIn(item["run_label"], updated)
        # An old-step capability stops working when the member advances.
        item.update(stage=3, summary="next-step-private-result", missing=["next-step-private-missing"])
        self.store.put("onboarding_session", item["id"], item)
        old_step = self.client.get(link["url"])
        self.assertEqual(old_step.status_code, 410)
        self.assertNotIn("next-step-private-result", old_step.text)
        self.assertNotIn("next-step-private-missing", old_step.text)

    def test_connection_instruction_does_not_include_work_assessment_or_history(self):
        self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(summary="personal-work-summary", missing=["personal-work-missing"],
                    history=[{"stage": 0, "summary": "personal-work-history"}])
        self.store.put("onboarding_session", item["id"], item)
        link = self.client.post("/api/central/onboarding/instruction", json={}).json()
        body = self.client.get(link["url"]).text
        for value in ("personal-work-summary", "personal-work-history"):
            self.assertNotIn(value, body)
        self.assertIn("personal-work-missing", body)
        self.assertIn("步骤：0", body)
        self.assertIn("连接检查", body)
        self.assertNotIn("npx @larksuite/cli", body)
        self.assertNotIn(self.store.BASE_ID, body)

    def test_onboarding_has_no_project_or_task_snapshot_and_later_steps_skip_installation(self):
        self.login("researcher")
        state = self.begin()
        saved = self.store.get("onboarding_session", self.researcher["id"])
        self.assertFalse(saved.get("project_id"))
        for stage, expected_doc in [(1, self.store.ONBOARDING_IDS[0]), (3, self.store.ONBOARDING_IDS[2])]:
            with self.subTest(stage=stage):
                saved["stage"] = stage
                self.store.put("onboarding_session", saved["id"], saved)
                link = self.client.post("/api/central/onboarding/instruction", json={}).json()
                body = self.client.get(link["url"]).text
                self.assertIn(expected_doc, body)
                self.assertIn(state["session_id"], body)
                self.assertNotIn("rdos-self", body)
                self.assertNotIn(self.store.BASE_ID, body)
                self.assertNotIn("npx @larksuite/cli", body)
                self.assertNotIn("当前可用任务快照", body)
                self.assertNotIn("lark-cli auth status", body)
                for unrelated in set(self.store.ONBOARDING_IDS) - {expected_doc}:
                    self.assertNotIn(unrelated, body)

    def test_project_instruction_keeps_its_own_project_assignment(self):
        project = {"id": "research-project", "name": "研究项目", "member_ids": [self.researcher["id"]],
                   "feedback_id": "research-feedback", "folder_url": self.store.FEISHU_HOST + "/drive/folder/research", "status": "active"}
        self.store.put("project", project["id"], project)
        self.login("researcher")
        self.begin()
        link = self.client.post("/api/central/onboarding/instruction", json={
            "purpose": "project", "project_id": project["id"]}).json()
        body = self.client.get(link["url"]).text
        self.assertIn("请打开这份百特项目说明", link["prompt"])
        self.assertIn(project["id"], body)
        self.assertIn(project["feedback_id"], body)
        self.assertIn(self.store.BASE_ID, body)
        self.assertNotIn(self.store.FEEDBACK_ID, body)
        self.assertNotIn("接入阶段：不适用", body)

    def test_old_run_link_cannot_reopen_after_explicit_restart(self):
        first = self.begin()
        old_link = self.client.post("/api/central/onboarding/instruction", json={}).json()["url"]
        # A repeated visit is idempotent; an explicit restart begins a new run.
        self.assertEqual(self.begin()["session_id"], first["session_id"])
        second = self.begin("new", restart=True)
        self.assertNotEqual(second["session_id"], first["session_id"])
        self.assertIsNotNone(self.store.get("onboarding_history", first["session_id"]))
        self.assertEqual(self.client.get(old_link).status_code, 410)

    def test_connection_readback_advances_without_manual_technical_form(self):
        state = self.begin()
        remote = MagicMock()
        remote.read_document.return_value = self.record(state)
        with patch.object(onb, "organization_feishu", return_value=remote):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(result["stage"], 1)
        self.assertFalse(result["ready_for_work"])
        self.assertIn("下一步", result["summary"])
        self.assertEqual(len(result["history"]), 1)

    def test_connection_pass_with_explanation_requires_completed_readback(self):
        state = self.begin()
        remote = MagicMock()
        documented = self.record(state)
        documented["content"] = documented["content"].replace(
            "连接测试结果：通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。",
            "连接测试结果：通过（已读取、追加，最终以实际读回核对为准）。").replace(
            "实际结果：已读取指定本步材料；已追加本条最小连接检查，并从线上读回核对正文及写回标识。",
            "实际结果：已读取指定本步材料；已追加本条最小连接检查。")
        remote.read_document.return_value = documented
        with patch.object(onb, "organization_feishu", return_value=remote):
            incomplete = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(incomplete["stage"], 0)
        self.assertIn("读回", " ".join(incomplete["missing"]))
        documented["content"] = documented["content"].replace(
            "实际结果：已读取指定本步材料；已追加本条最小连接检查。",
            "实际结果：已读取指定本步材料；已追加本条最小连接检查；写后已读回指定反馈，核对本条写回标识。")
        documented["content"] = documented["content"].replace(
            "连接测试结果：通过（已读取、追加，最终以实际读回核对为准）。",
            "连接测试结果：通过。已读取、追加并从线上读回核对。")
        with patch.object(onb, "organization_feishu", return_value=remote):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(result["stage"], 1)
        for outcome in ("未通过", "通过（写入失败）", "通过（未读回）"):
            with self.subTest(outcome=outcome):
                self.begin("new", restart=True)
                current = self.client.get("/api/central/onboarding/status").json()
                remote.read_document.return_value = self.record(current)
                remote.read_document.return_value["content"] = remote.read_document.return_value["content"].replace(
                    "连接测试结果：通过。已真实读取本步材料、追加本条反馈并从线上读回核对正文及写回标识。", "连接测试结果：" + outcome)
                with patch.object(onb, "organization_feishu", return_value=remote):
                    checked = self.client.post("/api/central/onboarding/check", json={}).json()
                self.assertEqual(checked["stage"], 0)

    def test_wrong_member_stage_and_failed_connection_do_not_advance(self):
        state = self.begin()
        for entry in [self.record(state, member=self.researcher["id"]), self.record(state, stage=1), self.record(state, success=False)]:
            remote = MagicMock()
            remote.read_document.return_value = entry
            with patch.object(onb, "organization_feishu", return_value=remote):
                result = self.client.post("/api/central/onboarding/check", json={}).json()
            self.assertEqual(result["stage"], 0)
            self.assertFalse(result["ready_for_work"])

    def test_stage_three_manual_ready_does_not_wait_for_scheduled_trigger(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item["stage"] = 3
        self.store.put("onboarding_session", item["id"], item)
        remote = MagicMock()
        remote.read_document.return_value = self.record(state, stage=3)
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(return_value={"ready": True, "summary": "配置和手动结果已核对", "missing": [], "scheduled_verified": False})):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertTrue(result["ready_for_work"])
        self.assertFalse(result["scheduled_verified"])
        self.assertIn("项目", result["next_action"])

    def test_new_access_advances_from_scope_directly_to_schedule(self):
        state = self.begin()
        remote = MagicMock()
        remote.read_document.return_value = self.record(state)
        with patch.object(onb, "organization_feishu", return_value=remote):
            connected = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(connected["stage"], 1)
        scope = self.record(state, stage=1)
        scope["content"] = scope["content"].replace("记录类型：connection_check", "记录类型：onboarding")
        remote.read_document.return_value = scope
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(return_value={
            "ready": True, "summary": "本人范围和持续记忆已核对", "missing": [], "scheduled_verified": False})):
            scoped = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(scoped["stage"], 3)
        self.assertEqual([entry["stage"] for entry in scoped["history"]], [0, 1])
        self.assertFalse(scoped["ready_for_work"])
        self.assertFalse(any(entry["stage"] == 2 for entry in scoped["history"]))

    def test_legacy_stage_two_stays_in_history_without_becoming_new_access_gate(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=2, history=[{"stage": 0, "summary": "连接已核对"},
                                      {"stage": 1, "summary": "范围已核对"}])
        self.store.put("onboarding_session", item["id"], item)
        status = self.client.get("/api/central/onboarding/status").json()
        self.assertEqual(status["session_id"], state["session_id"])
        self.assertEqual(status["stage"], 3)
        # The old stage remains reconstructable, while a fresh run does not require it.
        self.begin("new", restart=True)
        archived = self.store.get("onboarding_history", state["session_id"])
        self.assertEqual([entry["stage"] for entry in archived["history"]], [0, 1])
        self.assertEqual(archived["stage"], 3)

    def test_withdrawn_old_onboarding_task_does_not_erase_verified_history(self):
        self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=3, history=[{"stage": 0, "summary": "连接已核对"},
                                      {"stage": 1, "summary": "范围已核对"},
                                      {"stage": 2, "summary": "旧任务当时已完成"}])
        self.store.put("onboarding_session", item["id"], item)
        self.store.put("task", "ONB-REVIEW-01", {"task_id": "ONB-REVIEW-01", "status": "停用", "version": "2"})
        current = self.client.get("/api/central/onboarding/status").json()
        self.assertEqual([entry["stage"] for entry in current["history"]], [0, 1, 2])
        self.assertEqual(current["stage"], 3)

    def test_switching_path_preserves_history_and_other_user_cannot_supply_identity(self):
        first = self.begin()
        second = self.begin("refresh")
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertIsNotNone(self.store.get("onboarding_history", first["session_id"]))
        self.assertEqual(self.client.post("/api/central/onboarding/start", json={"path": "new", "member_id": self.dual["id"]}).status_code, 422)

    def test_refresh_reuses_verified_connection_and_unchanged_scope(self):
        old = self.begin()
        remote = MagicMock()
        connection = self.record(old)["content"]
        remote.read_document.return_value = {"content": connection,
            "source_url": self.store.FEISHU_HOST + "/docx/" + self.store.FEEDBACK_ID}
        with patch.object(onb, "organization_feishu", return_value=remote):
            connected = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(connected["stage"], 1)
        scope = self.record(old, stage=1)["content"].replace(
            "记录类型：connection_check", "记录类型：onboarding").replace(
            "### 连接检查", "### 身份与范围")
        scope += "本人确认：身份、工作范围与共享范围沿用；持久记忆已保存并读回。\n"
        remote.read_document.return_value = {"content": connection + "\n" + scope,
            "source_url": self.store.FEISHU_HOST + "/docx/" + self.store.FEEDBACK_ID}
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(return_value={
            "ready": True, "summary": "身份与共享范围已核对", "missing": [], "scheduled_verified": False})):
            scoped = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(scoped["stage"], 3)
        # A refresh should not demand another write to the same connection-test position.
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(return_value={
            "ready": True, "summary": "旧轮范围仍适用", "missing": [], "scheduled_verified": False})):
            refreshed = self.begin("refresh")
            if refreshed["stage"] == 0:
                refreshed = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertNotEqual(refreshed["session_id"], old["session_id"])
        self.assertEqual(refreshed["stage"], 3)
        self.assertFalse(refreshed["ready_for_work"])

    def test_refresh_does_not_reuse_local_history_when_remote_evidence_is_missing(self):
        old = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item["history"] = [{"stage": 0, "summary": "过去核对过连接"},
                           {"stage": 1, "summary": "过去核对过范围"}]
        self.store.put("onboarding_session", item["id"], item)
        refreshed = self.begin("refresh")
        self.assertNotEqual(refreshed["session_id"], old["session_id"])
        remote = MagicMock()
        remote.read_document.return_value = {"content": "", "source_url": self.store.FEISHU_HOST + "/docx/" + self.store.FEEDBACK_ID}
        with patch.object(onb, "organization_feishu", return_value=remote):
            checked = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(checked["stage"], 0)
        self.assertFalse(checked["ready_for_work"])

    def test_read_or_model_failure_keeps_previous_evidence(self):
        state = self.begin()
        remote = MagicMock()
        remote.read_document.side_effect = ValueError("授权失效，请重新登录")
        with patch.object(onb, "organization_feishu", return_value=remote):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(result["stage"], 0)
        self.assertIn("授权失效", result["error"])
        self.assertFalse(result["checking"])

    def test_read_failure_keeps_last_assessment_and_missing_items(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=1, stage_status="blocked", summary="仍需本人确认工作范围",
                    missing=["请补充本人本次确认的来源"], checked_hash="last-successful-read",
                    history=[{"stage": 0, "summary": "连接已核对"}])
        self.store.put("onboarding_session", item["id"], item)
        remote = MagicMock()
        remote.read_document.side_effect = ValueError("飞书暂时无法读取")
        with patch.object(onb, "organization_feishu", return_value=remote):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        remote.read_document.assert_called_once_with(self.store.FEEDBACK_ID)
        self.assertEqual(result["session_id"], state["session_id"])
        self.assertEqual(result["stage"], 1)
        self.assertEqual(result["stage_status"], "blocked")
        self.assertEqual(result["summary"], "仍需本人确认工作范围")
        self.assertEqual(result["missing"], ["请补充本人本次确认的来源"])
        self.assertIn("飞书暂时无法读取", result["error"])
        self.assertEqual(self.store.get("onboarding_session", item["id"])["checked_hash"], "last-successful-read")

    def test_model_failure_keeps_verified_connection_and_does_not_advance_scope(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=1, history=[{"stage": 0, "summary": "连接已核对"}])
        self.store.put("onboarding_session", item["id"], item)
        scope = self.record(state, stage=1)
        scope["content"] = scope["content"].replace("记录类型：connection_check", "记录类型：onboarding")
        remote = MagicMock()
        remote.read_document.return_value = scope
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(side_effect=ValueError("模型当前不可用"))):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(result["stage"], 1)
        self.assertFalse(result["ready_for_work"])
        self.assertEqual([entry["stage"] for entry in result["history"]], [0])
        self.assertTrue(result["error"])

    def test_parse_failure_is_not_authorization_and_recovers_without_losing_record(self):
        state = self.begin()
        remote = MagicMock()
        remote.read_document.side_effect = json.JSONDecodeError("Expecting value", "invalid", 0)
        with patch.object(onb, "organization_feishu", return_value=remote):
            failed = self.client.post("/api/central/onboarding/check", json={}).json()
            self.assertEqual(failed["stage"], 0)
            self.assertFalse(failed["ready_for_work"])
            self.assertIn("暂时无法解析检查结果", failed["error"])
            self.assertNotIn("授权", failed["error"])
            self.assertIn("Expecting value", failed["technical_detail"])
            remote.read_document.side_effect = None
            remote.read_document.return_value = self.record(state)
            recovered = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(recovered["stage"], 1)
        self.assertEqual(recovered["error"], "")
        self.assertEqual(recovered["technical_detail"], "")

    def test_same_feedback_recovers_after_read_failure(self):
        state = self.begin()
        remote = MagicMock()
        remote.read_document.return_value = self.record(state, success=False)
        with patch.object(onb, "organization_feishu", return_value=remote):
            self.client.post("/api/central/onboarding/check", json={})
            remote.read_document.side_effect = ValueError("授权失效")
            self.client.post("/api/central/onboarding/check", json={})
            remote.read_document.side_effect = None
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(result["error"], "")
        self.assertIn("可核对", result["summary"])

    def test_model_cannot_pass_with_missing_requirements_or_bad_types(self):
        remote = MagicMock()
        remote.read_document.return_value = {"content": "真实材料", "source_url": "https://example.com/source"}
        remote.list_tasks.return_value = {"items": []}
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "CentralAgent") as agent:
            agent.return_value.max_seconds = 180
            body = {"ready": True, "summary": "仍缺核对", "missing": ["新会话恢复"], "scheduled_verified": False}
            agent.return_value.run = AsyncMock(return_value={"content": json.dumps(body)})
            result = asyncio.run(onb.assess(self.admin, {"stage": 3, "path": "new"}, ["实际反馈"], {"source_url": "https://example.com/feedback"}))
            self.assertFalse(result["ready"])
            body["scheduled_verified"] = "false"
            agent.return_value.run.return_value = {"content": json.dumps(body)}
            with self.assertRaises(ValueError):
                asyncio.run(onb.assess(self.admin, {"stage": 3, "path": "new"}, ["实际反馈"], {"source_url": "https://example.com/feedback"}))

    def task_feedback(self, version="2", task_id="RDOS-CURRENT", combined=False):
        reference = f"任务 ID 与版本：{task_id} @ v{version}" if combined else f"任务ID：{task_id}\n任务版本：{version}"
        return f"""### 接入反馈
记录类型：onboarding
接入阶段：2
{reference}
实际结果：保留真实成果，仍按接入要求核对。
[RDOS 写回标识:review-{version}]
"""

    def assess_task_feedback(self, sections, tasks):
        remote = MagicMock()
        remote.read_document.return_value = {"content": "真实材料", "source_url": "https://example.com/source"}
        remote.list_tasks.return_value = {"items": tasks}
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "CentralAgent") as factory:
            agent = factory.return_value
            agent.max_seconds = 180
            agent.run = AsyncMock(return_value={"content": json.dumps({"ready": True, "summary": "核对通过", "missing": [], "scheduled_verified": False})})
            result = asyncio.run(onb.assess(self.admin, {"stage": 2, "path": "new", "run_label": "review-run"}, sections,
                {"source_url": "https://example.com/feedback"}))
            return result, agent.run.await_count

    def current_task(self, **changes):
        return {"task_id": "RDOS-CURRENT", "version": "2", "status": "已发布", "task_kind": "work",
                "member_ids": [self.admin["id"]], "project_id": self.store.PROJECT_ID, **changes}

    def test_explicit_task_version_and_assignment_cannot_be_overridden_by_model(self):
        cases = [
            (self.task_feedback("1"), [self.current_task()], "版本 1"),
            (self.task_feedback("3"), [self.current_task()], "版本 3"),
            (self.task_feedback(""), [self.current_task()], "缺少任务版本"),
            (self.task_feedback(), [self.current_task(member_ids=[self.researcher["id"]])], "不适用于本人"),
            (self.task_feedback(), [self.current_task(status="停用")], "已停用"),
            (self.task_feedback(), [], "不存在"),
            (self.task_feedback(), [self.current_task(), self.current_task()], "ID重复"),
            (self.task_feedback("1", combined=True), [self.current_task()], "版本 1"),
        ]
        for section, tasks, message in cases:
            with self.subTest(message=message, section=section):
                result, calls = self.assess_task_feedback([section], tasks)
                self.assertFalse(result["ready"])
                self.assertIn(message, " ".join(result["missing"]))
                self.assertEqual(calls, 0)

    def test_latest_task_correction_is_checked_and_old_history_is_preserved(self):
        old, current = self.task_feedback("1"), self.task_feedback("2", combined=True)
        result, calls = self.assess_task_feedback([old, current], [self.current_task()])
        self.assertTrue(result["ready"])
        self.assertEqual(calls, 1)
        result, calls = self.assess_task_feedback([current, old], [self.current_task()])
        self.assertFalse(result["ready"])
        self.assertEqual(calls, 0)

    def test_actual_task_reference_requires_missing_version_before_model_review(self):
        for label in ("实际任务ID与版本", "实际任务 ID 与版本"):
            with self.subTest(label=label):
                section = "### 接入反馈\n记录类型：onboarding\n接入阶段：2\n" + label + "：LOCAL-DEEPSEEK-ONB-01（按成员指令执行，版本未能在线核实）"
                result, calls = self.assess_task_feedback([section], [self.current_task(task_id="LOCAL-DEEPSEEK-ONB-01")])
                self.assertFalse(result["ready"])
                self.assertIn("LOCAL-DEEPSEEK-ONB-01", " ".join(result["missing"]))
                self.assertIn("缺少任务版本", " ".join(result["missing"]))
                self.assertEqual(calls, 0)

    def test_onboarding_without_business_task_still_uses_model(self):
        result, calls = self.assess_task_feedback(["""### 接入反馈
记录类型：onboarding
接入阶段：2
实际结果：已有完整接入材料，未关联独立业务任务。
[RDOS 写回标识:review-no-task]
"""], [])
        self.assertTrue(result["ready"])
        self.assertEqual(calls, 1)

    def test_unambiguous_annotated_task_version_is_accepted(self):
        section = self.task_feedback("2（已发布；补充说明）")
        result, calls = self.assess_task_feedback([section], [self.current_task()])
        self.assertTrue(result["ready"])
        self.assertEqual(calls, 1)

    def test_ambiguous_task_version_requests_a_specific_correction(self):
        section = self.task_feedback("2 或 3，尚未核实")
        result, calls = self.assess_task_feedback([section], [self.current_task()])
        self.assertFalse(result["ready"])
        self.assertIn("版本", " ".join(result["missing"]))
        self.assertEqual(calls, 0)

    def test_stage_judge_receives_current_scope_and_actual_remote_evidence(self):
        for stage in (1, 3):
            with self.subTest(stage=stage):
                section = f"""### 接入反馈
记录类型：onboarding
成员ID：{self.admin['id']}
本次运行标识：scope-review
接入阶段：{stage}
实际结果：已保存持久memory入口并实际读回；工作及共享范围仍待本人确认。
[RDOS 写回标识:scope-stage-{stage}]
"""
                document = {"content": section, "source_url": "https://example.com/feedback", "object_id": "feedback-real",
                            "revision": 19, "content_hash": "observed-content-hash"}
                remote = MagicMock()
                remote.read_document.return_value = {"content": "当前正式原文", "source_url": "https://example.com/stage"}
                remote.list_tasks.return_value = {"items": []}
                with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "CentralAgent") as factory:
                    agent = factory.return_value
                    agent.max_seconds = 180
                    agent.run = AsyncMock(return_value={"content": json.dumps({"ready": True, "summary": "范围尚未确认",
                        "missing": ["本人工作及共享范围尚未确认"], "scheduled_verified": False})})
                    result = asyncio.run(onb.assess(self.admin, {"stage": stage, "path": "new", "run_label": "scope-review"}, [section], document))
                    messages = agent.run.await_args.args[0]
                prompt = messages[0]["content"]
                context = json.loads(messages[1]["content"])
                self.assertIn("远端", prompt)
                self.assertNotIn("本次核对第二阶段：", prompt)
                self.assertIn("本人", prompt)
                evidence = context["remote_feedback_evidence"]
                self.assertTrue(evidence["central_readback"])
                self.assertEqual(evidence["revision"], 19)
                self.assertEqual(evidence["object_id"], "feedback-real")
                self.assertEqual(evidence["content_hash"], "observed-content-hash")
                self.assertEqual(evidence["writeback_markers"], [f"[RDOS 写回标识:scope-stage-{stage}]"])
                self.assertEqual(context["feedback"], [section])
                self.assertFalse(result["ready"])
                self.assertEqual(result["missing"], ["本人工作及共享范围尚未确认"])

    def test_updated_judgment_rules_reassess_same_feedback_once(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=1, stage_status="blocked", assessment_version="old-rules",
                    summary="旧核验误要求全新会话", missing=["全新会话自动加载"])
        document = self.record(state, stage=1)
        document["content"] = document["content"].replace("记录类型：connection_check", "记录类型：onboarding")
        item["checked_hash"] = self.store.digest(onb.matching_sections(document, self.admin, item))
        self.store.put("onboarding_session", item["id"], item)
        remote = MagicMock()
        remote.read_document.return_value = document
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(return_value={
            "ready": False, "summary": "按当前阶段重评，范围尚未确认", "missing": ["本人范围确认"], "scheduled_verified": False})) as judge:
            first = self.client.post("/api/central/onboarding/check", json={}).json()
            second = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertEqual(judge.await_count, 1)
        self.assertEqual(first["stage"], 1)
        self.assertEqual(second["missing"], ["本人范围确认"])
        self.assertNotIn("全新会话", second["summary"])
        saved = self.store.get("onboarding_session", self.admin["id"])
        self.assertEqual(saved["assessment_version"], onb.ASSESSMENT_VERSION)

    def test_newer_schedule_evidence_can_reopen_a_previously_completed_step(self):
        state = self.begin()
        item = self.store.get("onboarding_session", self.admin["id"])
        item.update(stage=3, ready_for_work=True, stage_status="verified", assessment_version="old-rules",
                    history=[{"stage": 0}, {"stage": 1}, {"stage": 3}])
        self.store.put("onboarding_session", item["id"], item)
        document = self.record(state, stage=3)
        document["content"] = document["content"].replace("记录类型：connection_check", "记录类型：onboarding")
        remote = MagicMock()
        remote.read_document.return_value = document
        with patch.object(onb, "organization_feishu", return_value=remote), patch.object(onb, "assess", AsyncMock(return_value={
            "ready": False, "summary": "现有计划仍只筛第三阶段", "missing": ["更新每日有效筛选"],
            "scheduled_verified": False})):
            result = self.client.post("/api/central/onboarding/check", json={}).json()
        self.assertFalse(result["ready_for_work"])
        self.assertEqual(result["completed_steps"], [0, 1])
        self.assertEqual(result["stage"], 3)
        self.assertIn("更新每日有效筛选", result["missing"])
        self.assertEqual(len(self.store.get("onboarding_session", self.admin["id"])["history"]), 3)
