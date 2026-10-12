import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('reviewed_promotion', ROOT / 'scripts/reviewed-promotion.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
from release_manifest import create, record_deployment

REVISION = 'a' * 40
CONFIG = 'sha256:' + 'b' * 64
IMAGE = 'ghcr.io/leodotsinc/meeting-ai@sha256:' + 'c' * 64
CURRENT = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)
STAMP = CURRENT.isoformat()
POLICY = gate.policy()
CHECKS = ('same_database same_upload_volume baseline_authenticated candidate_reads_baseline_data '
          'candidate_preserves_baseline_session candidate_writes_verified rollback_reads_candidate_data '
          'rollback_preserves_baseline_session rollback_preserves_candidate_session rollback_writes_verified '
          'schema_unchanged migration_history_unchanged uploads_preserved recovery_verified').split()


def run(workflow='deploy.yml', event='workflow_dispatch', run_id=123, attempt=1):
    return {'id': run_id, 'run_attempt': attempt, 'path': '.github/workflows/' + workflow,
            'event': event, 'head_branch': 'main', 'head_sha': REVISION,
            'repository': {'full_name': gate.REPO}, 'head_repository': {'full_name': gate.REPO},
            'status': 'completed', 'conclusion': 'success', 'workflow_id': 9,
            'actor': POLICY['operator'].copy(), 'triggering_actor': POLICY['operator'].copy()}


def security(image=CONFIG):
    return {'schema_version': 1, 'image': image, 'status': 'passed', 'observed_at': STAMP,
            'database_updated_at': STAMP, 'counts': dict.fromkeys(('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'), 0),
            'findings': [], 'packages': 42, 'report_sha256': 'd' * 64}


def environment():
    return {'GITHUB_REPOSITORY': gate.REPO, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_SHA': REVISION, 'EXPECTED_SHA': REVISION, 'GITHUB_RUN_ID': '123',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_ACTOR': 'leodots', 'GITHUB_TRIGGERING_ACTOR': 'leodots',
            'GITHUB_ACTOR_ID': '22529012', 'REVIEWED_ACTION': 'prepare', 'PREPARE_ONLY': 'true'}


class OriginTests(unittest.TestCase):
    def test_actor_context_and_reruns_fail_closed(self):
        gate.dispatch_context(POLICY, environment())
        for key, value in {'GITHUB_ACTOR': 'other', 'GITHUB_ACTOR_ID': '42',
                           'GITHUB_TRIGGERING_ACTOR': 'other', 'GITHUB_RUN_ATTEMPT': '2',
                           'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_REF': 'refs/heads/feature',
                           'GITHUB_REPOSITORY': 'fork/meeting-ai', 'PREPARE_ONLY': 'false',
                           'REVIEWED_ACTION': 'monthly'}.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                gate.dispatch_context(POLICY, environment() | {key: value})

    def test_run_origin_failure_actor_and_attempt(self):
        original = run()
        gate.trusted_run(original, 'deploy.yml', REVISION, 'workflow_dispatch', operator=POLICY['operator'])
        cases = {'path': '.github/workflows/other.yml', 'event': 'push', 'head_branch': 'feature',
                 'head_sha': 'f' * 40, 'repository': {'full_name': 'fork/meeting-ai'},
                 'head_repository': {'full_name': 'fork/meeting-ai'}, 'conclusion': 'failure',
                 'status': 'in_progress', 'actor': {'login': 'leodots', 'id': 99},
                 'triggering_actor': {'login': 'stranger', 'id': 22529012}, 'run_attempt': 2}
        for field, value in cases.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.trusted_run(original | {field: value}, 'deploy.yml', REVISION, 'workflow_dispatch', operator=POLICY['operator'])

    def test_source_rejects_stale_main_before_baseline_or_ci(self):
        with patch.object(gate, 'git', return_value=REVISION), patch.object(gate, 'api', return_value={'sha': 'f' * 40}), patch.object(gate, 'verify_ci') as ci:
            with self.assertRaisesRegex(ValueError, 'STALE_MAIN'):
                gate.source(environment())
            ci.assert_not_called()

    def test_source_rejects_wrong_expected_sha(self):
        with patch.object(gate, 'git', return_value=REVISION), patch.object(gate, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'SOURCE_MISMATCH'):
                gate.source(environment() | {'EXPECTED_SHA': 'f' * 40})
            api.assert_not_called()

    def ci_api(self, current=None, jobs=None, listing=None, attempt=None):
        current = current or run('ci.yml', 'push', 456)
        job_rows = [{'name': name, 'status': 'completed', 'conclusion': 'success',
                     'head_sha': REVISION, 'run_id': 456} for name in
                    ('Tests, migrations and build', 'Exact image, authentication and safe migration bootstrap')]
        def api(endpoint):
            if endpoint.endswith('/ci.yml'):
                return {'path': '.github/workflows/ci.yml', 'id': 9}
            if '/runs?' in endpoint:
                self.assertNotIn('status=success', endpoint)
                return listing or {'total_count': 1, 'workflow_runs': [current]}
            if '/jobs?' in endpoint:
                return jobs if jobs is not None else {'total_count': 2, 'jobs': job_rows}
            if '/attempts/' in endpoint:
                return attempt or current
            return current
        return api

    def test_ci_requires_latest_exact_push_attempt_and_both_jobs(self):
        with patch.object(gate, 'api', side_effect=self.ci_api()):
            self.assertEqual(gate.verify_ci(REVISION), {'id': 456, 'attempt': 1})
        for change in ({'status': 'in_progress', 'conclusion': None}, {'conclusion': 'failure'},
                       {'event': 'workflow_dispatch'}, {'head_repository': {'full_name': 'fork/repo'}},
                       {'workflow_id': 99}, {'head_sha': 'f' * 40}):
            with self.subTest(change=change), patch.object(gate, 'api', side_effect=self.ci_api(current=run('ci.yml', 'push', 456) | change)):
                with self.assertRaises(ValueError):
                    gate.verify_ci(REVISION)
        with patch.object(gate, 'api', side_effect=self.ci_api(jobs={'total_count': 0, 'jobs': []})):
            with self.assertRaisesRegex(ValueError, 'JOBS_INCOMPLETE'):
                gate.verify_ci(REVISION)
        with patch.object(gate, 'api', side_effect=self.ci_api(attempt=run('ci.yml', 'push', 456, 2))):
            with self.assertRaisesRegex(ValueError, 'ATTEMPT_INVALID'):
                gate.verify_ci(REVISION)
        with patch.object(gate, 'api', side_effect=self.ci_api()):
            with self.assertRaisesRegex(ValueError, 'ATTEMPT_CHANGED'):
                gate.verify_ci(REVISION, {'id': 456, 'attempt': 2})

    def test_older_success_cannot_hide_newer_failed_ci(self):
        listing = {'total_count': 2, 'workflow_runs': [run('ci.yml', 'push', 455), run('ci.yml', 'push', 456)]}
        with patch.object(gate, 'api', side_effect=self.ci_api(current=run('ci.yml', 'push', 456) | {'conclusion': 'failure'}, listing=listing)):
            with self.assertRaisesRegex(ValueError, 'NOT_SUCCESSFUL'):
                gate.verify_ci(REVISION)

    def test_additional_application_changes_refused(self):
        for path in ('package.json', 'package-lock.json', 'docker/Dockerfile', 'prisma/schema.prisma', 'src/app/page.tsx', 'vendor/foo/index.js'):
            with self.subTest(path=path), patch.object(gate, 'command'), patch.object(gate, 'git', return_value=path):
                with self.assertRaisesRegex(ValueError, 'SCOPE_CHANGED'):
                    gate.cumulative_delta(POLICY, REVISION)


class BaselineTests(unittest.TestCase):
    def manifest(self):
        base = POLICY['baseline']
        return record_deployment(create(service='meeting-ai', compose_project='meeting-ai',
            application_kind='first_party', app_version=base['version'], release_version=base['version'],
            image=base['image'], git_sha=base['revision'], source_repository='https://github.com/' + gate.REPO,
            build_id=base['build_id'], build_attempt=1, built_at='2026-09-21T00:00:00Z'),
            status='verified', deployed_at='2026-09-21T00:01:00Z', verified_at='2026-09-21T00:02:00Z', observed_image=base['image'])

    def test_published_baseline_authentication_is_mandatory(self):
        publisher = Mock()
        publisher.verify_published_release.side_effect = ValueError('TAG_COMMIT_MISMATCH')
        with patch.object(gate, 'load_module', return_value=publisher), patch.object(gate, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'TAG_COMMIT_MISMATCH'):
                gate.verify_baseline(POLICY)
            api.assert_not_called()

    def test_baseline_image_original_build_asset_bytes_and_latest_tag(self):
        original = self.manifest()
        cases = [({}, 'v0.1.5', None),
                 ({'image': IMAGE, 'deployment': original['deployment'] | {'observed_image': IMAGE}}, 'v0.1.5', 'IDENTITY_INVALID'),
                 ({'build': original['build'] | {'id': '124'}}, 'v0.1.5', 'IDENTITY_INVALID'),
                 ({'build': original['build'] | {'attempt': 2}}, 'v0.1.5', 'IDENTITY_INVALID'),
                 ({}, 'v0.1.5\nv0.1.6', 'SUPERSEDED')]
        for update, tags, error in cases:
            raw = json.dumps(original | update).encode()
            publisher = Mock()
            publisher.verify_published_release.return_value = {'manifest_sha256': gate.digest(raw)}
            with self.subTest(update=update, tags=tags), patch.object(gate, 'load_module', return_value=publisher), \
                 patch.object(gate, 'api', return_value={'assets': [{'name': 'deployed-release.json', 'id': 7}]}), \
                 patch.object(gate, 'command', return_value=raw), patch.object(gate, 'git', return_value=tags):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        gate.verify_baseline(POLICY)
                else:
                    self.assertEqual(gate.verify_baseline(POLICY), POLICY['baseline'] | {'manifest_sha256': gate.digest(raw)})
        publisher.verify_published_release.return_value = {'manifest_sha256': '0' * 64}
        with patch.object(gate, 'load_module', return_value=publisher), \
             patch.object(gate, 'api', return_value={'assets': [{'name': 'deployed-release.json', 'id': 7}]}), \
             patch.object(gate, 'command', return_value=raw):
            with self.assertRaisesRegex(ValueError, 'BASELINE_CHANGED'):
                gate.verify_baseline(POLICY)


class ProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = {'revision': REVISION, 'ci': {'id': 456, 'attempt': 1}, 'cumulative_delta': {'tree': 'e' * 40}}
        self.release = create(service='meeting-ai', compose_project='meeting-ai', application_kind='first_party',
                              app_version='0.1.6', release_version='0.1.6', image=IMAGE, git_sha=REVISION,
                              source_repository='https://github.com/' + gate.REPO, build_id='123', build_attempt=1, built_at=STAMP.replace('+00:00', 'Z'))
        self.functional = {'schema_version': 2, 'observed_at': STAMP, 'harness_rehearsal_only': False, 'ok': True, 'image_id': CONFIG, 'version': '0.1.6', 'candidate_revision': REVISION,
                           'candidate_build_id': '123', 'baseline_image': POLICY['baseline']['image'],
                           'baseline_image_id': 'sha256:' + 'e' * 64, 'baseline_revision': POLICY['baseline']['revision'],
                           'baseline_version': '0.1.5', 'baseline_build_id': '35489726673',
                           'candidate_upgrade_and_downgrade_qualified': True, 'network_internal': True,
                           'ports_published': False, 'external_processing': False, 'production_restore': False,
                           'model_calls': 0, 'cross_version': {'sequence': ['baseline', 'candidate', 'baseline'],
                                                            'checks': dict.fromkeys(CHECKS, True)}}
        self.write('release.json', self.release)
        self.write('functional-proof.json', self.functional)
        for stage in ('runtime', 'builder', 'deps'):
            self.write(stage + '-security.json', security())
        self.proof = {'schema_version': 1, 'source': self.source, 'action': 'prepare',
                      'prepared_run': {'id': 123, 'attempt': 1}, 'created_at': STAMP,
                      'image': IMAGE, 'image_configs': dict.fromkeys(('runtime', 'builder', 'deps'), CONFIG)}
        self.seal()

    def write(self, filename, value):
        (self.directory / filename).write_text(json.dumps(value))

    def seal(self):
        self.proof['files_sha256'] = {f: gate.digest((self.directory / f).read_bytes()) for f in gate.FILES - {'reviewed-proof.json'}}
        self.write('reviewed-proof.json', self.proof)

    def verify(self, **kwargs):
        return gate.verify_bundle(self.directory, self.source, '123', '1', current=CURRENT, **kwargs)

    def test_complete_bundle_is_accepted_and_original_manifest_preserved(self):
        original = (self.directory / 'release.json').read_bytes()
        proof_hash = gate.digest((self.directory / 'reviewed-proof.json').read_bytes())
        self.verify(expected_hash=proof_hash)
        self.assertEqual(original, (self.directory / 'release.json').read_bytes())

    def test_wrong_reviewed_proof_hash(self):
        with self.assertRaisesRegex(ValueError, 'PROOF_HASH_MISMATCH'):
            self.verify(expected_hash='0' * 64)

    def test_tampered_or_missing_file(self):
        self.write('release.json', self.release | {'image': IMAGE[:-1] + 'a'})
        with self.assertRaisesRegex(ValueError, 'FILE_HASH_MISMATCH'):
            self.verify()
        (self.directory / 'runtime-security.json').unlink()
        with self.assertRaisesRegex(ValueError, 'BUNDLE_FILES_INVALID'):
            self.verify()

    def test_stale_proof_and_future_timestamp(self):
        for stamp in ((CURRENT - timedelta(hours=7)).isoformat(), (CURRENT + timedelta(seconds=1)).isoformat()):
            self.proof['created_at'] = stamp
            self.seal()
            with self.assertRaisesRegex(ValueError, 'EVIDENCE_STALE'):
                self.verify()

    def test_source_and_original_attempt_binding(self):
        for change in ({'source': {'revision': 'f' * 40}}, {'prepared_run': {'id': 124, 'attempt': 1}},
                       {'prepared_run': {'id': 123, 'attempt': 2}}, {'action': 'promote'}):
            original = self.proof.copy()
            self.proof.update(change)
            self.seal()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'PROOF_SOURCE_MISMATCH'):
                self.verify()
            self.proof = original

    def test_manifest_version_sha_build_and_image_binding(self):
        for change in ({'app_version': '0.1.0'}, {'git_sha': 'f' * 40},
                       {'build': self.release['build'] | {'id': '124'}}, {'image': IMAGE[:-1] + 'a'}):
            self.write('release.json', self.release | change)
            self.seal()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify()

    def test_functional_wrong_image_revision_or_rehearsal_refused(self):
        for change in ({'image_id': 'sha256:' + 'f' * 64}, {'candidate_revision': 'f' * 40},
                       {'version': '0.1.0'}, {'baseline_image': IMAGE}, {'baseline_build_id': '124'},
                       {'candidate_build_id': '124'}, {'candidate_upgrade_and_downgrade_qualified': False},
                       {'harness_rehearsal_only': True}, {'harness_rehearsal_only': None}, {'schema_version': None},
                       {'observed_at': (CURRENT - timedelta(hours=7)).isoformat()}, {'observed_at': None}, {'network_internal': False}, {'ports_published': True}):
            self.write('functional-proof.json', self.functional | change)
            self.seal()
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify()

    def test_each_cross_version_check_required(self):
        for check in CHECKS:
            altered = copy.deepcopy(self.functional)
            altered['cross_version']['checks'][check] = False
            self.write('functional-proof.json', altered)
            self.seal()
            with self.subTest(check=check), self.assertRaisesRegex(ValueError, 'CHECKS_INCOMPLETE'):
                self.verify()

    def test_scan_identities_freshness_and_unknown_fail_closed(self):
        cases = [security() | {'image': 'sha256:' + 'e' * 64}, security() | {'status': 'unknown'},
                 security() | {'counts': security()['counts'] | {'HIGH': 1}},
                 security() | {'counts': security()['counts'] | {'CRITICAL': 1}},
                 security() | {'counts': security()['counts'] | {'UNKNOWN': 1}},
                 security() | {'observed_at': (CURRENT - timedelta(hours=7)).isoformat()},
                 security() | {'database_updated_at': (CURRENT - timedelta(hours=25)).isoformat()},
                 security() | {'findings': [{'Severity': 'HIGH'}]}, security() | {'findings': [{}]},
                 security() | {'findings': [{'Severity': 'UNRECOGNIZED'}]},
                 security() | {'findings': [{'Severity': 'MEDIUM'}]}, security() | {'packages': 0}]
        for stage in ('runtime', 'builder', 'deps'):
            for report in cases:
                self.write(stage + '-security.json', report)
                self.seal()
                with self.subTest(stage=stage, report=report), self.assertRaises(ValueError):
                    self.verify()
            self.write(stage + '-security.json', security())

    def test_promotion_rechecks_ci_after_registry_inspection(self):
        with patch.object(gate, 'source', return_value=self.source), \
             patch.dict(os.environ, {'REVIEWED_ACTION': 'promote', 'PREPARED_RUN_ID': '123',
                                     'PREPARED_ATTEMPT': '1', 'EXPECTED_PROOF_SHA256': 'f' * 64}), \
             patch.object(gate, 'download_bundle'), \
             patch.object(gate, 'verify_bundle', return_value=(self.proof, self.release)), \
             patch.object(gate, 'verify_baseline_config'), \
             patch.object(gate, 'verify_prepared_run'), \
             patch.object(gate, 'verify_image', return_value=CONFIG) as image_check, \
             patch.object(gate, 'api') as api, \
             patch.object(gate.Path, 'write_bytes') as write:
            def newer_ci(*args):
                image_check.assert_called_once()
                raise ValueError('REVIEWED_CI_ATTEMPT_CHANGED')
            with patch.object(gate, 'verify_ci', side_effect=newer_ci):
                with self.assertRaisesRegex(ValueError, 'ATTEMPT_CHANGED'):
                    gate.promote(self.directory)
            api.assert_not_called()
            write.assert_not_called()

    def test_promotion_refuses_evidence_expiring_during_registry_inspection(self):
        clock = [CURRENT + timedelta(hours=5, minutes=59)]
        expected_hash = gate.digest((self.directory / 'reviewed-proof.json').read_bytes())
        def slow_registry(*args, **kwargs):
            clock[0] += timedelta(minutes=2)
            return CONFIG
        with patch.object(gate, 'source', return_value=self.source), \
             patch.dict(os.environ, {'REVIEWED_ACTION': 'promote', 'PREPARED_RUN_ID': '123',
                                     'PREPARED_ATTEMPT': '1', 'EXPECTED_PROOF_SHA256': expected_hash}), \
             patch.object(gate, 'download_bundle'), \
             patch.object(gate, 'verify_image', side_effect=slow_registry) as image_check, \
             patch.object(gate, 'verify_baseline_config'), \
             patch.object(gate, 'verify_prepared_run'), \
             patch.object(gate, 'verify_ci'), \
             patch.object(gate, 'api', return_value={'sha': REVISION}), \
             patch.object(gate, 'now', side_effect=lambda: clock[0]) as current_time, \
             patch.object(gate.Path, 'write_bytes') as write:
            with self.assertRaisesRegex(ValueError, 'EVIDENCE_STALE'):
                gate.promote(self.directory)
            image_check.assert_called_once()
            self.assertEqual(current_time.call_count, 2)
            write.assert_not_called()

    def test_baseline_config_must_match_the_pinned_registry_digest(self):
        with patch.object(gate, 'command', return_value=json.dumps({'config': {'digest': 'sha256:' + 'f' * 64}})):
            with self.assertRaisesRegex(ValueError, 'BASELINE_CONFIG_MISMATCH'):
                gate.verify_baseline_config(self.directory, POLICY)

    def test_registry_config_and_metadata_binding(self):
        labels = {'org.opencontainers.image.revision': REVISION, 'org.opencontainers.image.version': '0.1.6',
                  'org.opencontainers.image.source': 'https://github.com/' + gate.REPO}
        image = {'Id': CONFIG, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {'Labels': labels,
                 'Env': [f'APP_REVISION={REVISION}', 'APP_VERSION=0.1.6', 'APP_BUILD_ID=123']}}
        for update in ({}, {'Id': 'sha256:' + 'e' * 64}, {'Architecture': 'arm64'},
                       {'Config': image['Config'] | {'Env': ['APP_VERSION=0.1.0']}}):
            with patch.object(gate, 'command', side_effect=[json.dumps({'config': {'digest': CONFIG}}), json.dumps([image | update])]):
                if update:
                    with self.assertRaises(ValueError):
                        gate.verify_image(IMAGE, REVISION, '0.1.6', '123')
                else:
                    self.assertEqual(gate.verify_image(IMAGE, REVISION, '0.1.6', '123'), CONFIG)


class ArtifactTests(unittest.TestCase):
    def archive(self, bad_name=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name in gate.FILES:
                archive.writestr(bad_name if bad_name and name == 'release.json' else name, '{}')
        return stream.getvalue()

    def api(self, raw, *, changed=None, origin=None):
        artifact = {'id': 789, 'name': 'meeting-reviewed-123-1', 'expired': False,
                    'size_in_bytes': len(raw), 'digest': 'sha256:' + gate.digest(raw),
                    'workflow_run': {'id': 123, 'head_sha': REVISION, 'head_branch': 'main'}}
        artifact.update(changed or {})
        def api(endpoint):
            if '/artifacts?' in endpoint:
                return {'total_count': 1, 'artifacts': [artifact]}
            return origin or run()
        return api

    def test_download_requires_trusted_original_attempt_and_zip_digest(self):
        raw = self.archive()
        with tempfile.TemporaryDirectory() as temp, patch.object(gate, 'api', side_effect=self.api(raw)), patch.object(gate, 'command', return_value=raw):
            destination = Path(temp) / 'bundle'
            gate.download_bundle('123', '1', REVISION, destination)
            self.assertEqual({p.name for p in destination.iterdir()}, gate.FILES)
        for changed in ({'expired': True}, {'digest': 'sha256:' + 'f' * 64},
                        {'workflow_run': {'id': 124, 'head_sha': REVISION, 'head_branch': 'main'}},
                        {'workflow_run': {'id': 123, 'head_sha': 'f' * 40, 'head_branch': 'main'}}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp, patch.object(gate, 'api', side_effect=self.api(raw, changed=changed)), patch.object(gate, 'command', return_value=raw):
                with self.assertRaises(ValueError):
                    gate.download_bundle('123', '1', REVISION, Path(temp) / 'bundle')
        with patch.object(gate, 'api', side_effect=self.api(raw, origin=run(attempt=2))):
            with self.assertRaisesRegex(ValueError, 'RERUN_REFUSED'):
                gate.download_bundle('123', '1', REVISION, 'unused')

    def test_unexpected_archive_members_refused_before_writing(self):
        raw = self.archive('../release.json')
        with tempfile.TemporaryDirectory() as temp, patch.object(gate, 'api', side_effect=self.api(raw)), patch.object(gate, 'command', return_value=raw):
            destination = Path(temp) / 'bundle'
            with self.assertRaisesRegex(ValueError, 'CONTENT_INVALID'):
                gate.download_bundle('123', '1', REVISION, destination)
            self.assertFalse(destination.exists())

    def test_workflow_preserves_ordinary_guard_and_has_no_build_in_promotion(self):
        workflow = (ROOT / '.github/workflows/deploy.yml').read_text()
        self.assertIn("if: github.event_name != 'workflow_dispatch' || inputs.reviewed_action == 'ordinary'", workflow)
        self.assertIn('run: python3 scripts/maintenance-guard.py', workflow)
        promote = workflow.split('\n  promote:', 1)[1].split('\n  rollout:', 1)[0]
        self.assertNotIn('docker build', promote)
        self.assertIn('scripts/reviewed-promotion.py promote', promote)
        self.assertIn('packages: read', promote)
        self.assertNotIn('packages: write', promote)
        self.assertIn('path: release.json', promote)
        self.assertIn('needs: [gate, build, promote]', workflow)
        self.assertIn("always() && !cancelled() && needs.gate.result == 'success'", workflow)
        self.assertIn('result_artifact: ${{ needs.rollout.outputs.result_artifact }}', workflow)


if __name__ == '__main__':
    unittest.main()
