#!/usr/bin/env python3
"""Refuse unqualified maintenance reaching the ordinary release pipeline.

Metadata comes from GitHub's associated-PR API and every unreleased commit,
not a commit author string. There is deliberately no calendar bypass: the
Cloudbox scheduler has not yet qualified these applications.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(['git', *args], text=True, timeout=30).strip()


def maintenance_path(path):
    name = Path(path).name
    return (name in {'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
                     'Dockerfile', '.node-version', '.nvmrc', '.tool-versions',
                     'renovate.json', 'renovate.json5', 'dependabot.yml'}
            or name.endswith('.Dockerfile') or path.startswith('.github/workflows/'))


def maintenance_pr(pr):
    if not isinstance(pr, dict) or not isinstance(pr.get('user'), dict):
        raise ValueError('MAINTENANCE_METADATA_UNKNOWN')
    login = pr['user'].get('login')
    ref = pr.get('head', {}).get('ref', '')
    labels = pr.get('labels', [])
    if not isinstance(login, str) or not isinstance(ref, str) or not isinstance(labels, list):
        raise ValueError('MAINTENANCE_METADATA_UNKNOWN')
    return (login in {'renovate[bot]', 'dependabot[bot]', 'renovate-bot'}
            or ref.startswith(('renovate/', 'dependabot/'))
            or any(label.get('name') in {'dependencies', 'maintenance'} for label in labels))


def associated_prs(repository, revision):
    # Three pages is a hard bound, not an invitation to scan arbitrary history.
    result_prs = []
    for page in range(1, 4):
        result = subprocess.run(['gh', 'api',
                                 f'repos/{repository}/commits/{revision}/pulls?per_page=100&page={page}'],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError('MAINTENANCE_METADATA_UNKNOWN')
        prs = json.loads(result.stdout)
        if not isinstance(prs, list):
            raise ValueError('MAINTENANCE_METADATA_UNKNOWN')
        result_prs.extend(prs)
        if len(prs) < 100:
            return result_prs
    raise ValueError('MAINTENANCE_METADATA_LIMIT')


def inspect_range(repository, baseline, revision, lookup=associated_prs):
    commits = git('rev-list', baseline + '..' + revision).splitlines()
    if len(commits) > 100:
        raise ValueError('MAINTENANCE_RANGE_REQUIRES_REVIEW')
    # Examine individual commits too: a later revert or feature commit must not
    # hide a bot merge or dependency change in the unreleased history.
    for commit in commits:
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('MAINTENANCE_METADATA_UNKNOWN')
        paths = git('diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-m', commit).splitlines()
        if any(maintenance_path(path) for path in paths):
            raise ValueError('MAINTENANCE_NOT_QUALIFIED')
        if any(maintenance_pr(pr) for pr in lookup(repository, commit)):
            raise ValueError('MAINTENANCE_NOT_QUALIFIED')


def baseline_tag(tags, version_parser):
    stable = [tag for tag in tags
              if re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag)]
    if not stable:
        raise ValueError('MAINTENANCE_BASELINE_UNKNOWN')
    return max(stable, key=lambda tag: version_parser(tag[1:]))


def main():
    repository = os.environ['GITHUB_REPOSITORY']
    if repository != 'leodotsinc/meeting-ai':
        raise ValueError('UNEXPECTED_REPOSITORY')
    # Read-only image preparation remains available. A later rollout must run
    # this gate again; this mode grants no production authorization.
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and os.environ.get('PREPARE_ONLY') == 'true':
        print('MAINTENANCE_PREPARE_ONLY; production remains disabled')
        return
    spec = importlib.util.spec_from_file_location('release_selection', Path(__file__).with_name('select-release.py'))
    selector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selector)
    revision = git('rev-parse', 'HEAD')
    last = baseline_tag(git('tag', '--merged', revision).splitlines(), selector.stable_version)
    baseline = git('rev-parse', last + '^{commit}')
    selector.verify_published_release(repository, last, baseline)
    inspect_range(repository, baseline, revision)
    print('ORDINARY_RELEASE_SOURCE; no unqualified maintenance in unpublished history')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, TypeError, AttributeError):
        print('MAINTENANCE_GATE_REFUSED; inspect maintenance qualification, source history and GitHub metadata. No production action authorized.', file=sys.stderr)
        sys.exit(1)
