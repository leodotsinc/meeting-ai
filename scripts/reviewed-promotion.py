#!/usr/bin/env python3
"""One reviewed v0.1.5 promotion. No GitHub, host or release mutations.

The ordinary maintenance guard remains separate. Preparation emits bounded
proof; promotion consumes that original run/attempt without rebuilding.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

from release_manifest import read_json, validate

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / '.github/reviewed-promotion.json'
REPO = 'leodotsinc/meeting-ai'
SHA = re.compile(r'[0-9a-f]{40}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
FILES = {'release.json', 'functional-proof.json', 'runtime-security.json',
         'builder-security.json', 'deps-security.json', 'reviewed-proof.json'}


def require(value, code):
    if not value:
        raise ValueError(code)


def command(args, *, binary=False, timeout=60):
    result = subprocess.run(args, capture_output=True, text=not binary, timeout=timeout)
    require(result.returncode == 0, 'REVIEWED_COMMAND_FAILED')
    return result.stdout


def git(*args):
    return command(['git', *args]).strip()


def api(endpoint):
    return json.loads(command(['gh', 'api', '--method', 'GET', endpoint]))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc)


def utc(value):
    require(isinstance(value, str), 'REVIEWED_TIMESTAMP_INVALID')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.utcoffset() is not None and result.utcoffset().total_seconds() == 0,
            'REVIEWED_TIMESTAMP_INVALID')
    return result


def fresh(value, age, current):
    elapsed = (current - utc(value)).total_seconds()
    require(0 <= elapsed <= age, 'REVIEWED_EVIDENCE_STALE')


def load_module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy():
    result = read_json(POLICY_PATH)
    require(result['repository'] == REPO and result['schema_version'] == 1,
            'REVIEWED_POLICY_INVALID')
    return result


def trusted_run(run, workflow, revision, event, *, complete=True, operator=None):
    require(isinstance(run, dict) and run.get('path') == '.github/workflows/' + workflow
            and run.get('event') == event and run.get('head_branch') == 'main'
            and run.get('head_sha') == revision
            and run.get('repository', {}).get('full_name') == REPO
            and run.get('head_repository', {}).get('full_name') == REPO
            and type(run.get('id')) is int and run['id'] > 0
            and type(run.get('run_attempt')) is int and run['run_attempt'] > 0,
            'REVIEWED_RUN_ORIGIN_INVALID')
    if complete:
        require(run.get('status') == 'completed' and run.get('conclusion') == 'success',
                'REVIEWED_RUN_NOT_SUCCESSFUL')
    if operator:
        for name in ('actor', 'triggering_actor'):
            require(all(run.get(name, {}).get(k) == v for k, v in operator.items()),
                    'REVIEWED_ACTOR_INVALID')
        # Reruns can skip the source gate or reuse older outputs. A new reviewed
        # dispatch is required; publication recovery retains its existing job.
        require(run['run_attempt'] == 1, 'REVIEWED_RERUN_REFUSED')
    return {'id': run['id'], 'attempt': run['run_attempt']}


def verify_ci(revision, identity=None):
    workflow = api(f'repos/{REPO}/actions/workflows/ci.yml')
    require(workflow.get('path') == '.github/workflows/ci.yml'
            and type(workflow.get('id')) is int, 'REVIEWED_CI_WORKFLOW_INVALID')
    # Include pending and failed runs: an older success cannot hide a newer CI.
    listing = api(f'repos/{REPO}/actions/workflows/ci.yml/runs?head_sha={revision}&event=push&per_page=100')
    runs = listing['workflow_runs']
    require(isinstance(runs, list) and runs and type(listing.get('total_count')) is int
            and listing['total_count'] <= 100, 'REVIEWED_CI_REQUIRED')
    latest = max(runs, key=lambda r: r['id'])
    run = api(f'repos/{REPO}/actions/runs/{latest["id"]}')
    selected = trusted_run(run, 'ci.yml', revision, 'push')
    require(run.get('workflow_id') == workflow['id'], 'REVIEWED_CI_WORKFLOW_INVALID')
    attempt = api(f'repos/{REPO}/actions/runs/{selected["id"]}/attempts/{selected["attempt"]}')
    require(trusted_run(attempt, 'ci.yml', revision, 'push') == selected
            and attempt.get('workflow_id') == workflow['id'], 'REVIEWED_CI_ATTEMPT_INVALID')
    if identity is not None:
        require(selected == identity, 'REVIEWED_CI_ATTEMPT_CHANGED')
    jobs = api(f'repos/{REPO}/actions/runs/{selected["id"]}/attempts/{selected["attempt"]}/jobs?per_page=100')
    expected = {'Tests, migrations and build', 'Exact image, authentication and safe migration bootstrap'}
    require(jobs.get('total_count') == 2 and len(jobs.get('jobs', [])) == 2
            and {j.get('name') for j in jobs['jobs']} == expected
            and all(j.get('status') == 'completed' and j.get('conclusion') == 'success'
                    and j.get('head_sha') == revision and j.get('run_id') == selected['id']
                    for j in jobs['jobs']), 'REVIEWED_CI_JOBS_INCOMPLETE')
    return selected


def verify_baseline(p):
    base = p['baseline']
    publisher = load_module('publish-release.py')
    result = publisher.verify_published_release(REPO, 'v' + base['version'], base['revision'])
    # The authenticated GitHub release asset must also identify the pinned image
    # and original build, not merely the tag's source revision.
    published = api(f'repos/{REPO}/releases/tags/v{base["version"]}')
    assets = [a for a in published['assets'] if a['name'] == 'deployed-release.json']
    require(len(assets) == 1, 'REVIEWED_BASELINE_ASSET_INVALID')
    raw = command(['gh', 'api', '--method', 'GET', '-H', 'Accept: application/octet-stream',
                   f'repos/{REPO}/releases/assets/{assets[0]["id"]}'], binary=True)
    require(digest(raw) == result['manifest_sha256'], 'REVIEWED_BASELINE_CHANGED')
    manifest = validate(json.loads(raw))
    require(manifest['image'] == base['image'] and manifest['build']['id'] == base['build_id']
            and manifest['build']['attempt'] == base['build_attempt'], 'REVIEWED_BASELINE_IDENTITY_INVALID')
    stable = [t for t in git('tag', '--merged', 'HEAD').splitlines()
              if re.fullmatch(r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', t)]
    require(max(stable, key=lambda t: tuple(map(int, t[1:].split('.')))) == 'v' + base['version'],
            'REVIEWED_BASELINE_SUPERSEDED')
    return {**base, 'manifest_sha256': result['manifest_sha256']}


def cumulative_delta(p, revision):
    base = p['baseline']['revision']
    for ancestor in (base, p['required_ancestor']):
        command(['git', 'merge-base', '--is-ancestor', ancestor, revision])
    # This is one reviewed delivery correction, not an open maintenance lane.
    # Every application/dependency/Docker/schema byte stays at the reviewed 2f
    # remediation; only this controller, harness and their tests may differ.
    allowed = {'.github/workflows/deploy.yml', '.github/reviewed-promotion.json',
               'scripts/reviewed-promotion.py', 'scripts/qualify-image.py',
               'tests/test_reviewed_promotion.py', 'tests/test_image_qualification.py', 'AGENTS.md'}
    changed = git('diff', '--name-only', p['required_ancestor'], revision).splitlines()
    require(set(changed) <= allowed, 'REVIEWED_SCOPE_CHANGED')
    commits = git('rev-list', '--reverse', base + '..' + revision).splitlines()
    require(0 < len(commits) <= 100 and all(SHA.fullmatch(c) for c in commits), 'REVIEWED_DELTA_INVALID')
    return {'baseline_revision': base, 'revision': revision,
            'tree': git('rev-parse', revision + '^{tree}'), 'commits': commits,
            'paths': git('diff', '--name-only', base, revision).splitlines(),
            'diff_sha256': digest(command(['git', 'diff', '--binary', '--full-index', base, revision], binary=True))}


def dispatch_context(p, environment):
    require(environment.get('GITHUB_REPOSITORY') == REPO
            and environment.get('GITHUB_REF') == 'refs/heads/main', 'REVIEWED_REPOSITORY_OR_REF_INVALID')
    for key, value in (('GITHUB_ACTOR', p['operator']['login']),
                       ('GITHUB_ACTOR_ID', str(p['operator']['id'])),
                       ('GITHUB_TRIGGERING_ACTOR', p['operator']['login'])):
        require(environment.get(key) == value, 'REVIEWED_ACTOR_INVALID')
    require(environment.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'REVIEWED_DISPATCH_REQUIRED')
    require(environment.get('GITHUB_RUN_ATTEMPT') == '1', 'REVIEWED_RERUN_REFUSED')
    require(environment.get('REVIEWED_ACTION') in ('prepare', 'promote'), 'REVIEWED_ACTION_INVALID')
    require(environment.get('PREPARE_ONLY') == ('true' if environment['REVIEWED_ACTION'] == 'prepare' else 'false'),
            'REVIEWED_PREPARE_MODE_INVALID')


def source(environment=None):
    environment = os.environ if environment is None else environment
    p = policy()
    dispatch_context(p, environment)
    revision = environment.get('EXPECTED_SHA', '')
    require(SHA.fullmatch(revision) and revision == environment.get('GITHUB_SHA') == git('rev-parse', 'HEAD'),
            'REVIEWED_SOURCE_MISMATCH')
    require(api(f'repos/{REPO}/commits/main').get('sha') == revision, 'REVIEWED_STALE_MAIN')
    run = api(f'repos/{REPO}/actions/runs/{environment["GITHUB_RUN_ID"]}')
    current = trusted_run(run, 'deploy.yml', revision, 'workflow_dispatch', complete=False, operator=p['operator'])
    require(str(current['id']) == environment['GITHUB_RUN_ID'], 'REVIEWED_RUN_ID_INVALID')
    return {'schema_version': 1, 'repository': REPO, 'revision': revision,
            'policy_sha256': digest(POLICY_PATH.read_bytes()), 'baseline': verify_baseline(p),
            'cumulative_delta': cumulative_delta(p, revision), 'ci': verify_ci(revision)}


def verify_image(image, revision, version, build_id, *, pull=False):
    require(re.fullmatch(r'ghcr\.io/leodotsinc/meeting-ai@sha256:[0-9a-f]{64}', image), 'REVIEWED_IMAGE_INVALID')
    if pull:
        command(['docker', 'pull', '--platform', 'linux/amd64', image], timeout=300)
    manifest = json.loads(command(['docker', 'manifest', 'inspect', image]))
    config = manifest.get('config', {}).get('digest', '')
    require(DIGEST.fullmatch(config), 'REVIEWED_REGISTRY_CONFIG_INVALID')
    inspect = json.loads(command(['docker', 'image', 'inspect', image]))
    require(len(inspect) == 1 and inspect[0]['Id'] == config
            and inspect[0]['Os'] == 'linux' and inspect[0]['Architecture'] == 'amd64', 'REVIEWED_IMAGE_CONFIG_MISMATCH')
    labels = inspect[0]['Config']['Labels']
    env = dict(v.split('=', 1) for v in inspect[0]['Config']['Env'])
    require(labels.get('org.opencontainers.image.revision') == env.get('APP_REVISION') == revision
            and labels.get('org.opencontainers.image.version') == env.get('APP_VERSION') == version
            and labels.get('org.opencontainers.image.source') == 'https://github.com/' + REPO
            and env.get('APP_BUILD_ID') == build_id, 'REVIEWED_IMAGE_METADATA_MISMATCH')
    return config


def verify_baseline_config(directory, p):
    functional = read_json(Path(directory) / 'functional-proof.json')
    manifest = json.loads(command(['docker', 'manifest', 'inspect', p['baseline']['image']]))
    require(manifest.get('config', {}).get('digest') == functional['baseline_image_id'],
            'REVIEWED_BASELINE_CONFIG_MISMATCH')


def verify_security(report, image_id, p, current):
    require(report.get('schema_version') == 1 and report.get('image') == image_id
            and DIGEST.fullmatch(image_id) and report.get('status') == 'passed', 'REVIEWED_SCAN_IDENTITY_INVALID')
    counts = report.get('counts', {})
    require(set(counts) == {'CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'}
            and all(type(n) is int and n >= 0 for n in counts.values())
            and counts['CRITICAL'] == counts['HIGH'] == counts['UNKNOWN'] == 0,
            'REVIEWED_SCAN_BLOCKED')
    findings = report.get('findings')
    require(isinstance(findings, list)
            and all(isinstance(f, dict) and f.get('Severity') in counts for f in findings),
            'REVIEWED_SCAN_FINDINGS_UNKNOWN')
    observed_counts = {severity: sum(f['Severity'] == severity for f in findings) for severity in counts}
    require(observed_counts == counts and type(report.get('packages')) is int and report['packages'] > 0
            and HASH.fullmatch(report.get('report_sha256', '')), 'REVIEWED_SCAN_INVALID')
    fresh(report['observed_at'], p['scan_max_age_seconds'], current)
    fresh(report['database_updated_at'], p['database_max_age_seconds'], current)
    require(utc(report['database_updated_at']) <= utc(report['observed_at']), 'REVIEWED_SCAN_DATABASE_INVALID')


def verify_functional(proof, config, source_proof, version, build_id, p, current):
    require(proof.get('schema_version') == 2 and proof.get('ok') is True and proof.get('image_id') == config
            and proof.get('version') == version
            and proof.get('candidate_revision') == source_proof['revision']
            and proof.get('candidate_build_id') == build_id
            and proof.get('baseline_build_id') == p['baseline']['build_id']
            and proof.get('harness_rehearsal_only') is False
            and proof.get('baseline_image') == p['baseline']['image']
            and proof.get('baseline_revision') == p['baseline']['revision']
            and proof.get('baseline_version') == p['baseline']['version']
            and DIGEST.fullmatch(proof.get('baseline_image_id', ''))
            and proof['baseline_image_id'] != config
            and proof.get('candidate_upgrade_and_downgrade_qualified') is True,
            'REVIEWED_FUNCTIONAL_IDENTITY_INVALID')
    fresh(proof.get('observed_at'), p['proof_max_age_seconds'], current)
    require(proof.get('network_internal') is True and proof.get('ports_published') is False
            and proof.get('external_processing') is False and proof.get('production_restore') is False
            and type(proof.get('model_calls')) is int and proof['model_calls'] == 0, 'REVIEWED_FUNCTIONAL_ISOLATION_INVALID')
    # Cross-version fields are emitted only after all three applications have
    # authenticated against the same synthetic data and upload volume.
    cross = proof.get('cross_version', {})
    checks = {'same_database', 'same_upload_volume', 'baseline_authenticated',
              'candidate_reads_baseline_data', 'candidate_preserves_baseline_session',
              'candidate_writes_verified', 'rollback_reads_candidate_data',
              'rollback_preserves_baseline_session', 'rollback_preserves_candidate_session',
              'rollback_writes_verified', 'schema_unchanged', 'migration_history_unchanged',
              'uploads_preserved', 'recovery_verified'}
    require(cross.get('sequence') == ['baseline', 'candidate', 'baseline']
            and all(cross.get('checks', {}).get(check) is True for check in checks),
            'REVIEWED_FUNCTIONAL_CHECKS_INCOMPLETE')


def verify_bundle(directory, source_proof, run_id, attempt, *, expected_hash=None, current=None):
    directory = Path(directory)
    p, current = policy(), current or now()
    require({f.name for f in directory.iterdir()} == FILES
            and all(f.is_file() and not f.is_symlink() and f.stat().st_size <= 1_000_000 for f in directory.iterdir()),
            'REVIEWED_BUNDLE_FILES_INVALID')
    raw = (directory / 'reviewed-proof.json').read_bytes()
    if expected_hash is not None:
        require(HASH.fullmatch(expected_hash) and digest(raw) == expected_hash, 'REVIEWED_PROOF_HASH_MISMATCH')
    proof = read_json(directory / 'reviewed-proof.json')
    require(proof.get('schema_version') == 1 and proof.get('source') == source_proof
            and proof.get('prepared_run') == {'id': int(run_id), 'attempt': int(attempt)}
            and proof.get('action') == 'prepare', 'REVIEWED_PROOF_SOURCE_MISMATCH')
    fresh(proof['created_at'], p['proof_max_age_seconds'], current)
    hashes = proof.get('files_sha256', {})
    require(set(hashes) == FILES - {'reviewed-proof.json'}, 'REVIEWED_PROOF_FILES_INCOMPLETE')
    for filename, expected in hashes.items():
        require(expected == digest((directory / filename).read_bytes()), 'REVIEWED_FILE_HASH_MISMATCH')
    release = validate(read_json(directory / 'release.json'))
    require(release['service'] == release['compose_project'] == 'meeting-ai'
            and release['application_kind'] == 'first_party'
            and release['source_repository'] == 'https://github.com/' + REPO
            and release['git_sha'] == source_proof['revision']
            and release['app_version'] == release['release_version'] == p['target_version']
            and release['build']['id'] == str(run_id) and release['build']['attempt'] == int(attempt)
            and release['deployment']['status'] == 'built', 'REVIEWED_RELEASE_IDENTITY_INVALID')
    fresh(release['build']['built_at'], p['proof_max_age_seconds'], current)
    identities = proof.get('image_configs', {})
    require(set(identities) == {'runtime', 'builder', 'deps'}, 'REVIEWED_IMAGE_CONFIGS_INCOMPLETE')
    for stage, image_id in identities.items():
        verify_security(read_json(directory / (stage + '-security.json')), image_id, p, current)
    verify_functional(read_json(directory / 'functional-proof.json'), identities['runtime'], source_proof,
                      release['app_version'], str(run_id), p, current)
    require(proof.get('image') == release['image'], 'REVIEWED_IMAGE_MISMATCH')
    return proof, release


def prepare(directory):
    p = policy()
    dispatch_context(p, os.environ)
    require(os.environ['REVIEWED_ACTION'] == 'prepare', 'REVIEWED_PREPARE_REQUIRED')
    directory = Path(directory)
    source_proof = json.loads(os.environ['REVIEWED_SOURCE'])
    require(source_proof['revision'] == os.environ['GITHUB_SHA']
            and source_proof['cumulative_delta'] == cumulative_delta(p, os.environ['GITHUB_SHA'])
            and source_proof['policy_sha256'] == digest(POLICY_PATH.read_bytes()), 'REVIEWED_BUILD_SOURCE_MISMATCH')
    release = read_json(directory / 'release.json')
    config = verify_image(release['image'], source_proof['revision'], p['target_version'], os.environ['GITHUB_RUN_ID'])
    identities = {stage: read_json(directory / (stage + '-security.json'))['image'] for stage in ('runtime', 'builder', 'deps')}
    require(identities['runtime'] == config, 'REVIEWED_RUNTIME_SCAN_MISMATCH')
    proof = {'schema_version': 1, 'action': 'prepare', 'source': source_proof,
             'prepared_run': {'id': int(os.environ['GITHUB_RUN_ID']), 'attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])},
             'created_at': now().isoformat(), 'image': release['image'], 'image_configs': identities,
             'files_sha256': {f: digest((directory / f).read_bytes()) for f in sorted(FILES - {'reviewed-proof.json'})}}
    (directory / 'reviewed-proof.json').write_text(json.dumps(proof, indent=2, sort_keys=True) + '\n')
    verify_bundle(directory, source_proof, os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'])
    verify_baseline_config(directory, p)
    print('REVIEWED_PROOF_SHA256=' + digest((directory / 'reviewed-proof.json').read_bytes()))


def verify_prepared_run(run_id, attempt, revision):
    require(re.fullmatch(r'[1-9][0-9]{0,19}', run_id) and attempt == '1', 'REVIEWED_PREPARED_RUN_INVALID')
    p = policy()
    run = api(f'repos/{REPO}/actions/runs/{run_id}')
    expected = {'id': int(run_id), 'attempt': int(attempt)}
    require(trusted_run(run, 'deploy.yml', revision, 'workflow_dispatch', operator=p['operator']) == expected,
            'REVIEWED_PREPARED_ATTEMPT_CHANGED')
    require(trusted_run(api(f'repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}'), 'deploy.yml', revision,
                        'workflow_dispatch', operator=p['operator']) == expected, 'REVIEWED_PREPARED_ATTEMPT_INVALID')


def download_bundle(run_id, attempt, revision, directory):
    verify_prepared_run(run_id, attempt, revision)
    listing = api(f'repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100')
    require(type(listing.get('total_count')) is int and listing['total_count'] <= 100,
            'REVIEWED_ARTIFACT_LIMIT')
    artifacts = [a for a in listing['artifacts'] if a.get('name') == f'meeting-reviewed-{run_id}-{attempt}']
    require(len(artifacts) == 1, 'REVIEWED_ARTIFACT_REQUIRED')
    artifact = artifacts[0]
    require(artifact.get('expired') is False and artifact.get('workflow_run', {}).get('id') == int(run_id)
            and artifact['workflow_run'].get('head_sha') == revision
            and artifact['workflow_run'].get('head_branch') == 'main'
            and type(artifact.get('id')) is int and artifact['id'] > 0
            and type(artifact.get('size_in_bytes')) is int and 0 < artifact['size_in_bytes'] <= 2_000_000
            and DIGEST.fullmatch(artifact.get('digest', '')), 'REVIEWED_ARTIFACT_INVALID')
    raw = command(['gh', 'api', '--method', 'GET', f'repos/{REPO}/actions/artifacts/{artifact["id"]}/zip'], binary=True)
    require(len(raw) <= 2_000_000 and 'sha256:' + digest(raw) == artifact['digest'], 'REVIEWED_ARTIFACT_DIGEST_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) == len(FILES) and {m.filename for m in members} == FILES
                and all(not m.is_dir() and m.file_size <= 1_000_000
                        and (m.external_attr >> 16) & 0o170000 != 0o120000 for m in members),
                'REVIEWED_ARTIFACT_CONTENT_INVALID')
        Path(directory).mkdir(parents=True, exist_ok=False)
        for member in members:
            (Path(directory) / member.filename).write_bytes(archive.read(member))


def promote(directory):
    source_proof = source()
    p = policy()
    require(os.environ['REVIEWED_ACTION'] == 'promote', 'REVIEWED_PROMOTE_REQUIRED')
    run_id, attempt = os.environ['PREPARED_RUN_ID'], os.environ['PREPARED_ATTEMPT']
    download_bundle(run_id, attempt, source_proof['revision'], directory)
    proof, release = verify_bundle(directory, source_proof, run_id, attempt,
                                   expected_hash=os.environ['EXPECTED_PROOF_SHA256'])
    require(verify_image(release['image'], release['git_sha'], release['app_version'], run_id, pull=True)
            == proof['image_configs']['runtime'], 'REVIEWED_REGISTRY_CONFIG_MISMATCH')
    verify_baseline_config(directory, p)
    # Recheck after registry access/pull, which can take minutes. A new run or
    # rerun cannot hide behind the earlier successful source check.
    verify_prepared_run(run_id, attempt, source_proof['revision'])
    verify_ci(source_proof['revision'], proof['source']['ci'])
    # Last read before handing the original bytes to the unchanged transport.
    require(api(f'repos/{REPO}/commits/main').get('sha') == release['git_sha'], 'REVIEWED_STALE_MAIN')
    # Registry and API calls can cross an evidence TTL. Re-read and validate all
    # original bytes with a fresh clock immediately before emitting the manifest.
    verify_bundle(directory, source_proof, run_id, attempt,
                  expected_hash=os.environ['EXPECTED_PROOF_SHA256'])
    Path('release.json').write_bytes((Path(directory) / 'release.json').read_bytes())
    print('REVIEWED_PROMOTION_VERIFIED; original image and manifest preserved')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('source', 'prepare', 'promote'))
    parser.add_argument('--directory', default='reviewed-bundle')
    args = parser.parse_args()
    if args.command == 'source':
        result = source()
        print(json.dumps(result, sort_keys=True))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('proof=' + json.dumps(result, separators=(',', ':'), sort_keys=True) + '\n')
    elif args.command == 'prepare':
        prepare(args.directory)
    else:
        promote(args.directory)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        code = str(error)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{2,80}', code):
            code = 'REVIEWED_PROMOTION_REFUSED'
        print(code + '; no production action authorized.', file=sys.stderr)
        sys.exit(1)
