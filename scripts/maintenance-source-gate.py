#!/usr/bin/env python3
"""Adapted from Pluggy source gate 1af193714e457d53af3233aa0fb4023599e4a561.
Classify npm-only Git deltas; never authorize merge, build or deployment.

Run this trusted collector/classifier from outside candidate-controlled code.
The caller must obtain the verified production release from its protected store.
A PR-supplied snapshot or release is not trusted production evidence. No model,
registry request, checkout, package install or candidate code execution occurs.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse

SHA = re.compile(r'[0-9a-f]{40}\Z')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')
NAME = re.compile(r'(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*\Z')
ALLOWED = {'package.json', 'package-lock.json'}
GROUPS = ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')
LIMIT = 16 * 1024 * 1024


class Refusal(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def decode(raw):
    require(isinstance(raw, str) and len(raw.encode()) <= LIMIT, 'INPUT_SIZE_OR_TYPE')
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refusal('NONFINITE_JSON')))


def git_oid(kind, data):
    return hashlib.sha1(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def tree_oid(files):
    tree = {}
    for path, item in files.items():
        require(isinstance(path, str) and path and not any(ord(c) < 32 for c in path), 'INVALID_GIT_PATH')
        parts = path.split('/')
        require(all(p not in ('', '.', '..', '.git') for p in parts), 'INVALID_GIT_PATH')
        require(isinstance(item, dict) and set(item) == {'mode', 'oid'} and
                item['mode'] in {'100644', '100755', '120000'} and SHA.fullmatch(item['oid']), 'INVALID_GIT_ENTRY')
        node = tree
        for part in parts[:-1]:
            require(part not in node or isinstance(node[part], dict), 'GIT_PATH_COLLISION')
            node = node.setdefault(part, {})
        require(parts[-1] not in node, 'GIT_PATH_COLLISION')
        node[parts[-1]] = (item['mode'], item['oid'])

    def encode(node):
        body = b''
        for name in sorted(node, key=lambda n: (n + ('/' if isinstance(node[n], dict) else '')).encode()):
            value = node[name]
            mode, oid = ('40000', encode(value)) if isinstance(value, dict) else value
            body += mode.encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(oid)
        return git_oid('tree', body)
    return encode(tree)


def validate_snapshot(snapshot):
    require(isinstance(snapshot, dict) and set(snapshot) == {'schema_version', 'commit', 'tree', 'files', 'contents'},
            'SNAPSHOT_SCHEMA')
    require(type(snapshot['schema_version']) is int and snapshot['schema_version'] == 1 and
            isinstance(snapshot['commit'], str) and SHA.fullmatch(snapshot['commit']), 'SNAPSHOT_IDENTITY')
    require(isinstance(snapshot['files'], dict) and 2 <= len(snapshot['files']) <= 20000, 'SNAPSHOT_FILES')
    require(tree_oid(snapshot['files']) == snapshot['tree'], 'SNAPSHOT_TREE_MISMATCH')
    require(set(snapshot['contents']) == ALLOWED, 'SNAPSHOT_CONTENTS')
    parsed = {}
    for name in ALLOWED:
        item = snapshot['files'].get(name, {})
        raw = snapshot['contents'][name]
        require(item.get('mode') == '100644' and isinstance(raw, str), 'MANIFEST_NOT_REGULAR')
        require(git_oid('blob', raw.encode()) == item.get('oid'), 'MANIFEST_BLOB_MISMATCH')
        parsed[name] = decode(raw)
    return parsed['package.json'], parsed['package-lock.json']


def version(value):
    require(isinstance(value, str) and VERSION.fullmatch(value), 'STABLE_VERSION_REQUIRED')
    return tuple(map(int, value.split('.')))


def compatible(before, after):
    if before==after:return 'unchanged'
    a, b = version(before), version(after)
    require(b >= a, 'DOWNGRADE_REFUSED')
    require(a[0] == b[0] and (a[0] != 0 or a[1] == b[1]), 'MAJOR_OR_ZERO_MINOR_REFUSED')
    return 'minor' if a[1] != b[1] else 'patch' if a[2] != b[2] else 'unchanged'


def range_bound(spec):
    require(isinstance(spec, str), 'DEPENDENCY_SPEC_REQUIRED')
    prefix = spec[:1] if spec[:1] in ('^', '~') else ''
    return prefix, version(spec[len(prefix):])


def native_semver(edges):
    require(isinstance(edges,list) and len(edges)<=20000,'NATIVE_EDGE_LIMIT')
    raw=canonical(edges);require(len(raw)<=4*1024*1024,'NATIVE_EDGE_LIMIT')
    env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT')}
    env.update(HOME='/nonexistent',LANG='C',NODE_OPTIONS='',NODE_PATH='')
    result=subprocess.run(['node',str(Path(__file__).with_name('maintenance-semver.cjs'))],
        input=raw,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15,env=env)
    require(result.returncode==0 and len(result.stdout)<=128*1024,'NATIVE_SEMVER_UNAVAILABLE')
    values=decode(result.stdout.decode());require(isinstance(values,list) and len(values)==len(edges) and
        all(type(v)is bool for v in values),'NATIVE_SEMVER_RESULT')
    return values


def satisfies(spec, actual):
    return native_semver([[actual, spec]])[0]



def specs(before, after):
    require(isinstance(before, dict) and isinstance(after, dict) and set(before) == set(after), 'DEPENDENCY_SET_CHANGED')
    for name in before:
        require(NAME.fullmatch(name), 'DEPENDENCY_NAME')
        for value in (before[name], after[name]):
            require(isinstance(value, str) and not any(marker in value.lower() for marker in
                    ('http:', 'https:', 'git', 'file:', 'link:', 'workspace:', 'npm:', '\\')), 'DEPENDENCY_SOURCE_REFUSED')
        if before[name] != after[name]:
            _, a = range_bound(before[name]); _, b = range_bound(after[name])
            compatible('.'.join(map(str, a)), '.'.join(map(str, b)))


def package_name(path):
    parts = path.split('/node_modules/')
    parts[0] = parts[0].removeprefix('node_modules/')
    require(path.startswith('node_modules/') and all(NAME.fullmatch(p) for p in parts), 'LOCK_PACKAGE_PATH')
    return parts[-1]


def target(packages, origin, name):
    # Resolve npm's installed node_modules layout without executing Node/npm.
    directory = origin
    while True:
        candidate = (directory + '/' if directory else '') + 'node_modules/' + name
        if candidate in packages: return candidate
        if not directory: return None
        directory = directory.rsplit('/node_modules/', 1)[0] if '/node_modules/' in directory else ''


def effective_spec(package, origin, name, spec):
    # Meeting's existing exact root/nested overrides; never mutate their keys or
    # values during this maintenance scope. Unknown forms require review.
    overrides=package.get('overrides',{})
    require(isinstance(overrides,dict),'OVERRIDE_SCHEMA')
    for key,value in overrides.items():
        require(NAME.fullmatch(key),'OVERRIDE_SCOPE')
        if isinstance(value,dict):
            require(all(k=='.' or NAME.fullmatch(k) for k in value),'OVERRIDE_SCOPE')
            for bound in value.values():version(bound)
        else:version(value)
    root=overrides.get(name)
    selected=root.get('.') if isinstance(root,dict) else root
    if origin:
        parent=overrides.get(package_name(origin))
        if isinstance(parent,dict) and name in parent:selected=parent[name]
    return selected or spec


def validate_lock(package, lock):
    require(isinstance(package, dict) and isinstance(lock, dict), 'MANIFEST_OBJECT_REQUIRED')
    require(set(lock) <= {'name', 'version', 'lockfileVersion', 'requires', 'packages'} and
            type(lock.get('lockfileVersion')) is int and lock['lockfileVersion'] == 3 and
            lock.get('requires') is True, 'LOCKFILE_V3_REQUIRED')
    require(lock.get('name') == package.get('name') and lock.get('version') == package.get('version'), 'LOCK_ROOT_IDENTITY')
    require(isinstance(lock.get('packages'), dict) and '' in lock['packages'], 'LOCK_PACKAGES_REQUIRED')
    root = lock['packages']['']
    require(root.get('name') == package.get('name') and root.get('version') == package.get('version'), 'LOCK_ROOT_IDENTITY')
    for group in GROUPS:
        require(root.get(group, {}) == package.get(group, {}), 'LOCK_ROOT_DEPENDENCIES')
    require(not package.get('workspaces') and not root.get('workspaces'), 'WORKSPACES_REQUIRE_REVIEW')
    packages = lock['packages']
    for path, item in packages.items():
        require(isinstance(item, dict) and not item.get('link'), 'LINK_PACKAGE_REFUSED')
        if not path: continue
        name = package_name(path)
        require(isinstance(item.get('version'),str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?',item['version']), 'LOCK_VERSION_INVALID')
        require(item.get('name', name) == name and not item.get('bundledDependencies'), 'ALIASED_OR_BUNDLED_PACKAGE')
        if item.get('inBundle') is True:
            owner=path.rsplit('/node_modules/',1)[0]
            require(owner!=path and owner in packages and name in packages[owner].get('bundleDependencies',[]) and
                    'resolved' not in item and 'integrity' not in item, 'BUNDLE_OWNER_REQUIRED')
            for group in GROUPS:
                if group in item:specs(item[group],item[group])
            continue
        url = urllib.parse.urlsplit(item.get('resolved', ''))
        expected = '/' + name + '/-/' + name.rsplit('/', 1)[-1] + '-' + item['version'] + '.tgz'
        require(url.scheme == 'https' and url.netloc == 'registry.npmjs.org' and
                urllib.parse.unquote(url.path) == expected and not url.query and not url.fragment, 'NONREGISTRY_PACKAGE')
        integrity = item.get('integrity', '')
        require(isinstance(integrity, str) and integrity.startswith('sha512-'), 'STRONG_INTEGRITY_REQUIRED')
        try: value = base64.b64decode(integrity[7:], validate=True)
        except ValueError: raise Refusal('INVALID_INTEGRITY') from None
        require(len(value) == 64 and base64.b64encode(value).decode() == integrity[7:], 'INVALID_INTEGRITY')
        for group in GROUPS:
            if group in item: specs(item[group], item[group])
    edges=[]
    for origin, item in packages.items():
        for group in GROUPS:
            for name, spec in item.get(group, {}).items():
                resolved = target(packages, origin, name)
                optional = group == 'optionalDependencies' or (
                    group == 'peerDependencies' and item.get('peerDependenciesMeta', {}).get(name, {}).get('optional') is True)
                require(resolved is not None or optional, 'LOCK_EDGE_MISSING')
                if resolved is not None:
                    edges.append([packages[resolved]['version'],effective_spec(package,origin,name,spec)])
    require(all(native_semver(edges)), 'LOCK_EDGE_VERSION_MISMATCH')
    return packages


def classify_source(before, after):
    """Classify immutable source only; consumers must bind the live baseline."""
    old_package, old_lock = validate_snapshot(before)
    new_package, new_lock = validate_snapshot(after)
    require(before['commit'] != after['commit'], 'CANDIDATE_EQUALS_BASE')
    paths = sorted(path for path in before['files'].keys() | after['files'].keys()
                   if before['files'].get(path) != after['files'].get(path))
    require(paths and set(paths) <= ALLOWED, 'NON_DEPENDENCY_SOURCE_DELTA')
    version(old_package.get('version'));version(new_package.get('version'))
    require(old_package['version']==new_package['version'], 'PACKAGE_VERSION_IS_NOT_RELEASE_VERSION')
    require({k: v for k, v in old_package.items() if k not in (*GROUPS, 'version')} ==
            {k: v for k, v in new_package.items() if k not in (*GROUPS, 'version')}, 'PACKAGE_BEHAVIOR_CHANGED')
    for group in GROUPS: specs(old_package.get(group, {}), new_package.get(group, {}))
    old, new = validate_lock(old_package, old_lock), validate_lock(new_package, new_lock)
    require(set(old) == set(new), 'LOCK_GRAPH_CHANGED')
    changes = []
    for path in sorted(old):
        if not path:
            require({k: v for k, v in old[path].items() if k not in (*GROUPS, 'version')} ==
                    {k: v for k, v in new[path].items() if k not in (*GROUPS, 'version')}, 'ROOT_LOCK_METADATA_CHANGED')
            continue
        left, right = old[path], new[path]
        if left.get('inBundle') or left.get('bundleDependencies'):
            require(left==right,'BUNDLED_SUBTREE_UPDATE_REQUIRES_REVIEW')
        mutable = {'version', 'resolved', 'integrity', *GROUPS}
        require({k: v for k, v in left.items() if k not in mutable} ==
                {k: v for k, v in right.items() if k not in mutable}, 'DEPENDENCY_BEHAVIOR_METADATA_CHANGED')
        for group in GROUPS: specs(left.get(group, {}), right.get(group, {}))
        change = compatible(left['version'], right['version'])
        if change == 'unchanged':
            require(left == right, 'SAME_VERSION_CONTENT_OR_METADATA_CHANGED')
        else:
            require(left['integrity'] != right['integrity'], 'VERSION_CHANGE_SAME_INTEGRITY')
            require(not right.get('hasInstallScript') and not right.get('scripts'), 'INSTALL_SCRIPT_REQUIRES_REVIEW')
            changes.append({'path': path, 'name': package_name(path), 'before': left['version'],
                            'after': right['version'], 'change': change,
                            'integrity': right['integrity'], 'resolved': right['resolved']})
    require(changes, 'NO_VERSION_UPDATE')
    result = {'schema_version': 1, 'classification': 'npm_dependency_candidate',
              'base_commit': before['commit'], 'candidate_commit': after['commit'],
              'base_tree': before['tree'], 'candidate_tree': after['tree'],
              'changed_files': paths,
              'app_version_before': old_package['version'], 'app_version': new_package['version'],
              'changes': changes, 'change': 'minor' if any(c['change'] == 'minor' for c in changes) else 'patch',
              'authorized_to_apply': False,
              'boundary': 'Source classification only; the consumer must authenticate the CI proof and bind the protected production baseline.'}
    result['delta_sha256'] = sha256(result)
    return result


def classify(before, after, verified_release):
    require(isinstance(verified_release, dict) and verified_release.get('service') == 'meeting-ai' and
            verified_release.get('deployment', {}).get('status') == 'verified' and
            verified_release.get('git_sha') == before.get('commit') and
            verified_release.get('source_repository') == 'https://github.com/leodotsinc/meeting-ai' and
            re.fullmatch(r'ghcr.io/leodotsinc/meeting-ai@sha256:[0-9a-f]{64}', verified_release.get('image', '')),
            'VERIFIED_PRODUCTION_BASE_REQUIRED')
    result = classify_source(before, after)
    result['verified_release_sha256'] = sha256(verified_release)
    result['boundary'] = 'Trusted Git snapshots and protected verified release required; CI, package artifact review, security, backup, window and runtime gates are separate.'
    result.pop('delta_sha256')
    result['delta_sha256'] = sha256(result)
    return result


def collect(repository, commit):
    require(SHA.fullmatch(commit), 'FULL_COMMIT_REQUIRED')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), '--no-replace-objects', *args],
                                       stderr=subprocess.DEVNULL, timeout=30,
                                       env=os.environ | {'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'})
    raw = git('cat-file', 'commit', commit)
    require(git_oid('commit', raw) == commit, 'COMMIT_OBJECT_MISMATCH')
    tree = raw.splitlines()[0].decode().removeprefix('tree ')
    require(SHA.fullmatch(tree), 'COMMIT_TREE_REQUIRED')
    files = {}
    for entry in git('ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if not entry: continue
        header, path = entry.split(b'\t', 1)
        mode, kind, oid = header.decode().split(' ')
        require(kind == 'blob', 'SUBMODULES_REQUIRE_REVIEW')
        files[path.decode()] = {'mode': mode, 'oid': oid}
    contents = {}
    for name in ALLOWED:
        require(name in files, 'MANIFEST_MISSING')
        require(int(git('cat-file', '-s', files[name]['oid'])) <= LIMIT, 'INPUT_SIZE_OR_TYPE')
        body = git('cat-file', 'blob', files[name]['oid'])
        require(len(body) <= LIMIT, 'INPUT_SIZE_OR_TYPE')
        contents[name] = body.decode()
    snapshot = {'schema_version': 1, 'commit': commit, 'tree': tree, 'files': files, 'contents': contents}
    validate_snapshot(snapshot)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    snapshot = commands.add_parser('snapshot')
    snapshot.add_argument('--repo', type=Path, required=True)
    snapshot.add_argument('--commit', required=True)
    compare = commands.add_parser('classify')
    compare.add_argument('--before', type=Path, required=True)
    compare.add_argument('--after', type=Path, required=True)
    compare.add_argument('--verified-release', type=Path, required=True)
    args = parser.parse_args()
    def read(path):
        require(path.stat().st_size <= LIMIT, 'INPUT_SIZE_OR_TYPE')
        return decode(path.read_text())
    result = collect(args.repo, args.commit) if args.command == 'snapshot' else classify(
        read(args.before), read(args.after), read(args.verified_release))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try: main()
    except Refusal as error:
        print('MAINTENANCE_SOURCE_REFUSED: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.SubprocessError):
        print('MAINTENANCE_SOURCE_REFUSED; no application or repository action authorized.', file=sys.stderr)
        raise SystemExit(2)
