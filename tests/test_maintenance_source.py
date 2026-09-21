import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gate',ROOT/'scripts/maintenance-source-gate.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)

def item(name, version, **kwargs):
    return {'version': version,
            'resolved': f'https://registry.npmjs.org/{name}/-/{name.rsplit("/", 1)[-1]}-{version}.tgz',
            'integrity': 'sha512-' + base64.b64encode(hashlib.sha512((name + version).encode()).digest()).decode(),
            **kwargs}


def snapshot(package, lock, commit='a' * 40, files=None):
    contents = {'package.json': json.dumps(package, indent=2) + '\n',
                'package-lock.json': json.dumps(lock, indent=2) + '\n'}
    entries = copy.deepcopy(files or {'src/index.ts': {'mode': '100644', 'oid': gate.git_oid('blob', b'unchanged source')}})
    for name, text in contents.items():
        entries[name] = {'mode': '100644', 'oid': gate.git_oid('blob', text.encode())}
    return {'schema_version': 1, 'commit': commit, 'files': entries,
            'tree': gate.tree_oid(entries), 'contents': contents}


def fixture():
    package = {'name': 'synthetic', 'version': '0.4.2', 'private': True,
               'scripts': {'test': 'node --test'}, 'engines': {'node': '>=22'},
               'dependencies': {'alpha': '^1.1.0'}, 'devDependencies': {'gamma': '^0.4.1'}}
    lock = {'name': package['name'], 'version': package['version'], 'lockfileVersion': 3,
            'requires': True, 'packages': {
                '': {k: copy.deepcopy(package[k]) for k in ('name', 'version', 'dependencies', 'devDependencies')},
                'node_modules/alpha': item('alpha', '1.1.0', dependencies={'beta': '~2.3.0'}),
                'node_modules/beta': item('beta', '2.3.0'),
                'node_modules/gamma': item('gamma', '0.4.1', dev=True)}}
    release = {'service': 'meeting-ai', 'git_sha': 'a' * 40,
               'source_repository': 'https://github.com/leodotsinc/meeting-ai',
               'image': 'ghcr.io/leodotsinc/meeting-ai@sha256:' + 'c' * 64,
               'deployment': {'status': 'verified'}}
    return package, lock, release


class SourceGateTests(unittest.TestCase):
    def setUp(self):
        self.package,self.lock,self.release=fixture()
        self.before=snapshot(self.package,self.lock)
        self.new_package=copy.deepcopy(self.package);self.new_lock=copy.deepcopy(self.lock)
        self.new_lock['packages']['node_modules/alpha']=item('alpha','1.1.1',dependencies={'beta':'~2.3.0'})

    def after(self):return snapshot(self.new_package,self.new_lock,'b'*40)
    def classify(self):return gate.classify(self.before,self.after(),self.release)

    def test_lock_only_direct_transitive_and_dev_patch_need_no_package_bump(self):
        self.new_lock['packages']['node_modules/beta']=item('beta','2.3.1')
        self.new_lock['packages']['node_modules/gamma']=item('gamma','0.4.2',dev=True)
        result=self.classify()
        self.assertEqual(result['changed_files'],['package-lock.json'])
        self.assertEqual(result['app_version'],self.package['version'])
        self.assertEqual(len(result['changes']),3);self.assertFalse(result['authorized_to_apply'])
        self.assertEqual(result['delta_sha256'],gate.sha256({k:v for k,v in result.items() if k!='delta_sha256'}))

    def test_package_release_version_remains_owned_by_verified_tag_selector(self):
        self.new_package['version']=self.new_lock['version']=self.new_lock['packages']['']['version']='0.4.3'
        with self.assertRaisesRegex(gate.Refusal,'PACKAGE_VERSION_IS_NOT_RELEASE_VERSION'):self.classify()

    def test_semver_uses_npm_arborist_loose_ranges_and_preserves_prerelease(self):
        self.assertEqual(gate.native_semver([['4.3.2','>=3.0.0 || >=4.0.0 || insiders'],
            ['2.9.0','>=3.0.0 || >=4.0.0 || insiders'],['5.0.0-beta.32','^5.0.0-beta.32'],
            ['5.0.0-beta.32','^5.0.0'],['1.2.0','1 - 2']]),[True,False,True,False,True])
        for obj in (self.lock,self.new_lock):
            obj['packages']['node_modules/gamma']=item('gamma','0.4.1-beta.2',dev=True)
            obj['packages']['']['devDependencies']['gamma']='^0.4.1-beta.2'
        self.package['devDependencies']['gamma']=self.new_package['devDependencies']['gamma']='^0.4.1-beta.2'
        self.before=snapshot(self.package,self.lock);self.classify()
        self.new_lock['packages']['node_modules/gamma']=item('gamma','0.4.1-beta.3',dev=True)
        with self.assertRaisesRegex(gate.Refusal,'STABLE_VERSION'):self.classify()

    def test_major_zero_minor_downgrade_script_and_registry_drift_refuse(self):
        original=copy.deepcopy(self.new_lock)
        for name,version in [('alpha','2.0.0'),('beta','2.2.9'),('gamma','0.5.0')]:
            self.new_lock=copy.deepcopy(original)
            old=self.new_lock['packages']['node_modules/'+name]
            self.new_lock['packages']['node_modules/'+name]=item(name,version,**{k:v for k,v in old.items() if k not in ('version','resolved','integrity')})
            with self.subTest(name=name),self.assertRaises(gate.Refusal):self.classify()
        self.new_lock=copy.deepcopy(original);self.new_lock['packages']['node_modules/alpha']['hasInstallScript']=True
        with self.assertRaises(gate.Refusal):self.classify()
        self.new_lock=copy.deepcopy(original);self.new_lock['packages']['node_modules/alpha']['resolved']='https://example.invalid/pkg.tgz'
        with self.assertRaisesRegex(gate.Refusal,'NONREGISTRY'):self.classify()

    def test_cumulative_unrelated_feature_and_baseline_drift_refuse(self):
        after=self.after();after['files']['src/new-feature.ts']={'mode':'100644','oid':'f'*40};after['tree']=gate.tree_oid(after['files'])
        with self.assertRaisesRegex(gate.Refusal,'NON_DEPENDENCY'):gate.classify(self.before,after,self.release)
        self.release['git_sha']='c'*40
        with self.assertRaisesRegex(gate.Refusal,'VERIFIED_PRODUCTION_BASE'):self.classify()

    def test_existing_overrides_are_exact_immutable_and_edge_validated(self):
        self.package['overrides']=self.new_package['overrides']={'alpha':{'beta':'2.4.0'}}
        for obj in (self.lock,self.new_lock):obj['packages']['node_modules/beta']=item('beta','2.4.0')
        self.before=snapshot(self.package,self.lock);self.classify()
        self.new_package['overrides']={'alpha':{'beta':'2.4.1'}}
        with self.assertRaisesRegex(gate.Refusal,'PACKAGE_BEHAVIOR'):self.classify()
        self.new_package=copy.deepcopy(self.package);self.new_lock['packages']['node_modules/beta']=item('beta','2.4.1')
        with self.assertRaisesRegex(gate.Refusal,'LOCK_EDGE_VERSION_MISMATCH'):self.classify()

    def test_existing_bundle_and_ancestor_cannot_change(self):
        for obj in (self.lock,self.new_lock):
            obj['packages']['node_modules/bundle']=item('bundle','1.0.0',bundleDependencies=['child'],dependencies={'child':'^1.0.0'})
            obj['packages']['node_modules/bundle/node_modules/child']={'version':'1.0.0','inBundle':True}
        self.before=snapshot(self.package,self.lock);self.classify()
        self.new_lock['packages']['node_modules/bundle/node_modules/child']['version']='1.0.1'
        with self.assertRaisesRegex(gate.Refusal,'BUNDLED_SUBTREE'):self.classify()

    def test_native_tool_is_bounded_and_ignores_node_injection_environment(self):
        with patch.dict(os.environ,{'NODE_OPTIONS':'--require=/missing/untrusted','NODE_PATH':'/untrusted'}):
            self.assertEqual(gate.native_semver([['1.0.0','^1']]),[True])
        with self.assertRaisesRegex(gate.Refusal,'EDGE_LIMIT'):gate.native_semver([['1.0.0','^1']]*20001)


if __name__=='__main__':unittest.main()
