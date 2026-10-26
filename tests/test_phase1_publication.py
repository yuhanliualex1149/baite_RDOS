"""Approval and version checks on real proposal publication, under the guard."""
from __future__ import annotations

import unittest

from tests.phase1_support import run_guarded


SETUP = '''
from server import central_service as service, central_templates as templates, phase1_runtime as runtime
runtime.initialize()
run = {"id": "publication-run", "operation_id": "publication-run", "project_id": project_id, "identity_mode": "role_test"}

async def template_proposal():
    current = templates.snapshot("project.instructions")
    old = "读取项目背景、资料和适用于本人的当前任务。"
    new = "读取合成项目背景、资料和适用于本人的当前测试任务。"
    proposal = await service.create_proposal({"target_id": "project.instructions", "target_kind": "template",
        "old_text": old, "new_text": new, "summary": "仅修改合成测试措辞", "evidence": "本地测试确认模板版本边界"}, run)
    return current, proposal, old, new
'''


class Phase1PublicationTest(unittest.TestCase):
    def check(self, body):
        result = run_guarded(SETUP + "\n" + body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_template_denies_no_grant_then_exact_confirmation_activates_once(self):
        self.check('''
async def scenario():
    original, proposal, old, new = await template_proposal()
    try:
        templates.apply_approved("project.instructions", original["content_hash"], old, new,
                                 "no-confirmation", admin["id"])
    except PermissionError:
        pass
    else:
        raise AssertionError("direct template mutation had no confirmation")
    assert templates.snapshot("project.instructions") == original
    assert not store.items("template_activation")
    try:
        await service.publish_proposal(proposal["id"], "stale-hash", "stale-confirmation", admin)
    except ValueError:
        pass
    else:
        raise AssertionError("stale draft accepted")
    assert not store.get("operation", "stale-confirmation")
    result = await service.publish_proposal(proposal["id"], proposal["draft_hash"], "template-confirmation", admin)
    assert result["status"] == "published"
    current = templates.snapshot("project.instructions")
    assert current["version"] == original["version"] + 1 and new in current["content"]
    again = await service.publish_proposal(proposal["id"], proposal["draft_hash"], "another-dialog-id", admin)
    assert again["publication_operation_id"] == "template-confirmation"
    assert templates.snapshot("project.instructions") == current
    assert len(store.items("template_activation")) == 1
    op = store.get("operation", "template-confirmation")
    assert op["status"] == "succeeded" and op["authorization"]["target_kind"] == "template"
    assert op["authorization"]["base"]["version"] == original["version"]
asyncio.run(scenario())
''')

    def test_equal_template_body_with_new_version_rejects_old_confirmation(self):
        self.check('''
async def scenario():
    original, proposal, old, new = await template_proposal()
    # Fixture models another approved revision returning to the same text.
    store.put("instruction_template", "project.instructions", {"id": "project.instructions",
        "content": original["content"], "version": original["version"] + 2})
    current = templates.snapshot("project.instructions")
    assert current["content_hash"] == original["content_hash"]
    try:
        await service.publish_proposal(proposal["id"], proposal["draft_hash"], "stale-base-version", admin)
    except ValueError as exc:
        assert "版本" in str(exc)
    else:
        raise AssertionError("same text bypassed version approval")
    assert templates.snapshot("project.instructions") == current
    assert not store.items("template_activation")
    op = store.get("operation", "stale-base-version")
    assert op["status"] == "failed" and not op.get("dispatched_at")
asyncio.run(scenario())
''')

    def test_document_proposal_lost_response_reconciles_without_second_write(self):
        self.check('''
async def scenario():
    source = next(s for s in store.items("source") if s.get("project_id") == project_id and s.get("watch"))
    original = store.get("lab_remote_document", source["id"])
    old = original["content"].splitlines()[2]
    proposal = await service.create_proposal({"target_id": source["id"], "target_kind": "docx", "old_text": old,
        "new_text": old + " 本次仅增加合成核对说明。", "summary": "合成资料补充", "evidence": "本地测试草稿审批证据"}, run)
    backend.set_fault(project_id, "write_document_after_commit", code="timeout")
    backend.set_fault(project_id, "read_document_after_write", code="timeout")
    try:
        await service.publish_proposal(proposal["id"], proposal["draft_hash"], "document-confirmation", admin)
    except Exception:
        assert store.get("operation", "document-confirmation")["status"] == "uncertain"
    else:
        raise AssertionError("synthetic lost response was not exercised")
    assert backend.stats(project_id).get("document_writes", 0) == 1
    result = await service.publish_proposal(proposal["id"], proposal["draft_hash"], "new-browser-attempt", admin)
    assert result["status"] == "published" and result["publication_operation_id"] == "document-confirmation"
    op = store.get("operation", "document-confirmation")
    assert op["status"] == "succeeded" and op["authorization"]["target_id"] == source["id"]
    assert [a["mode"] for a in op["attempts"]] == ["write", "reconcile"]
    assert backend.stats(project_id).get("document_writes", 0) == 1
asyncio.run(scenario())
''')


if __name__ == "__main__":
    unittest.main()
