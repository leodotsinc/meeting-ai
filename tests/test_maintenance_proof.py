import base64
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_maintenance_source import ROOT, fixture, item

spec=importlib.util.spec_from_file_location('proof',ROOT/'scripts/maintenance-source-proof.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
NOW=datetime(2026,9,21,12,tzinfo=timezone.utc)


class API:
    def __init__(self,test):self.test=test;self.calls=[];self.fault=None
    def get(self,path):
        self.calls.append(path);t=self.test
        if path==p.PREFIX:return {'id':p.REPOSITORY_ID,'full_name':p.REPO}
        if '/pulls/' in path:
            return {'number':12,'state':'open','draft':False,'merged':False,
                'user':{'id':42 if self.fault=='actor' else 29139614,'login':'renovate[bot]','type':'Bot'},
                'base':{'sha':t.base,'ref':'main','repo':{'full_name':p.REPO}},
                'head':{'sha':'f'*40 if self.fault=='head' else t.head,'repo':{'full_name':p.REPO}}}
        if path.endswith('/commits/main'):return {'sha':t.base}
        if '/contents/' in path:
            name=path.split('/contents/',1)[1].split('?ref=')[0];raw=(t.root/name).read_bytes()
            return {'encoding':'base64','content':base64.b64encode(raw).decode(),'sha':p.gate.git_oid('blob',raw)}
        raise AssertionError(path)
    def package(self,name):
        self.calls.append('registry:'+name)
        old=self.test.lock['packages']['node_modules/'+name];new=self.test.new_lock['packages']['node_modules/'+name]
        versions={}
        for lock in (old,new):
            versions[lock['version']]={'name':name,'version':lock['version'],
                'dist':{'integrity':lock['integrity'],'tarball':lock['resolved']},
                **{key:lock[key] for key in p.gate.GROUPS if key in lock}}
        if self.fault=='metadata':versions[new['version']]['scripts']={'postinstall':'do-not-execute'}
        return {'name':name,'versions':versions,'time':{new['version']:(NOW-timedelta(days=2 if self.fault=='young' else 15)).isoformat()}}
    def read(self,url,*,payload=None):
        self.calls.append(url);assert url=='https://registry.npmjs.org/-/npm/v1/security/advisories/bulk'
        assert payload
        return {'alpha':[{'severity':'high'}]} if self.fault=='advisory' else {}


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.package,self.lock,_=fixture();self.new_lock=copy.deepcopy(self.lock)
        self.new_lock['packages']['node_modules/alpha']=item('alpha','1.1.1',dependencies={'beta':'~2.3.0'})
        for name in p.CODE:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((ROOT/name).read_bytes())
        (self.root/'package.json').write_text(json.dumps(self.package));(self.root/'package-lock.json').write_text(json.dumps(self.lock))
        self.git('init','-q');self.git('config','user.name','Synthetic');self.git('config','user.email','synthetic@example.invalid')
        self.git('add','.');self.git('commit','-qm','baseline');self.base=self.git('rev-parse','HEAD')
        (self.root/'package-lock.json').write_text(json.dumps(self.new_lock));self.git('add','.');self.git('commit','-qm','dependency patch');self.head=self.git('rev-parse','HEAD')
        self.git('checkout','-q',self.base)
        self.env={'GITHUB_REPOSITORY':p.REPO,'GITHUB_EVENT_NAME':'pull_request','GITHUB_SHA':self.head,
            'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','GITHUB_WORKFLOW_SHA':self.head}
        self.event={'number':12,'pull_request':{'head':{'sha':self.head},'base':{'sha':self.base}}}
        self.api=API(self)
    def git(self,*args):return subprocess.check_output(['git','-C',str(self.root),*args],text=True,stderr=subprocess.DEVNULL).strip()
    def emit(self,**kwargs):
        with patch('socket.socket',side_effect=AssertionError('Tests do not access network')):
            return p.emit_source(self.api,self.root,self.env,self.event,NOW,
                baseline_lookup=lambda *_:{'commit':self.base,'tag':'v0.1.5','published_manifest_sha256':'a'*64},
                clock=kwargs.get('clock',lambda:NOW))

    def test_proof_binds_source_workflow_closure_and_published_baseline_without_authority(self):
        value=self.emit()
        self.assertEqual(value['status'],'passed');self.assertIsNone(value['code'])
        self.assertEqual(value['classification']['base_commit'],self.base)
        self.assertEqual(value['classification']['candidate_commit'],self.head)
        self.assertEqual(value['baseline']['published_manifest_sha256'],'a'*64)
        self.assertEqual(set(value['code_sha256']),p.CODE)
        self.assertFalse(value['authorized_to_apply'])
        self.assertEqual(value['release_version_strategy'],'existing_verified_tag_selector')
        self.assertTrue(all('dispatch' not in path for path in self.api.calls))

    def test_advisory_immaturity_and_lifecycle_unknown_never_pass(self):
        for fault,code in [('young','NPM_RELEASE_TOO_YOUNG_OR_FUTURE'),('advisory','OFFICIAL_ADVISORY_REVIEW'),
                           ('metadata','INSTALL_SCRIPT_REQUIRES_REVIEW')]:
            self.api.fault=fault
            with self.subTest(fault=fault):
                value=self.emit();self.assertEqual(value['status'],'refused');self.assertEqual(value['code'],code)

    def test_untrusted_identity_code_drift_and_expired_observation_fail_closed(self):
        for fault in ('actor','head'):
            self.api.fault=fault
            with self.assertRaises(p.gate.Refusal):self.emit()
        self.api.fault=None
        with self.assertRaisesRegex(p.gate.Refusal,'DEADLINE'):self.emit(clock=lambda:NOW+timedelta(minutes=4))
        (self.root/'scripts/maintenance-source-proof.py').write_text('changed source')
        with self.assertRaisesRegex(p.gate.Refusal,'SOURCE_CODE_DRIFT'):self.emit()

    def test_production_to_candidate_features_refuse_even_if_latest_pr_only_changes_lock(self):
        # A separate baseline with older executable source is real Git data.
        self.git('checkout','-q','--detach',self.base)
        (self.root/'feature.txt').write_text('unreleased feature');self.git('add','.');self.git('commit','-qm','feature')
        newer_base=self.git('rev-parse','HEAD')
        (self.root/'package-lock.json').write_text(json.dumps(self.new_lock));self.git('add','.');self.git('commit','-qm','lock patch')
        self.head=self.git('rev-parse','HEAD');self.git('checkout','-q',newer_base)
        production=self.base;self.base=newer_base
        self.env['GITHUB_WORKFLOW_SHA']=self.head;self.event['pull_request']={'head':{'sha':self.head},'base':{'sha':self.base}}
        value=p.emit_source(self.api,self.root,self.env,self.event,NOW,
            baseline_lookup=lambda *_:{'commit':production,'tag':'v0.1.5','published_manifest_sha256':'a'*64},clock=lambda:NOW)
        self.assertEqual(value['status'],'refused');self.assertEqual(value['code'],'NON_DEPENDENCY_SOURCE_DELTA')

    def test_api_fixed_origin_proxy_refusal_and_deadline(self):
        client=p.API('synthetic-token')
        proxies=[h for h in client.opener.handlers if isinstance(h,p.urllib.request.ProxyHandler)]
        self.assertTrue(not proxies or all(h.proxies=={} for h in proxies))
        with self.assertRaisesRegex(p.gate.Refusal,'API_ORIGIN'):client.read('https://example.invalid/private')
        client.deadline=0
        with self.assertRaisesRegex(p.gate.Refusal,'API_BUDGET'):client.get(p.PREFIX)

    def test_nominal_cli_removes_model_credentials_and_keeps_read_only_real_classifier(self):
        event=self.root/'event.json';event.write_text(json.dumps(self.event));output=self.root/'proof.json'
        original=p.emit_source
        def emit(api,root,env,payload,now):
            self.assertNotIn('OPENAI_API_KEY',os.environ);self.assertNotIn('ANTHROPIC_API_KEY',os.environ)
            return original(api,self.root,env,payload,now,
                baseline_lookup=lambda *_:{'commit':self.base,'tag':'v0.1.5','published_manifest_sha256':'a'*64},clock=lambda:now)
        environment={**self.env,'GH_TOKEN':'synthetic-only','GITHUB_EVENT_PATH':str(event),'PATH':os.environ['PATH'],
                     'OPENAI_API_KEY':'do-not-use','ANTHROPIC_API_KEY':'do-not-use'}
        with patch.dict(os.environ,environment,clear=True),patch.object(p.sys,'argv',['proof',str(output)]), \
             patch.object(p,'API',return_value=self.api),patch.object(p,'emit_source',side_effect=emit), \
             patch('socket.socket',side_effect=AssertionError('No network or model in synthetic CLI')):
            p.main()
        value=json.loads(output.read_text());self.assertEqual(value['status'],'passed');self.assertFalse(value['authorized_to_apply'])

    def test_workflow_source_producer_is_separate_from_candidate_execution(self):
        ci=(ROOT/'.github/workflows/ci.yml').read_text();job=ci.split('  source-qualification:',1)[1].split('  qualify:',1)[0]
        self.assertIn("github.event_name == 'pull_request'",job)
        self.assertIn('github.event.pull_request.user.id == 29139614',job)
        self.assertIn('ref: ${{ github.event.pull_request.base.sha }}',job)
        self.assertNotIn('npm ci\n',job);self.assertNotIn('npm run',job);self.assertNotIn('secrets.',job)
        self.assertIn('npm ci --prefix scripts/maintenance-tools --ignore-scripts',job)
        self.assertIn('retention-days: 45',job);self.assertIn('retention-days: 7',job)
        deploy=(ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertIn("github.event.workflow_run.event == 'push'",deploy)
        self.assertIn('branches: [main]',deploy)


if __name__=='__main__':unittest.main()
