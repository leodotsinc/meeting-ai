import copy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import test_maintenance_receiver as f

spec=importlib.util.spec_from_file_location('normalizer',f.ROOT/'scripts/maintenance-release-proof.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)


class ReleaseProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.image='sha256:'+'b'*64;self.manifest={'image':'ghcr.io/leodotsinc/meeting-ai@sha256:'+'a'*64,'git_sha':'c'*40,'release_version':'0.1.6'}
        self.context={'producer_commit':'d'*40,'merged_sha':'c'*40,'delta_sha256':'e'*64}
        self.request={'source_proof':{'run_id':123,'run_attempt':1,'artifact_id':19,'digest':'sha256:'+'a'*64}}
        self.env={'GITHUB_RUN_ID':'456','GITHUB_RUN_ATTEMPT':'1'};self.config={'trusted_code':{'synthetic':'f'*64}}
        self.functional={'ok':True,'image_id':self.image,'version':'0.1.6','revision':'c'*40,'build_id':'456',
            'model_calls':0,'network_internal':True,'ports_published':False,'external_processing':False,'migration_readonly_image':True,
            'checks':sorted(n.FUNCTIONAL),'observed_at':f.NOW.isoformat(),'synthetic_restore':{
                'database_separate':True,'authenticated_projects':True,'existing_session_preserved':True,
                'original_application_preserved':True,'upload_sha256':'a'*64,'restored_upload_sha256':'a'*64}}
        self.scan={'schema_version':1,'status':'passed','image':self.image,'packages':40,'counts':{s:0 for s in n.SEVERITIES},
            'findings':[],'observed_at':f.NOW.isoformat(),'database_updated_at':f.NOW.isoformat()}
        self.write('release.json',self.manifest)
        self.write('release-image.json',{'image':self.manifest['image'],'image_id':self.image,'source_commit':'c'*40,
            'version':'0.1.6','run_id':456,'run_attempt':1})
        self.write('meeting-functional.json',self.functional)
        for stage in ('runtime','builder','deps'):self.write('security/'+stage+'/security.json',self.scan)
    def write(self,path,value):
        target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(value))
    def normalize(self,refresh=None):
        if refresh is None:refresh=lambda *args,**kwargs:{'change':'patch','scope':'dependency_updates','registry':[{'checked_at':f.NOW.isoformat(),
            'published_at':(f.NOW-timedelta(days=15)).isoformat(),'affecting_advisories':[]}]}
        return n.normalize(self.root,self.request,self.context,self.config,self.env,{},None,self.root,f.NOW,
            refresh=refresh,clock=lambda:f.NOW)
    def test_same_release_image_source_build_and_full_reports_are_bound(self):
        result=self.normalize();self.assertEqual(result['release_sha256'],n.gate.sha256(self.manifest))
        self.assertEqual(result['images'],{'app':self.manifest['image']});self.assertEqual(len(result['scans']),3)
        self.assertEqual(result['context_sha256'],n.gate.sha256(self.context));self.assertNotIn('authorized',result)
    def test_substituted_image_release_attempt_or_incomplete_functional_proof_refuses(self):
        for key,value in [('image_id','sha256:'+'f'*64),('revision','e'*40),('build_id','457'),('checks',[]),('model_calls',1)]:
            with self.subTest(key=key):
                self.write('meeting-functional.json',dict(self.functional,**{key:value}))
                with self.assertRaises(ValueError):self.normalize()
        self.write('meeting-functional.json',self.functional)
        image=json.loads((self.root/'release-image.json').read_text());image['run_attempt']=2;self.write('release-image.json',image)
        with self.assertRaisesRegex(ValueError,'PUBLISHED_IMAGE_BINDING'):self.normalize()
    def test_unknown_incomplete_stale_or_inconsistent_scan_fails(self):
        for key,value in [('status','unknown'),('packages',0),('database_updated_at',(f.NOW-timedelta(hours=25)).isoformat()),
            ('findings',[{'Severity':'HIGH'}]),('image','sha256:'+'f'*64)]:
            with self.subTest(key=key):
                self.write('security/runtime/security.json',dict(self.scan,**{key:value}))
                with self.assertRaises(ValueError):self.normalize()
        self.write('security/runtime/security.json',self.scan);(self.root/'security/builder/security.json').unlink()
        with self.assertRaises(ValueError):self.normalize()
    def test_registry_failure_or_immaturity_cannot_be_turned_into_green(self):
        def fail(*args,**kwargs):raise ValueError('synthetic advisory failure')
        with self.assertRaises(ValueError):self.normalize(fail)
        with self.assertRaises(ValueError):self.normalize(lambda *a,**k:{'change':'patch','scope':'dependency_updates','registry':[{'checked_at':f.NOW.isoformat(),
            'published_at':f.NOW.isoformat(),'affecting_advisories':[]}]})

if __name__=='__main__':unittest.main()
