#!/usr/bin/env python3
"""Select a stable per-app release; Git tags identify verified deployments only."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from release_manifest import stable_version, next_version

_spec = importlib.util.spec_from_file_location("meeting_publish_release", Path(__file__).with_name("publish-release.py"))
_publisher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_publisher)
verify_published_release = _publisher.verify_published_release


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def select():
    revision = git('rev-parse', 'HEAD')
    tags = [t for t in git('tag', '--merged', revision).splitlines()
            if re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', t)]
    tags.sort(key=lambda t: stable_version(t[1:]))
    last = tags[-1] if tags else None
    if last:
        if os.environ.get('GITHUB_REPOSITORY') != _publisher.REPOSITORY:
            raise ValueError('UNEXPECTED_REPOSITORY')
        last_revision = git('rev-parse', last + '^{commit}')
        # A bare tag can remain after successful deploy but interrupted release
        # publication. Never silently skip that run or bump beyond it. Recovery
        # reuses the original verified artifact, without a new image/version.
        verify_published_release(_publisher.REPOSITORY, last, last_revision)
        if last_revision == revision:
            return {'version': last[1:], 'revision': revision, 'already_released': 'true'}
    if last:
        messages = subprocess.check_output(['git', 'log', '--format=%B%x00', last + '..' + revision], text=True)
        version = next_version(last[1:], [m.strip() for m in messages.split('\0') if m.strip()])
    else:
        version = json.loads(Path('package.json').read_text())['version']
        stable_version(version)
    collision = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/tags/v' + version])
    if collision.returncode != 1:
        raise ValueError('target version tag already exists outside the selected release history')
    return {'version': version, 'revision': revision, 'already_released': 'false'}


if __name__ == '__main__':
    try:
        result = select()
        print(json.dumps(result))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                for key, value in result.items():
                    output.write(f'{key}={value}\n')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        message = str(error)
        code = message if re.fullmatch(r'[A-Z][A-Z0-9_]{2,80}', message) else 'RELEASE_SELECTION_FAILED'
        print(code + '; incomplete publication requires retrying the failed publication job with its original verified artifact, or manual review. No image is rebuilt and no tag is changed by selection.', file=sys.stderr)
        sys.exit(1)
