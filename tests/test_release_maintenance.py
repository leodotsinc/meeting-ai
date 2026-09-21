import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance_guard', ROOT / 'scripts' / 'maintenance-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class MaintenanceGuardTests(unittest.TestCase):
    def test_no_published_baseline_refuses(self):
        for tags in ([], ['v1.0.0-beta'], ['arbitrary']):
            with self.assertRaisesRegex(ValueError, 'BASELINE_UNKNOWN'):
                guard.baseline_tag(tags, lambda version: tuple(map(int, version.split('.'))))

    def test_unmapped_ordinary_commit_has_no_implicit_bot_authorization(self):
        # Absence of PRs is allowed only after the dependency-path inspection.
        with patch.object(guard, 'git', side_effect=['a' * 40, 'package-lock.json']):
            with self.assertRaisesRegex(ValueError, 'NOT_QUALIFIED'):
                guard.inspect_range('test/repo', 'b' * 40, 'a' * 40, lambda *args: [])

    def test_metadata_pagination_is_bounded(self):
        response = subprocess.CompletedProcess([], 0, '[' + ','.join('{}' for _ in range(100)) + ']', '')
        with patch.object(guard.subprocess, 'run', return_value=response) as call:
            with self.assertRaisesRegex(ValueError, 'METADATA_LIMIT'):
                guard.associated_prs('test/repo', 'a' * 40)
            self.assertEqual(call.call_count, 3)

    def test_discovery_has_one_configured_owner(self):
        config = json.loads((ROOT / 'renovate.json').read_text())
        self.assertIs(config['enabled'], True)
        self.assertEqual(set(config['enabledManagers']), {'npm', 'dockerfile', 'github-actions'})
        self.assertFalse((ROOT / '.github/dependabot.yml').exists())

    def test_dependency_and_runtime_layers(self):
        for path in ('package.json', 'yarn.lock', 'package-lock.json', 'docker/Dockerfile',
                     '.node-version', '.github/workflows/ci.yml'):
            self.assertTrue(guard.maintenance_path(path), path)
        self.assertFalse(guard.maintenance_path('app/page.tsx'))

    def test_server_pr_metadata_not_commit_author(self):
        base = {'user': {'login': 'human'}, 'head': {'ref': 'feature/page'}, 'labels': []}
        self.assertFalse(guard.maintenance_pr(base))
        for altered in ({'user': {'login': 'renovate[bot]'}},
                        {'head': {'ref': 'dependabot/npm_and_yarn/lib-2'}},
                        {'labels': [{'name': 'dependencies'}]}):
            self.assertTrue(guard.maintenance_pr(base | altered))
        with self.assertRaises(ValueError):
            guard.maintenance_pr({})

    def test_prior_unreleased_commit_is_not_hidden_by_feature_commit(self):
        newer, older = 'a' * 40, 'b' * 40
        def git(*args):
            if args[0] == 'rev-list':
                return newer + '\n' + older
            return 'app/page.tsx' if args[-1] == newer else 'yarn.lock'
        with patch.object(guard, 'git', side_effect=git):
            with self.assertRaisesRegex(ValueError, 'NOT_QUALIFIED'):
                guard.inspect_range('test/repo', 'c' * 40, newer, lambda *args: [])

    def test_squash_merged_bot_is_refused(self):
        with patch.object(guard, 'git', side_effect=['a' * 40, 'app/page.tsx']):
            with self.assertRaisesRegex(ValueError, 'NOT_QUALIFIED'):
                guard.inspect_range('test/repo', 'b' * 40, 'a' * 40,
                                    lambda *args: [{'user': {'login': 'dependabot[bot]'}}])

    def test_metadata_failure_is_not_safe(self):
        with patch.object(guard.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            with self.assertRaises(ValueError):
                guard.associated_prs('test/repo', 'a' * 40)

    def test_ordinary_source_stays_allowed(self):
        with patch.object(guard, 'git', side_effect=['a' * 40, 'app/page.tsx']):
            guard.inspect_range('test/repo', 'b' * 40, 'a' * 40, lambda *args: [])

    def test_no_changes_need_no_metadata(self):
        with patch.object(guard, 'git', return_value=''):
            guard.inspect_range('test/repo', 'a' * 40, 'a' * 40, lambda *args: self.fail('unneeded API'))

    def test_workflows_scan_build_and_runtime_and_gate_before_version(self):
        ci = (ROOT / '.github/workflows/ci.yml').read_text()
        deploy = (ROOT / '.github/workflows/deploy.yml').read_text()
        for workflow in (ci, deploy):
            self.assertIn('maintenance-scan@', workflow)
            self.assertIn('--target builder', workflow)
            self.assertIn('retention-days: 7', workflow)
        self.assertLess(deploy.index('scripts/maintenance-guard.py'), deploy.index('scripts/select-release.py'))
        self.assertIn('pull-requests: read', deploy)


if __name__ == '__main__':
    unittest.main()
