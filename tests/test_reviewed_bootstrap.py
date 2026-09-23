"""No production I/O. Real Git delta + synthetic authenticated artifact contracts."""
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bootstrap',ROOT/'scripts/reviewed-bootstrap.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
NOW=datetime(2026,9,22,15,tzinfo=timezone.utc)
# Fully synthetic Git snapshots: CI may have a shallow checkout and tests must
# never fetch a production commit or network just to exercise these gates.
from test_maintenance_source import snapshot, item
package={'name':'meeting-fixture','version':'0.1.0','private':True,'scripts':{'test':'node --test'},'overrides':{'prisma':{'mysql2':'3.23.1'}}}
lock={'name':'meeting-fixture','version':'0.1.0','lockfileVersion':3,'packages':{
    '':{'name':'meeting-fixture','version':'0.1.0'},'node_modules/deepmerge-ts':item('deepmerge-ts','7.1.5')}}
BASE=snapshot(package,lock,b.BASE)
package=copy.deepcopy(package);package['overrides']['@prisma/config']={'deepmerge-ts':'8.0.0'}
lock=copy.deepcopy(lock);lock['packages']['node_modules/deepmerge-ts']=item('deepmerge-ts','8.0.0')
lock['packages']['node_modules/deepmerge-ts']['integrity']='sha512-ICNjaP0ML+eSdEpJYQC46XiAn/UjAdwbEl0dE8p85ZTeNDinN4Kd4+9jS4OSAuH7st6eC7rQhsqTF5zIDaUm2g=='
HEAD=snapshot(package,lock,'c'*40)


def changed(snapshot,path,data):
    value=copy.deepcopy(snapshot);value['contents'][path]=data
    value['files'][path]={'mode':'100644','oid':b.g.git_oid('blob',data.encode())}
    value['tree']=b.g.tree_oid(value['files']);return value


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config={'schema_version':1,'service':'meeting-ai','enabled':True,'baseline_commit':b.BASE}
        self.request={'schema_version':1,'service':'meeting-ai','source_sha':HEAD['commit'],'source_tree':HEAD['tree'],
            'baseline_receipt_sha256':'a'*64,'target_manifest_sha256':'b'*64,'prepare_run_id':123,'prepare_run_attempt':1,
            'cumulative_delta_sha256':b.g.sha256(b.delta(BASE,HEAD)), 'issued_at':NOW.isoformat().replace('+00:00','Z'),'expires_at':(NOW+timedelta(hours=1)).isoformat().replace('+00:00','Z')}
        self.env={'GITHUB_REPOSITORY':b.r.REPO,'GITHUB_REPOSITORY_ID':str(b.r.source.REPOSITORY_ID),
            'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1',
            'GITHUB_ACTOR_ID':'22529012','GITHUB_ACTOR':'leodots','GITHUB_TRIGGERING_ACTOR':'leodots',
            'GITHUB_SHA':HEAD['commit'],'GITHUB_WORKFLOW_SHA':HEAD['commit'],
            'GITHUB_WORKFLOW_REF':b.r.REPO+'/.github/workflows/deploy.yml@refs/heads/main'}
        self.event={'sender':{'id':22529012},'inputs':{'prepare_only':'false'}}
    def test_explicit_review_has_fixed_production_base_and_only_exact_existing_override(self):
        result=b.delta(BASE,HEAD)
        self.assertEqual(result['dependency']['before'],'7.1.5');self.assertEqual(result['dependency']['version'],'8.0.0')
        self.assertTrue(all(not row['path'].startswith(('src/','prisma/','app/')) for row in result['files']))
        for path in ('src/app/page.tsx','prisma/schema.prisma','scripts/unreviewed.py'):
            candidate=copy.deepcopy(HEAD);candidate['files'][path]={'mode':'100644','oid':'a'*40};candidate['tree']=b.g.tree_oid(candidate['files'])
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'UNREVIEWED_PATH'):b.delta(BASE,candidate)
    def test_other_dependency_major_script_or_override_cannot_piggyback(self):
        package=b.g.decode(HEAD['contents']['package.json'])
        for field,value in [('version','9.0.0'),('scripts',{'postinstall':'arbitrary'}),('overrides',{'deepmerge-ts':'9.0.0'})]:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'PACKAGE_BEHAVIOR'):
                b.delta(BASE,changed(HEAD,'package.json',json.dumps({**package,field:value})))
        lock=b.g.decode(HEAD['contents']['package-lock.json']);lock['packages']['node_modules/deepmerge-ts']['integrity']='sha512-wrong'
        with self.assertRaisesRegex(ValueError,'REVIEWED_OVERRIDE'):b.delta(BASE,changed(HEAD,'package-lock.json',json.dumps(lock)))
        lock=b.g.decode(HEAD['contents']['package-lock.json']);lock['packages']['node_modules/unreviewed']={'version':'1.0.0'}
        with self.assertRaisesRegex(ValueError,'LOCK_SCOPE'):b.delta(BASE,changed(HEAD,'package-lock.json',json.dumps(lock)))
    def test_disabled_config_personal_main_attempt_and_ttl_are_required(self):
        b.envelope(self.request,self.config,self.env,self.event,NOW)
        for key,value in [('enabled',False),('enabled',1),('baseline_commit','f'*40)]:
            with self.subTest(key=key),self.assertRaises(ValueError):b.envelope(self.request,{**self.config,key:value},self.env,self.event,NOW)
        for key,value in [('GITHUB_RUN_ATTEMPT','2'),('GITHUB_ACTOR_ID','332011818'),('GITHUB_TRIGGERING_ACTOR','other'),('GITHUB_REF','refs/heads/feature'),('GITHUB_WORKFLOW_SHA','a'*40)]:
            with self.subTest(key=key),self.assertRaises(ValueError):b.envelope(self.request,self.config,{**self.env,key:value},self.event,NOW)
        for minutes in (-1,46,60):
            with self.subTest(minutes=minutes),self.assertRaises(ValueError):b.envelope(self.request,self.config,self.env,self.event,NOW+timedelta(minutes=minutes))
        with self.assertRaises(ValueError):b.envelope(self.request,self.config,self.env,{'sender':{'id':22529012},'inputs':{'prepare_only':'true'}},NOW)
    def test_artifact_identity_archive_scope_expiry_and_expansion_fail_closed(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:archive.writestr('release.json',b'{}')
        raw=buf.getvalue();metadata={'name':'prepared','expired':False,'workflow_run':{'id':123,'head_sha':HEAD['commit'],
            'repository_id':b.r.source.REPOSITORY_ID,'head_repository_id':b.r.source.REPOSITORY_ID}}
        class API:
            def get(self,path):return {'total_count':1,'artifacts':[metadata]}
            def artifact_bytes(self,m):return raw
        api=API();files,linked=b.artifact(api,123,1,'prepared',{'release.json'});self.assertEqual(files['release.json'],b'{}')
        for change in ({'expired':True},{'workflow_run':{'id':124}},{'name':'other'}):
            with patch.dict(metadata,change),self.assertRaises(ValueError):b.artifact(api,123,1,'prepared',{'release.json'})
        with self.assertRaises(ValueError):b.artifact(api,123,1,'prepared',{'../release.json'})
    def test_deploy_workflow_reuses_image_and_never_builds_in_bootstrap_branch(self):
        workflow=(ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertIn("needs.gate.outputs.released != 'true' && inputs.reviewed_bootstrap == ''",workflow)
        self.assertIn('needs.gate.outputs.bootstrap_artifact || needs.build.outputs.artifact_name',workflow)
        self.assertIn("needs.build.result == 'skipped'",workflow)
        self.assertIn('meeting-ai-preparation-${{ github.run_id }}-${{ github.run_attempt }}',workflow)
        start=workflow.index('      - name: Authenticate personally reviewed')
        finish=workflow.index('      - name: Authenticate maintenance handoff',start)
        bootstrap=workflow[start:finish]
        self.assertIn('docker pull "$IMAGE"',bootstrap);self.assertNotIn('docker build',bootstrap);self.assertNotIn('docker push',bootstrap)
        self.assertIn('meeting-ai-bootstrap-attempt-',bootstrap)
        self.assertFalse(json.loads((ROOT/'.github/bootstrap.json').read_text())['enabled'])


class PreparedImageAuthenticationTests(unittest.TestCase):
    def setUp(self):
        BootstrapTests.setUp(self)
        import test_maintenance_release_proof as existing
        fixture=existing.ReleaseProofTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.inputs=fixture.root
        import sys
        sys.path.insert(0,str(ROOT/'scripts'));import release_manifest as contract
        self.manifest=contract.create(service='meeting-ai',compose_project='meeting-ai',application_kind='first_party',
            app_version='0.1.6',release_version='0.1.6',image='ghcr.io/leodotsinc/meeting-ai@sha256:'+'a'*64,
            git_sha=HEAD['commit'],source_repository='https://github.com/'+b.r.REPO,build_id='123',build_attempt=1,
            built_at=(NOW-timedelta(hours=1)).isoformat().replace('+00:00','Z'))
        previous=copy.deepcopy(self.manifest);previous.update(app_version='0.1.5',release_version='0.1.5',git_sha=b.BASE)
        self.baseline=contract.record_deployment(previous,status='verified',deployed_at=(NOW-timedelta(minutes=50)).isoformat().replace('+00:00','Z'),
            verified_at=(NOW-timedelta(minutes=49)).isoformat().replace('+00:00','Z'),observed_image=previous['image'])
        self.request['baseline_receipt_sha256']=b.g.sha256(self.baseline)
        self.request['target_manifest_sha256']=b.g.sha256(self.manifest)
        self.raw=json.dumps(self.manifest,indent=2).encode()+b'\n'
        (self.inputs/'release.json').write_bytes(self.raw)
        image=json.loads((self.inputs/'release-image.json').read_text());image.update(source_commit=HEAD['commit'],run_id=123)
        (self.inputs/'release-image.json').write_text(json.dumps(image))
        functional=json.loads((self.inputs/'meeting-functional.json').read_text())
        functional.update(revision=HEAD['commit'],build_id='123',observed_at=NOW.isoformat().replace('+00:00','Z'))
        (self.inputs/'meeting-functional.json').write_text(json.dumps(functional))
        for stage in ('runtime','builder','deps'):
            path=self.inputs/'security'/stage/'security.json';scan=json.loads(path.read_text());scan.update(observed_at=NOW.isoformat().replace('+00:00','Z'),database_updated_at=NOW.isoformat().replace('+00:00','Z'));path.write_text(json.dumps(scan))
        self.preparation={'schema_version':1,'prepare_only':True,'run_id':123,'run_attempt':1,'source_sha':HEAD['commit'],'manifest_sha256':b.g.sha256(self.manifest)}
        self.ci={'id':200,'head_sha':HEAD['commit'],'run_attempt':1,'path':'.github/workflows/ci.yml','event':'push',
            'status':'completed','conclusion':'success','head_repository':{'id':b.r.source.REPOSITORY_ID}}
        self.run={'id':123,'head_sha':HEAD['commit'],'run_attempt':1,'path':'.github/workflows/deploy.yml','event':'workflow_dispatch',
            'status':'completed','conclusion':'success','actor':{'id':22529012},'triggering_actor':{'id':22529012}}
        self.metadata=[];self.archives={};self.reserved=False;self.advisories={}
        self.build_archives()
        outer=self
        class API:
            def get(self,path):
                if path.endswith('/commits/main'):return {'sha':HEAD['commit']}
                if '/actions/workflows/ci.yml/runs?' in path:return {'total_count':1,'workflow_runs':[outer.ci]}
                if path.endswith('/actions/runs/200'):return outer.ci
                if path.endswith('/actions/runs/123'):return outer.run
                if '/attempts/1/jobs?' in path:return {'total_count':2,'jobs':[{'run_id':123,'name':name,'status':'completed','conclusion':'success'} for name in ('Select verified source and version','Build and qualify exact image')]}
                if '/actions/runs/123/artifacts?' in path:return {'total_count':len(outer.metadata),'artifacts':outer.metadata}
                if '/actions/artifacts?name=' in path:return {'total_count':int(outer.reserved),'artifacts':[{}] if outer.reserved else []}
                raise AssertionError(path)
            def artifact_bytes(self,meta):return outer.archives[meta['id']]
            def package(self,name):return {'name':name,'versions':{'8.0.0':{'name':name,'version':'8.0.0','dist':{'integrity':b.delta(BASE,HEAD)['dependency']['integrity']}}},'time':{'8.0.0':(NOW-timedelta(days=30)).isoformat().replace('+00:00','Z')}}
            def read(self,url,*,payload):
                assert url=='https://registry.npmjs.org/-/npm/v1/security/advisories/bulk'
                assert '8.0.0' in payload['deepmerge-ts'];return outer.advisories
        self.api=API()
    def build_archives(self):
        self.metadata=[];self.archives={}
        files=[('meeting-release-123-1',{'release.json':self.raw,'release-image.json':(self.inputs/'release-image.json').read_bytes()}),
            ('meeting-ai-preparation-123-1',{'preparation.json':json.dumps(self.preparation).encode()}),
            ('meeting-ai-release-functional-123-1',{'meeting-functional.json':(self.inputs/'meeting-functional.json').read_bytes()}),
            ('meeting-ai-release-security-123-1',{stage+'/security.json':(self.inputs/'security'/stage/'security.json').read_bytes() for stage in ('runtime','builder','deps')})]
        for index,(name,entries) in enumerate(files,1):
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w') as archive:
                for path,raw in entries.items():archive.writestr(path,raw)
            raw=out.getvalue();self.archives[index]=raw
            self.metadata.append({'name':name,'id':index,'expired':False,'size_in_bytes':len(raw),'digest':'sha256:'+hashlib.sha256(raw).hexdigest(),
                'workflow_run':{'id':123,'head_sha':HEAD['commit'],'repository_id':b.r.source.REPOSITORY_ID,'head_repository_id':b.r.source.REPOSITORY_ID}})
    def authenticate(self):
        def collect(root,sha):return BASE if sha==b.BASE else HEAD
        with patch.object(b.g,'collect',side_effect=collect),patch.object(b.r.source,'published_baseline',return_value={'manifest':self.baseline}), \
                patch.object(b.subprocess,'check_output',return_value='\n'.join('FROM '+x for x in (b.NODE,'base','base','base'))):
            return b.authenticate(self.request,self.config,self.env,self.event,self.api,ROOT,now_fn=lambda:NOW)
    def test_authentication_preserves_original_manifest_bytes_build_and_exact_image(self):
        raw,proof=self.authenticate();self.assertEqual(raw,self.raw)
        self.assertEqual(proof['prepare_run_id'],123);self.assertEqual(proof['image'],self.manifest['image'])
        self.assertFalse(proof['production_authorized']);self.assertEqual(json.loads(raw)['build']['id'],'123')
    def test_fake_prepare_flag_replay_or_current_ci_failure_refuses_original_image(self):
        self.preparation['prepare_only']=False;self.build_archives()
        with self.assertRaisesRegex(ValueError,'PREPARATION_FLAG'):self.authenticate()
        self.preparation['prepare_only']=True;self.build_archives();self.reserved=True
        with self.assertRaisesRegex(ValueError,'ATTEMPT_RECONCILIATION'):self.authenticate()
        self.reserved=False;self.ci['conclusion']='failure'
        with self.assertRaisesRegex(ValueError,'CI_REQUIRED'):self.authenticate()
    def test_stale_scanner_or_current_advisory_never_accepts_old_green_prepare(self):
        self.advisories={'deepmerge-ts':[{'id':1}]}
        with self.assertRaisesRegex(ValueError,'OFFICIAL_ADVISORY'):self.authenticate()
        self.advisories={};path=self.inputs/'security/runtime/security.json';scan=json.loads(path.read_text());scan['database_updated_at']=(NOW-timedelta(hours=25)).isoformat().replace('+00:00','Z');path.write_text(json.dumps(scan));self.build_archives()
        with self.assertRaisesRegex(ValueError,'STALE_OR_FUTURE_PROOF'):self.authenticate()
    def test_missing_or_cross_source_artifact_and_changed_preparation_attempt_refuse(self):
        self.metadata[0]['workflow_run']['head_sha']='f'*40
        with self.assertRaisesRegex(ValueError,'ARTIFACT_SHA'):self.authenticate()
        self.build_archives();self.metadata.pop()
        with self.assertRaisesRegex(ValueError,'ARTIFACT_MISSING'):self.authenticate()
        self.build_archives();self.run['run_attempt']=2
        with self.assertRaisesRegex(ValueError,'PREPARE_RUN'):self.authenticate()


if __name__=='__main__':unittest.main()
