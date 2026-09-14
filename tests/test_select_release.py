import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('select_release',SCRIPTS/'select-release.py')
select_release=importlib.util.module_from_spec(spec)
spec.loader.exec_module(select_release)

class VersionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'GITHUB_REPOSITORY':'leodotsinc/meeting-ai'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.publication = patch.object(select_release, 'verify_published_release', return_value={'status':'published_verified'})
        self.verify_publication = self.publication.start()
        self.addCleanup(self.publication.stop)
        self.original=Path.cwd()
        self.temp=tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.git('init','-q')
        self.git('config','user.email','ci@example.invalid')
        self.git('config','user.name','Release fixture')
        Path('package.json').write_text(json.dumps({'version':'0.1.0'}))
        self.commit('initial source')
    def tearDown(self):
        os.chdir(self.original)
        self.temp.cleanup()
    def git(self,*args):
        return subprocess.check_output(['git',*args],text=True).strip()
    def commit(self,message):
        self.git('add','.')
        self.git('commit','--allow-empty','-qm',message)
    def test_initial_version_is_explicit_package_baseline(self):
        self.assertEqual(select_release.select()['version'],'0.1.0')
    def test_retry_of_published_commit_does_not_bump(self):
        self.git('tag','v0.1.0')
        self.assertEqual(select_release.select()['already_released'],'true')
        self.verify_publication.assert_called_once_with('leodotsinc/meeting-ai','v0.1.0',self.git('rev-parse','HEAD'))

    def test_tag_without_complete_publication_blocks_retry_before_build(self):
        self.git('tag','v0.1.0')
        self.verify_publication.side_effect=ValueError('TAGGED_RELEASE_PUBLICATION_INCOMPLETE')
        with self.assertRaisesRegex(ValueError,'TAGGED_RELEASE_PUBLICATION_INCOMPLETE'):
            select_release.select()
        self.assertEqual(self.git('tag'),'v0.1.0')

    def test_new_commit_cannot_bump_beyond_incomplete_last_publication(self):
        self.git('tag','v0.1.0')
        self.commit('fix: another change')
        self.verify_publication.side_effect=ValueError('PUBLISHED_MANIFEST_ASSET_MISMATCH')
        with self.assertRaisesRegex(ValueError,'PUBLISHED_MANIFEST_ASSET_MISMATCH'):
            select_release.select()

    def test_unavailable_publication_verification_is_not_already_released(self):
        self.git('tag','v0.1.0')
        self.verify_publication.side_effect=ValueError('GITHUB_API_UNAVAILABLE')
        with self.assertRaisesRegex(ValueError,'GITHUB_API_UNAVAILABLE'):
            select_release.select()
    def test_routine_and_feature_bumps_from_last_release(self):
        self.git('tag','v0.1.0')
        self.commit('fix: preserve settings')
        self.assertEqual(select_release.select()['version'],'0.1.1')
        self.commit('feat: show release version')
        self.assertEqual(select_release.select()['version'],'0.2.0')
    def test_breaking_change_has_highest_priority(self):
        self.git('tag','v0.1.0')
        self.commit('feat: dashboard')
        self.commit('refactor!: change integration API')
        self.assertEqual(select_release.select()['version'],'1.0.0')
    def test_unrelated_branch_tag_cannot_choose_production_version(self):
        branch=self.git('branch','--show-current')
        self.git('tag','v0.1.0')
        self.git('checkout','-qb','unreleased-fixture')
        self.commit('feat: unreleased')
        self.git('tag','v99.0.0')
        self.git('checkout','-q',branch)
        self.commit('fix: scoped')
        self.assertEqual(select_release.select()['version'],'0.1.1')

    def test_unreachable_target_version_collision_blocks_before_build(self):
        branch=self.git('branch','--show-current')
        self.git('tag','v0.1.0')
        self.git('checkout','-qb','unpublished-version')
        self.commit('fix: different source')
        self.git('tag','v0.1.1')
        self.git('checkout','-q',branch)
        self.commit('fix: production candidate')
        with self.assertRaises(ValueError): select_release.select()

if __name__=='__main__':unittest.main()
