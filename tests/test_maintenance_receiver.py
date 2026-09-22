import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import test_maintenance_proof as fixtures
from test_maintenance_proof import API as SourceAPI, ROOT
sys.path.insert(0,str(ROOT/"scripts"))
from release_manifest import create, record_deployment

spec=importlib.util.spec_from_file_location('receiver',ROOT/'scripts/maintenance-receiver.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
NOW=datetime(2026,10,3,13,tzinfo=timezone.utc)


class API(SourceAPI):
    def __init__(self,test):
        super().__init__(test); self.snapshots={sha:r.gate.collect(test.root,sha) for sha in (test.base,test.head)}
        self.run={'id':123,'run_attempt':1,'head_sha':test.head,'event':'pull_request','status':'completed',
            'conclusion':'success','path':'.github/workflows/ci.yml','head_repository':{'id':r.source.REPOSITORY_ID},
            'updated_at':NOW.isoformat()}
        self.jobs={'total_count':3,'jobs':[{'name':name,'status':'completed','conclusion':'success',
            'run_id':123,'run_attempt':1,'head_sha':test.head} for name in sorted(r.JOBS)]}
        self.pack()
    def pack(self, name='source-qualification.json', extra=False):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr(name,json.dumps(self.test.proof))
            if extra:z.writestr('extra.json','{}')
        self.raw=stream.getvalue()
        self.artifact={'id':19,'name':'meeting-ai-source-qualification-123-1','expired':False,
            'size_in_bytes':len(self.raw),'digest':'sha256:'+hashlib.sha256(self.raw).hexdigest(),
            'workflow_run':{'id':123,'head_sha':self.test.head,'repository_id':r.source.REPOSITORY_ID,
                            'head_repository_id':r.source.REPOSITORY_ID}}
    def get(self,path):
        if '/git/commits/' in path:
            self.calls.append(path); sha=path.rsplit('/',1)[1]
            return {'sha':sha,'tree':{'sha':self.snapshots[sha]['tree']}}
        if '/git/trees/' in path:
            self.calls.append(path); sha=path.rsplit('/',1)[1].split('?')[0]
            snapshot=next(s for s in self.snapshots.values() if s['tree']==sha)
            return {'sha':sha,'truncated':False,'tree':[{'type':'blob','path':p,'mode':v['mode'],'sha':v['oid']}
                for p,v in snapshot['files'].items()]}
        if '/actions/workflows/ci.yml/runs?' in path:
            self.calls.append(path);return {'total_count':1,'workflow_runs':[copy.deepcopy(self.run)]}
        if path.endswith('/attempts/1/jobs?per_page=100'):
            self.calls.append(path);return self.jobs
        if path.endswith('/artifacts?per_page=100'):
            self.calls.append(path);return {'total_count':1,'artifacts':[self.artifact]}
        if path.endswith('/actions/runs/123'):
            self.calls.append(path);return self.run
        value=super().get(path)
        if '/pulls/' in path:
            for side in ('head','base'):value[side]['repo']['id']=r.source.REPOSITORY_ID
        return value
    def artifact_bytes(self,metadata):
        self.calls.append('artifact:19');return self.raw


class ReceiverTests(unittest.TestCase):
    git=fixtures.ProofTests.git
    def setUp(self):
        fixtures.ProofTests.setUp(self)
        self.proof=r.source.emit_source(self.api,self.root,self.env,self.event,NOW,
            baseline_lookup=lambda *_:{'commit':self.base,'tag':'v0.1.5','published_manifest_sha256':'a'*64},clock=lambda:NOW)
        self.assertEqual(self.proof['status'],'passed',self.proof.get('code'))
        built=create(service='meeting-ai',compose_project='meeting-ai',application_kind='first_party',
            app_version='0.1.5',release_version='0.1.5',git_sha=self.base,
            source_repository='https://github.com/'+r.REPO,image='ghcr.io/leodotsinc/meeting-ai@sha256:'+'d'*64,
            build_id='100',build_attempt=1,built_at='2026-09-20T03:00:00Z')
        self.manifest=record_deployment(built,status='verified',deployed_at='2026-09-20T03:01:00Z',
            verified_at='2026-09-20T03:02:00Z',observed_image=built['image'])
        self.baseline={'commit':self.base,'tag':'v0.1.5','published_manifest_sha256':'a'*64,'manifest':self.manifest}
        self.config=json.loads((ROOT/'.github/maintenance.json').read_text())
        self.config.update(enabled=True,host_qualification_sha256='b'*64,policy_sha256='c'*64,
            trusted_code={p:hashlib.sha256((self.root/p).read_bytes()).hexdigest() for p in r.source.CODE})
        self.env={'GITHUB_REPOSITORY':r.REPO,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
            'GITHUB_RUN_ATTEMPT':'1','GITHUB_RUN_ID':'456','GITHUB_SHA':self.base,'GITHUB_ACTOR_ID':'332011818',
            'GITHUB_ACTOR':'cloudbox-maintenance-scheduler[bot]','GITHUB_TRIGGERING_ACTOR':'cloudbox-maintenance-scheduler[bot]'}
        self.event={'sender':{'id':332011818,'type':'Bot'}}
        baseline={'git_sha':self.base,'image':self.manifest['image'],'receipt_sha256':r.gate.sha256(self.manifest)}
        self.request={'schema_version':1,'service':'meeting-ai','phase':'prepare',
            'request_id':r.gate.sha256({'app':'meeting-ai','baseline_receipt_sha256':baseline['receipt_sha256'],
                'head_sha':self.head,'tree_sha':self.proof['tree_sha']}),
            'issued_at':NOW.isoformat(),'expires_at':(NOW+timedelta(minutes=45)).isoformat(),
            'policy_sha256':'c'*64,'window':{'timezone':'America/Sao_Paulo','start':NOW.isoformat(),
                'end':(NOW+timedelta(hours=2)).isoformat()},
            'source_pr':{'number':12,'base_sha':self.base,'head_sha':self.head,'tree_sha':self.proof['tree_sha']},
            'baseline':baseline,'base_manifest':self.manifest}
        self.api=API(self)
    def observe(self,clock=lambda:NOW,baseline_lookup=None):
        with patch('socket.socket',side_effect=AssertionError('No network in synthetic receiver tests')):
            return r.observe(self.request,self.config,self.env,self.event,self.api,self.root,NOW,clock=clock,
                baseline_lookup=baseline_lookup or (lambda *_,**__:copy.deepcopy(self.baseline)))
    def test_authenticated_source_is_ready_without_merge_deploy_or_reclassification(self):
        with patch.object(r.gate,'classify_source',side_effect=AssertionError('CI classified once')):
            result=self.observe()
        self.assertEqual(result['status'],'source_ready');self.assertFalse(result['merge_authorized'])
        self.assertFalse(result['deployment_authorized']);self.assertEqual(result['request_id'],self.request['request_id'])
        self.assertEqual(result['source_artifact']['digest'],self.api.artifact['digest'])
        self.assertIn('HOST_EXECUTOR',result['blocker'])
        self.assertFalse(any('dispatch' in call or '/merge' in call for call in self.api.calls))
    def test_disabled_unqualified_or_unauthorized_request_stops_before_io(self):
        for container,key,value in ((self.config,'enabled',False),(self.config,'host_qualification_sha256',None),
            (self.env,'GITHUB_ACTOR_ID','22529012'),(self.env,'GITHUB_TRIGGERING_ACTOR','leodots'),
            (self.env,'GITHUB_REF','refs/heads/feature'),(self.env,'GITHUB_RUN_ATTEMPT','2'),
            (self.request,'phase','deploy'),(self.request,'policy_sha256','d'*64),
            (self.request,'request_id','d'*64),(self.request,'expires_at',(NOW-timedelta(seconds=1)).isoformat())):
            with self.subTest(key=key,value=value),patch.dict(container,{key:value}),self.assertRaises(ValueError):self.observe()
        self.assertEqual(self.api.calls,[])
        with patch.dict(self.request,{'approved':True}),self.assertRaisesRegex(ValueError,'REQUEST_SCHEMA'):self.observe()
    def test_closed_baseline_and_published_asset_are_both_required(self):
        with patch.dict(self.request['baseline'],{'image':'ghcr.io/other/image@sha256:'+'d'*64}),self.assertRaisesRegex(ValueError,'BASELINE_BINDING'):self.observe()
        other=copy.deepcopy(self.baseline);other['manifest']['git_sha']='f'*40
        with self.assertRaisesRegex(ValueError,'PUBLISHED_BASELINE_DRIFT'):
            self.observe(baseline_lookup=lambda *_,**__:other)
        with patch.dict(self.manifest['deployment'],{'status':'built'}),self.assertRaises(ValueError):self.observe()
    def test_ci_failure_attempt_job_unknown_or_unavailable_source_proof_refuses(self):
        for key,value in (('conclusion','failure'),('event','push'),('status','in_progress'),('head_sha','f'*40)):
            with self.subTest(key=key),patch.dict(self.api.run,{key:value}),self.assertRaises(ValueError):self.observe()
        for key,value in (('run_attempt',2),('head_sha','f'*40),('conclusion','skipped')):
            with self.subTest(key=key),patch.dict(self.api.jobs['jobs'][0],{key:value}),self.assertRaisesRegex(ValueError,'CI_JOBS_REQUIRED'):self.observe()
        with patch.dict(self.api.artifact,{'expired':True}),self.assertRaisesRegex(ValueError,'SOURCE_PROOF_MISSING'):self.observe()
        with patch.dict(self.api.artifact['workflow_run'],{'repository_id':1}),self.assertRaisesRegex(ValueError,'ARTIFACT_RUN_BINDING'):self.observe()
    def test_tampered_digest_extra_zip_file_and_refused_source_are_never_ready(self):
        with patch.dict(self.api.artifact,{'digest':'sha256:'+'0'*64}),self.assertRaisesRegex(ValueError,'ARTIFACT_DIGEST'):self.observe()
        self.api.pack(extra=True)
        with self.assertRaisesRegex(ValueError,'ARTIFACT_FILES'):self.observe()
        self.proof['status']='refused';self.proof['code']='NON_DEPENDENCY_FILE';self.api.pack()
        with self.assertRaisesRegex(ValueError,'SOURCE_PROOF_IDENTITY'):self.observe()
    def test_changed_trusted_code_or_delta_bound_to_another_tree_refuses(self):
        for key,value in (('code_sha256',{}),('tree_sha','f'*40),('trusted_commit','f'*40)):
            old=self.proof[key];self.proof[key]=value;self.api.pack()
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'SOURCE_PROOF_IDENTITY'):self.observe()
            self.proof[key]=old
        self.api.pack();(self.root/'scripts/maintenance-receiver.py').write_text('# changed trusted code')
        with self.assertRaisesRegex(ValueError,'TRUSTED_CODE_DRIFT'):self.observe()
    def test_cumulative_feature_cannot_hide_behind_a_dependency_only_proof(self):
        before=self.api.snapshots[self.base]
        before['files']['feature.js']={'mode':'100644','oid':r.gate.git_oid('blob',b'old feature')}
        before['tree']=r.gate.tree_oid(before['files'])
        self.proof['classification']['base_tree']=before['tree']
        delta=self.proof['classification'];delta['delta_sha256']=r.gate.sha256({k:v for k,v in delta.items() if k!='delta_sha256'})
        self.api.pack()
        with self.assertRaisesRegex(ValueError,'CUMULATIVE_FEATURE_OR_CONTROL_DELTA'):self.observe()
    def test_registry_and_advisory_are_refreshed_and_fail_closed(self):
        for fault,code in (('advisory','OFFICIAL_ADVISORY_REVIEW'),('metadata','INSTALL_SCRIPT_REQUIRES_REVIEW')):
            self.api.fault=fault
            with self.subTest(fault=fault),self.assertRaisesRegex(ValueError,code):self.observe()
        self.api.fault=None;original=self.api.package
        def immature(name):
            value=original(name)
            for v in value['time']:value['time'][v]=(NOW-timedelta(days=1)).isoformat()
            return value
        with patch.object(self.api,'package',side_effect=immature),self.assertRaisesRegex(ValueError,'NPM_RELEASE_TOO_YOUNG_OR_FUTURE'):self.observe()
    def test_final_freshness_window_baseline_and_concurrent_rerun_are_rechecked(self):
        self.proof['observed_at']=(NOW-timedelta(hours=24)+timedelta(seconds=1)).isoformat();self.api.pack()
        with self.assertRaisesRegex(ValueError,'STALE_OR_FUTURE_PROOF'):
            self.observe(clock=lambda:NOW+timedelta(seconds=2))
        self.proof['observed_at']=NOW.isoformat();self.api.pack()
        with self.assertRaisesRegex(ValueError,'WINDOW_OR_TTL'):
            self.observe(clock=lambda:NOW+timedelta(minutes=46))
        calls=[]
        def baseline(*_,**__):
            value=copy.deepcopy(self.baseline);calls.append(1)
            if len(calls)>1:value['published_manifest_sha256']='f'*64
            return value
        with self.assertRaisesRegex(ValueError,'FINAL_PUBLISHED_BASELINE_DRIFT'):self.observe(baseline_lookup=baseline)
        original=self.api.get
        def retry(path):
            value=copy.deepcopy(original(path))
            if path.endswith('/actions/runs/123'):value['run_attempt']=2
            return value
        with patch.object(self.api,'get',side_effect=retry),self.assertRaisesRegex(ValueError,'FINAL_REF_OR_CI_DRIFT'):self.observe()
    def test_tree_truncation_and_newer_ci_run_are_not_ignored(self):
        original=self.api.get;reads=[]
        def latest(path):
            value=copy.deepcopy(original(path))
            if '/actions/workflows/ci.yml/runs?' in path:
                reads.append(1)
                if len(reads)>1:value['workflow_runs'][0]['id']=124
            return value
        with patch.object(self.api,'get',side_effect=latest),self.assertRaisesRegex(ValueError,'NEW_CI_RUN'):self.observe()
        def truncated(path):
            value=copy.deepcopy(original(path))
            if '/git/trees/' in path:value['truncated']=True
            return value
        with patch.object(self.api,'get',side_effect=truncated),self.assertRaisesRegex(ValueError,'TREE_INCOMPLETE'):self.observe()


class TransportTests(unittest.TestCase):
    def test_disabled_cli_records_sanitized_refusal_without_token_or_llm_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            request=Path(folder)/'request.json';event=Path(folder)/'event.json';output=Path(folder)/'result.json'
            # A shape-correct but untrusted payload must not become receipt identity.
            request.write_text(json.dumps({key:None for key in r.REQUEST_KEYS}));event.write_text('{}')
            result=subprocess.run([sys.executable,'-I','-B',str(ROOT/'scripts/maintenance-receiver.py'),str(request),str(output)],
                env={'PATH':os.environ['PATH'],'GITHUB_EVENT_PATH':str(event)},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stderr.strip(),'RECEIVER_NOT_QUALIFIED')
            value=json.loads(output.read_text());self.assertEqual(value['status'],'refused')
            self.assertNotIn('request_id',value);self.assertFalse(value['deployment_authorized'])

    def test_fixed_origin_download_is_bounded_and_has_no_inherited_credentials(self):
        raw=b'bounded synthetic archive';metadata={'id':19,'size_in_bytes':len(raw),
            'digest':'sha256:'+hashlib.sha256(raw).hexdigest()}
        api=r.API('synthetic-read-token')
        def run(args,**kwargs):
            self.assertEqual(args,['gh','api',r.PREFIX+'/actions/artifacts/19/zip'])
            self.assertNotIn('synthetic-read-token',' '.join(args))
            self.assertEqual(set(kwargs['env']),{'PATH','GH_HOST','GH_TOKEN','HOME','GH_CONFIG_DIR'})
            self.assertEqual(kwargs['timeout'],20);self.assertTrue(callable(kwargs['preexec_fn']))
            kwargs['stdout'].write(raw);return subprocess.CompletedProcess(args,0)
        with patch.dict(os.environ,{'HTTP_PROXY':'http://invalid','OPENAI_API_KEY':'must-not-inherit'}),patch.object(r.subprocess,'run',side_effect=run):
            self.assertEqual(api.artifact_bytes(metadata),raw)
        with patch.object(r.subprocess,'run') as run,self.assertRaisesRegex(ValueError,'ARTIFACT_METADATA'):
            api.artifact_bytes(dict(metadata,id='../../other'))
        run.assert_not_called()
    def test_workflow_has_only_read_authority_and_default_config_is_disabled(self):
        raw=(ROOT/'.github/workflows/maintenance.yml').read_text()
        self.assertIn('actions: read',raw);self.assertIn('contents: read',raw)
        self.assertNotIn(': write',raw);self.assertNotIn('secrets.',raw);self.assertNotIn('deploy.yml',raw)
        self.assertNotIn('npm ',raw);self.assertNotIn('yarn ',raw)
        self.assertIn('ref: ${{ github.sha }}',raw)
        config=json.loads((ROOT/'.github/maintenance.json').read_text())
        self.assertFalse(config['enabled']);self.assertIsNone(config['host_qualification_sha256'])
        self.assertIsNone(config['policy_sha256']);self.assertEqual(config['trusted_code'],{})


if __name__=='__main__':unittest.main()
