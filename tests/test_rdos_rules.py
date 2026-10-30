"""Portable business rules, including retry decisions independent of transport."""
import copy
import unittest
from server import rdos_rules as rules


class RulesTest(unittest.TestCase):
    def task(self, **changes):
        return {'task_id':'task-a','goal':'完成配置','owner_id':'owner','deliverable':'配置结果',
                'executor_node_ids':['node-a'],'requirements_version':1,'record_revision':1,
                'task_type':'action','completion_rule':{'kind':'receipt_recorded'},
                'business_status':'active',**changes}

    def test_stops_and_version_changes_do_not_reuse_stale_work(self):
        checkpoint={'task_id':'task-a','requirements_version':1,'completed_steps':['artifact_verified']}
        ready=rules.execution_plan(self.task(),'node-a',checkpoint)
        self.assertEqual(ready['reason'],'submit_existing_result')
        newer=rules.execution_plan(self.task(requirements_version=2),'node-a',checkpoint)
        self.assertTrue(newer['changed_requirements'])
        self.assertEqual(newer['reusable_steps'],[])
        self.assertIn('perform_work',newer['allowed_actions'])
        for status,ack in [('paused','report_paused_ack'),('closed','report_closed_ack')]:
            plan=rules.execution_plan(self.task(business_status=status),'node-a',checkpoint)
            self.assertIn(ack,plan['allowed_actions'])
            self.assertNotIn('perform_work',plan['allowed_actions'])
        self.assertEqual(rules.execution_plan(self.task(),'other')['allowed_actions'],[])

    def test_uncertain_write_and_verified_receipt_choose_recovery_not_repeat(self):
        known={'task_id':'task-a','requirements_version':1,'uncertain_operations':['artifact-op']}
        self.assertEqual(rules.execution_plan(self.task(),'node-a',known)['reason'],'reconcile_before_retry')
        known['uncertain_operations']=[]
        known['completed_steps']=['submission_verified']
        self.assertEqual(rules.execution_plan(self.task(),'node-a',known)['allowed_actions'],['read_status','correct_previous_record'])
        missing=rules.execution_plan(self.task(owner_id=''),'node-a')
        self.assertEqual(missing['missing_conditions'],['owner_id'])
        self.assertEqual(missing['allowed_actions'],['report_blocker'])

    def test_draft_allows_deliberate_revision_and_reporting_before_submission(self):
        known={'task_id':'task-a','requirements_version':1,'completed_steps':['artifact_verified']}
        actions=rules.execution_plan(self.task(task_type='deliverable'),'node-a',known)['allowed_actions']
        self.assertTrue({'revise_existing_result','report_progress','report_blocker'} <= set(actions))
        self.assertNotIn('perform_work',actions)
        self.assertIn('report_progress',rules.execution_plan(self.task(),'node-a')['allowed_actions'])
        known['completed_steps'].append('submission_verified')
        submitted=rules.execution_plan(self.task(task_type='deliverable'),'node-a',known)['allowed_actions']
        self.assertNotIn('revise_existing_result',submitted)
        for state in ('paused','closed'):
            stopped=rules.execution_plan(self.task(business_status=state),'node-a',known)['allowed_actions']
            self.assertNotIn('revise_existing_result',stopped)
            self.assertNotIn('report_progress',stopped)

    def test_structured_result_cannot_be_replaced_by_prose_or_wrong_task_type(self):
        event={'event_type':'submission','body':'已完成'}
        with self.assertRaises(rules.RuleViolation):rules.validate_result(event,self.task())
        event['result_status']='completed'
        self.assertEqual(rules.validate_result(event,self.task()),'completed')
        with self.assertRaises(rules.RuleViolation):rules.validate_result(event,self.task(task_type='deliverable'))
        self.assertEqual(rules.validate_result(event,self.task(task_type='review')),'completed')
        review={**event,'event_type':'review'}
        self.assertEqual(rules.validate_result(review,self.task(task_type='review',input_refs=[{'document_id':'input'}])),'completed')

    def test_legacy_requirements_keep_identical_keys_and_new_fields_change_fingerprint(self):
        legacy={key:None for key in rules.LEGACY_TASK_FIELDS}
        self.assertEqual(rules.requirements(legacy),legacy)
        current={**legacy,'task_type':'action','deliverable_audience':['admin']}
        self.assertEqual(set(rules.requirements(current)),set(legacy)|{'task_type','deliverable_audience'})
        changed=copy.deepcopy(current);changed['deliverable_audience']=['project_members']
        self.assertNotEqual(rules.digest(rules.requirements(current)),rules.digest(rules.requirements(changed)))

    def test_opinions_use_ordinary_results_and_legacy_review_is_readable(self):
        first={'document_id':'first','revision':1,'content_hash':'a'}
        second={'document_id':'second','revision':2,'content_hash':'b'}
        task=self.task(task_type='action',input_refs=[first,second])
        event={'event_type':'submission','body':'审阅意见','result_status':'completed'}
        self.assertEqual(rules.validate_result(event,task),'completed')
        self.assertEqual(rules.task_type(self.task(task_type='review')),'action')
        self.assertEqual(rules.completion_kind(self.task(completion_rule={'kind':'human_review'})),'receipt_recorded')
        self.assertNotIn('review',rules.TASK_TYPES)
        self.assertFalse({'human','human_review'} & set(rules.COMPLETION_KINDS))


if __name__=='__main__':unittest.main()
