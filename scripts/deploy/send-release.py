#!/usr/bin/env python3
"""Send only a validated release request over a preconfigured, pinned SSH session."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_manifest import read_json, validate


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('preflight', 'deploy'):
        raise ValueError('expected preflight|deploy and a build manifest')
    manifest = validate(read_json(sys.argv[2]))
    if (manifest['service'] != 'meeting-ai' or manifest['compose_project'] != 'meeting-ai'
        or manifest['application_kind'] != 'first_party'
        or manifest['git_sha'] != os.environ['RELEASE_SHA']
        or manifest['deployment']['status'] != 'built'):
        raise ValueError('release identity mismatch')
    host, user, port = [os.environ[name] for name in ('VPS_IP', 'DEPLOY_USER', 'SSH_PORT')]
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
        raise ValueError('invalid SSH target')
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('invalid SSH port')
    request = json.dumps({'action': sys.argv[1], 'release': manifest})
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=4', '-i', str(Path.home()/'.ssh/meeting_deploy'),
               '-p', port, user+'@'+host, 'meeting-release']
    result = subprocess.run(command, input=request, text=True, capture_output=True, timeout=1500)
    # Remote logs remain root-private; stdout is a small sanitized JSON response only.
    if result.returncode:
        raise RuntimeError('remote release operation failed; inspect protected server checkpoint')
    response = json.loads(result.stdout)
    if response.get('ok') is not True:
        raise RuntimeError('remote release operation was not verified')
    if sys.argv[1] == 'deploy':
        deployed = validate(response['release'])
        if (deployed['image'] != manifest['image'] or deployed['git_sha'] != manifest['git_sha']
            or deployed['release_version'] != manifest['release_version']
            or deployed['deployment']['status'] != 'verified'):
            raise ValueError('deployed release did not match request')
        Path('deployed-release.json').write_text(json.dumps(deployed, indent=2)+'\n')
    print(json.dumps({"ok": True, "action": sys.argv[1], "status": response.get("status"),
                      "release_version": manifest["release_version"], "image": manifest["image"],
                      "already_verified": response.get("already_verified", False)}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Release request failed; no credential or raw remote output is printed.', file=sys.stderr)
        sys.exit(1)
