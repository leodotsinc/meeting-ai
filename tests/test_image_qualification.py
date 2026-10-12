"""No Docker/HTTP access: execute the real in-container client against fake fetch."""
import base64
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stderr

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('qualify_image',ROOT/'scripts/qualify-image.py')
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)


class InternalHttpTests(unittest.TestCase):
    def execute_client(self,*args,input=None,timeout=None):
        self.assertEqual(args[:5],('docker','exec','-i','synthetic-app','node'))
        self.assertEqual(timeout,12)
        fake="""globalThis.fetch=async(url,options)=>{
          if(url.origin!=='http://127.0.0.1:3003'||options.redirect!=='manual')throw Error('unsafe request');
          const body=JSON.stringify({path:url.pathname,method:options.method,cookie:options.headers.Cookie,data:options.body});
          return {status:200,body:(async function*(){yield Buffer.from(body)})(),
            headers:{getSetCookie:()=>['authjs.session-token=synthetic; Path=/; HttpOnly'],
              *[Symbol.iterator](){yield ['cache-control','no-store']}}};
        };"""
        result=subprocess.run(['node','-e',fake+args[-1]],input=input,capture_output=True,text=True,timeout=5)
        if result.returncode:raise RuntimeError('client refused unsafe input')
        return result.stdout

    def test_authenticated_post_retains_session_without_host_http(self):
        cookies={'authjs.csrf-token':'fixture'}
        with patch.object(q,'run',side_effect=self.execute_client):
            status,body,headers=q.local_request('synthetic-app','/api/auth/callback/credentials',b'a=b',cookies)
        value=json.loads(body)
        self.assertEqual(status,200);self.assertEqual(value['method'],'POST')
        self.assertEqual(value['data'],'a=b');self.assertEqual(value['cookie'],'authjs.csrf-token=fixture')
        self.assertEqual(cookies['authjs.session-token'],'synthetic')
        self.assertEqual(headers['cache-control'],'no-store')

    def test_anonymous_get_sends_no_cookie(self):
        with patch.object(q,'run',side_effect=self.execute_client):
            _,body,_=q.local_request('synthetic-app','/api/health')
        self.assertEqual(json.loads(body)['cookie'],'')
        self.assertEqual(json.loads(body)['method'],'GET')

    def test_json_patch_uses_real_method_and_explicit_content_type(self):
        with patch.object(q,'run',side_effect=self.execute_client):
            _,body,_=q.local_request('synthetic-app','/api/meetings/fixture',b'{"title":"synthetic"}',
                                     {'authjs.session-token':'saved'},method='PATCH',content_type='application/json')
        self.assertEqual(json.loads(body)['method'],'PATCH')
        self.assertEqual(json.loads(body)['data'],'{"title":"synthetic"}')

    def test_unsupported_request_controls_fail_before_docker(self):
        for options in ({'method':'CONNECT'},{'content_type':'text/plain\r\nHost: external'}):
            with self.subTest(options=options),patch.object(q,'run') as run:
                with self.assertRaises(ValueError):q.local_request('synthetic-app','/api/projects',**options)
                run.assert_not_called()

    def test_absolute_network_path_and_header_injection_never_execute(self):
        for path in ('https://example.invalid','//example.invalid','/api/health\r\nHost: x'):
            with self.subTest(path=path),patch.object(q,'run') as run:
                with self.assertRaises(ValueError):q.local_request('synthetic-app',path)
                run.assert_not_called()

    def test_backslash_origin_escape_refused_by_actual_url_parser(self):
        with patch.object(q,'run',side_effect=self.execute_client):
            with self.assertRaises(RuntimeError):q.local_request('synthetic-app','/\\example.invalid')

    def test_oversize_response_aborts_instead_of_returning_partial_proof(self):
        def oversized(*args,input=None,timeout=None):
            fake="globalThis.fetch=async()=>({body:(async function*(){yield Buffer.alloc(1048577)})()});"
            result=subprocess.run(['node','-e',fake+args[-1]],input=input,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            raise RuntimeError('bounded client refused')
        with patch.object(q,'run',side_effect=oversized):
            with self.assertRaises(RuntimeError):q.local_request('synthetic-app','/api/health')


class CrossVersionBoundaryTests(unittest.TestCase):
    baseline='ghcr.io/leodotsinc/meeting-ai@sha256:'+'a'*64
    revision='b'*40

    def image_info(self):
        return {'Id':'sha256:'+'c'*64,'RepoDigests':[self.baseline],
                'Created':'2026-10-11T20:00:00Z','Config':{
                    'Env':['APP_VERSION=0.1.5','APP_REVISION='+self.revision,'APP_BUILD_ID=35489726673'],
                    'Labels':{'org.opencontainers.image.version':'0.1.5','org.opencontainers.image.revision':self.revision}}}

    def test_single_image_cli_is_backwards_compatible(self):
        args=q.parse_args(['candidate:local'])
        self.assertEqual(args.image,'candidate:local')
        self.assertIsNone(args.baseline_image)

    def test_incomplete_baseline_identity_refuses_execution(self):
        for args in (['candidate','--baseline-image',self.baseline],
                     ['candidate','--baseline-revision',self.revision],
                     ['candidate','--candidate-revision',self.revision]):
            with self.subTest(args=args),redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):q.parse_args(args)

    def test_mutable_baseline_refused_without_docker(self):
        with patch.object(q,'run') as run:
            with self.assertRaisesRegex(ValueError,'IMMUTABLE_BASELINE_REQUIRED'):
                q.image_metadata('ghcr.io/leodotsinc/meeting-ai:latest',immutable=True)
            run.assert_not_called()

    def test_inspected_identity_and_timestamp_bound_to_requested_baseline(self):
        with patch.object(q,'run',return_value=json.dumps([self.image_info()])):
            result=q.image_metadata(self.baseline,immutable=True,revision=self.revision,version='0.1.5',build_id='35489726673')
        self.assertEqual(result['image_id'],'sha256:'+'c'*64)
        self.assertEqual(result['build_timestamp'],'2026-10-11T20:00:00Z')

    def test_wrong_revision_build_label_or_registry_digest_rejected(self):
        cases=[({'revision':'d'*40},None),({'build_id':'wrong'},None),
               ({},'label'),({},'digest')]
        for options,mutation in cases:
            info=self.image_info()
            if mutation=='label':info['Config']['Labels']['org.opencontainers.image.version']='0.1.6'
            if mutation=='digest':info['RepoDigests']=[]
            with self.subTest(options=options,mutation=mutation),patch.object(q,'run',return_value=json.dumps([info])):
                with self.assertRaises(ValueError):q.image_metadata(self.baseline,immutable=True,**options)

    def test_identical_images_cannot_be_release_qualification(self):
        metadata={'image_id':'sha256:'+'a'*64,'revision':self.revision}
        with patch.object(q,'image_metadata',return_value=metadata),patch.object(q,'exercise_cross_version') as exercise:
            with self.assertRaisesRegex(ValueError,'DISTINCT_CANDIDATE_REQUIRED'):
                q.qualify_cross_version('candidate',self.baseline,self.revision)
            exercise.assert_not_called()

    def test_pending_migration_refused_before_starting_application_and_cleans_up(self):
        fixture=Mock()
        fixture.migration.side_effect=[None,{'catalog_sha256':'original'},ValueError('MIGRATION_COMPATIBILITY_FAILED')]
        baseline={'image_id':'baseline','revision':'baseline'}
        candidate={'image_id':'candidate','revision':'candidate'}
        with patch.object(q,'CrossVersionFixture',return_value=fixture):
            with self.assertRaisesRegex(ValueError,'MIGRATION_COMPATIBILITY_FAILED'):
                q.exercise_cross_version(baseline,candidate)
        fixture.start.assert_not_called()
        fixture.cleanup.assert_called_once_with()
        self.assertEqual(fixture.migration.call_args_list[-1].args,('candidate','preflight'))

    def test_changed_catalog_refused_before_starting_application(self):
        fixture=Mock()
        fixture.migration.side_effect=[None,{'catalog_sha256':'original'},{'catalog_sha256':'changed'}]
        with patch.object(q,'CrossVersionFixture',return_value=fixture):
            with self.assertRaisesRegex(ValueError,'BASELINE_SCHEMA_MISMATCH'):
                q.exercise_cross_version({'image_id':'baseline','revision':'baseline'},
                                         {'image_id':'candidate','revision':'candidate'})
        fixture.start.assert_not_called()
        fixture.cleanup.assert_called_once_with()

    def test_reads_do_not_refresh_the_saved_session(self):
        saved={'authjs.session-token':'baseline-jwt'}
        def response(app,path,data=None,cookies=None,**kwargs):
            cookies['authjs.session-token']='refreshed-jwt'
            return 200,b'[]',{}
        with patch.object(q,'local_request',side_effect=response):
            self.assertEqual(q.CrossVersionFixture().read('fixture','/api/projects',saved),[])
        self.assertEqual(saved,{'authjs.session-token':'baseline-jwt'})

    def test_snapshot_requires_every_fixture_table_to_have_data(self):
        fixture=q.CrossVersionFixture()
        with patch.object(fixture,'sql',return_value='[]'):
            with self.assertRaisesRegex(ValueError,'SYNTHETIC_TABLE_COVERAGE_MISSING'):fixture.rows()

    def test_candidate_writes_cannot_hide_collateral_data_corruption(self):
        before={table:[{'id':table+'-fixture','value':'preserved','title':'baseline'}] for table in q.FIXTURE_TABLES}
        allowed={'Meeting':{'title'}}
        good=deepcopy(before)
        good['Meeting'][0]['title']='candidate'
        good['Project'].append({'id':'new-project','value':'new','title':'new'})
        q.verify_data_transition(before,good,allowed,{'Project':1})
        for table in q.FIXTURE_TABLES:
            corrupted=deepcopy(good)
            corrupted[table][0]['value']='corrupted'
            with self.subTest(table=table):
                with self.assertRaisesRegex(ValueError,'UNEXPECTED_FIXTURE_DATA_CHANGE'):
                    q.verify_data_transition(before,corrupted,allowed,{'Project':1})
        extra=deepcopy(good)
        extra['User'].append({'id':'unexpected-user'})
        with self.assertRaisesRegex(ValueError,'UNEXPECTED_FIXTURE_ROW_COUNT'):
            q.verify_data_transition(before,extra,allowed,{'Project':1})

    def test_cleanup_failure_is_reported_after_trying_all_owned_resources(self):
        fixture=q.CrossVersionFixture()
        fixture.made=[('network','owned-network'),('container','owned-app')]
        failed=subprocess.CompletedProcess([],1,stderr='permission denied')
        with patch.object(q.subprocess,'run',return_value=failed) as run:
            with self.assertRaisesRegex(ValueError,'FIXTURE_CLEANUP_FAILED'):fixture.cleanup()
        self.assertEqual(run.call_count,2)
        self.assertEqual(run.call_args_list[0].args[0],['docker','container','rm','-f','owned-app'])
        self.assertEqual(run.call_args_list[1].args[0],['docker','network','rm','owned-network'])

    def test_cleanup_accepts_already_removed_one_shot_container(self):
        fixture=q.CrossVersionFixture();fixture.made=[('container','owned-migrator')]
        absent=subprocess.CompletedProcess([],1,stderr='Error response from daemon: No such container: owned-migrator')
        with patch.object(q.subprocess,'run',return_value=absent):fixture.cleanup()

    def test_cleanup_timeout_cannot_produce_success(self):
        fixture=q.CrossVersionFixture();fixture.made=[('container','owned-app')]
        with patch.object(q.subprocess,'run',side_effect=subprocess.TimeoutExpired('docker',30)):
            with self.assertRaisesRegex(ValueError,'FIXTURE_CLEANUP_FAILED'):fixture.cleanup()


if __name__=='__main__':unittest.main()
