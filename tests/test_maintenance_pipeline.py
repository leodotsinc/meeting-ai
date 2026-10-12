import copy
from datetime import timedelta
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

import test_maintenance_receiver as fixtures

spec=importlib.util.spec_from_file_location('pipeline',fixtures.ROOT/'scripts/maintenance-pipeline.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)


class PipelineTests(fixtures.ReceiverTests):
    # Reuse source-proof fixtures; run only these flow tests in this subclass.
    def setUp(self):
        super().setUp();self.journal=self.root/'attempt.json';self.merged='e'*40;self.mutations=[]
        original=self.api.get
        def get(path):
            if '/actions/artifacts?name=' in path:
                return {'total_count':1,'artifacts':[{'name':'meeting-ai-maintenance-attempt-'+self.request['request_id'],
                    'expired':False,'workflow_run':{'id':456,'head_sha':self.base}}]}
            if path.endswith('/commits/'+self.merged):
                return {'sha':self.merged,'parents':[{'sha':self.base},{'sha':self.head}],
                    'commit':{'tree':{'sha':self.request['source_pr']['tree_sha']}}}
            if path.endswith('/commits/main') and self.mutations:return {'sha':self.merged}
            return original(path)
        self.api.get=get
        def merge_once(number,head):
            self.assertEqual((number,head),(12,self.head))
            self.assertEqual(p.gate.decode(self.journal.read_text())['state'],'merge_pending_reconciliation')
            self.mutations.append(head);return {'merged':True,'sha':self.merged}
        self.api.merge_once=merge_once
        self.admission={'status':'source_ready','request_id':self.request['request_id'],
            'source_artifact':{'id':19,'digest':self.api.artifact['digest']},'delta_sha256':'d'*64,'evidence_run_id':123}
    def run_merge(self,**overrides):
        options={'clock':lambda:fixtures.NOW,'admit':lambda *args,**kwargs:self.admission,'host_ready':lambda:None}
        options.update(overrides)
        return p.merge(self.request,self.config,self.env,self.event,self.api,self.root,self.journal,**options)
    def test_host_absent_is_explicit_and_precedes_mutation(self):
        with patch.dict(self.config,{'host_contract_sha256':None}),self.assertRaisesRegex(ValueError,'HOST_CONTRACT_NOT_REGISTERED'):
            self.run_merge()
        self.assertFalse(self.journal.exists());self.assertEqual(self.mutations,[])
    def test_single_merge_is_durable_and_context_keeps_all_authority_references(self):
        admission,sha=self.run_merge();context=p.context(self.request,admission,sha,self.env)
        self.assertEqual(context['producer_commit'],self.base);self.assertEqual(context['head_sha'],self.head)
        self.assertEqual(context['merged_sha'],self.merged);self.assertEqual(context['source_proof'],self.request['source_proof'])
        self.assertEqual(context['base_manifest'],self.manifest);self.assertEqual(context['release_run_id'],456)
        self.assertEqual(p.gate.decode(self.journal.read_text())['state'],'merged_pending_build')
        with self.assertRaisesRegex(ValueError,'MERGE_ATTEMPT_REQUIRES_RECONCILIATION'):self.run_merge()
        self.assertEqual(len(self.mutations),1)
    def test_unknown_result_keeps_pending_and_never_retries(self):
        def timeout(*args):self.mutations.append(self.head);raise TimeoutError('synthetic private text')
        self.api.merge_once=timeout
        with self.assertRaisesRegex(ValueError,'MERGE_RESULT_UNKNOWN_RECONCILE'):self.run_merge()
        self.assertEqual(p.gate.decode(self.journal.read_text())['state'],'merge_pending_reconciliation')
        with self.assertRaisesRegex(ValueError,'MERGE_ATTEMPT_REQUIRES_RECONCILIATION'):self.run_merge()
        self.assertEqual(len(self.mutations),1)
    def test_reservation_from_other_run_or_window_expiry_never_merges(self):
        original=self.api.get
        def other(path):
            result=original(path)
            if '/actions/artifacts?name=' in path:result['artifacts'][0]['workflow_run']['id']=455
            return result
        with patch.object(self.api,'get',side_effect=other),self.assertRaisesRegex(ValueError,'MERGE_ATTEMPT_REQUIRES_RECONCILIATION'):self.run_merge()
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_RELEASE_WINDOW'):
            self.run_merge(clock=lambda:fixtures.NOW+timedelta(minutes=31))
        self.assertEqual(self.mutations,[])
    def test_postmerge_parent_tree_main_or_context_drift_is_not_authority(self):
        commit={'sha':self.merged,'parents':[{'sha':self.base},{'sha':self.head}],
            'commit':{'tree':{'sha':self.request['source_pr']['tree_sha']}}}
        for key,value in [('parents',[{'sha':self.head},{'sha':self.base}]),('commit',{'tree':{'sha':'f'*40}})]:
            with self.subTest(key=key),patch.dict(commit,{key:value}),self.assertRaises(ValueError):
                p.merged_identity(commit,{'sha':self.merged},self.request)
        context=p.context(self.request,self.admission,self.merged,self.env)
        def admit(*args,**kwargs):
            self.assertEqual(kwargs['merged_sha'],self.merged);return self.admission
        expected=p.preflight(self.request,context,self.config,self.env,self.event,self.api,self.root,
            clock=lambda:fixtures.NOW,admit=admit,host_ready=lambda:None)
        self.assertEqual(expected,context)
        for key,value in [('infra_commit','1'*40),('release_run_id',457),('delta_sha256','e'*64),('base_manifest',{})]:
            with self.subTest(key=key),patch.dict(context,{key:value}),self.assertRaisesRegex(ValueError,'CONTEXT_DRIFT'):
                p.preflight(self.request,context,self.config,self.env,self.event,self.api,self.root,
                    clock=lambda:fixtures.NOW,admit=admit,host_ready=lambda:None)
    def test_atomic_terminal_write_failure_preserves_pending(self):
        with patch.object(p.os,'replace',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.run_merge()
        self.assertEqual(p.gate.decode(self.journal.read_text())['state'],'merge_pending_reconciliation')
        self.assertEqual(len(self.mutations),1)
    def test_request_references_cannot_substitute_another_ci_or_config(self):
        for key,value in [('config_sha256','f'*64),('infra_commit','main'),('source_proof',dict(self.request['source_proof'],run_id=124))]:
            with self.subTest(key=key),patch.dict(self.request,{key:value}),self.assertRaises(ValueError):self.observe()

# Fixtures inherit test methods for convenience but are exercised once by their
# own test module; this module's suite adds only the flow-specific cases.
for name in fixtures.ReceiverTests.__dict__:
    if name.startswith('test_') and name not in PipelineTests.__dict__:setattr(PipelineTests,name,None)

if __name__=='__main__':unittest.main()
