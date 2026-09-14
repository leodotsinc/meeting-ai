import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("publish_release", SCRIPTS / "publish-release.py")
publish_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish_release)
from release_manifest import create, record_deployment

REVISION, OTHER, ANNOTATION = "a" * 40, "b" * 40, "c" * 40
REPO, TAG = "leodotsinc/meeting-ai", "v0.1.0"


def response(status, body):
    return subprocess.CompletedProcess([], 0 if status < 400 else 1,
        "HTTP/2.0 " + str(status) + " Test\r\nContent-Type: application/json\r\n\r\n" + json.dumps(body), "")


class FakeGitHub:
    def __init__(self, artifact, *, tag=None, release_exists=False):
        self.artifact, self.tag, self.release_exists = artifact, tag, release_exists
        self.calls, self.annotations = [], {}
        self.release_error = self.changed_asset = self.changed_after_publish = False
        self.missing_asset = self.duplicate_asset = self.draft = False

    def ref(self):
        return {"ref": "refs/tags/" + TAG, "object": self.tag}

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["gh", "release", "create"]:
            if self.release_error:
                return subprocess.CompletedProcess(args, 1, "", "private error omitted")
            assert "--verify-tag" in args and "--target" not in args
            self.release_exists = True
            if self.changed_after_publish:
                self.tag = {"type": "commit", "sha": OTHER}
            return subprocess.CompletedProcess(args, 0, "release-url", "")
        endpoint = next(arg for arg in args if arg.startswith("repos/"))
        method = args[args.index("--method") + 1]
        if endpoint == f"repos/{REPO}":
            return response(200, {"full_name": REPO})
        if endpoint == f"repos/{REPO}/releases/tags/{TAG}":
            if not self.release_exists:
                return response(404, {"message": "Not Found"})
            assets = [{"name": self.artifact.name, "id": 123, "size": len(self.artifact.read_bytes())}]
            if self.missing_asset: assets = []
            if self.duplicate_asset: assets *= 2
            return response(200, {"tag_name": TAG, "draft": self.draft, "prerelease": False, "assets": assets})
        if endpoint == f"repos/{REPO}/git/ref/tags/{TAG}":
            return response(200, self.ref()) if self.tag else response(404, {"message": "Not Found"})
        if endpoint == f"repos/{REPO}/git/refs" and method == "POST":
            payload = json.loads(kwargs["input"])
            assert payload == {"ref": "refs/tags/" + TAG, "sha": REVISION}
            self.tag = {"type": "commit", "sha": REVISION}
            return response(201, self.ref())
        if endpoint.startswith(f"repos/{REPO}/git/tags/"):
            return response(200, self.annotations[endpoint.rsplit("/", 1)[1]])
        if endpoint == f"repos/{REPO}/releases/assets/123":
            content = b"altered" if self.changed_asset else self.artifact.read_bytes()
            return subprocess.CompletedProcess(args, 0, content, b"")
        raise AssertionError("unexpected mocked GitHub operation")

    def writes(self):
        return [args for args in self.calls if args[:3] == ["gh", "release", "create"]
                or ("--method" in args and args[args.index("--method") + 1] != "GET")]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifact = Path(self.temp.name) / "deployed-release.json"
        built = create(service="meeting-ai", compose_project="meeting-ai", application_kind="first_party",
            app_version="0.1.0", release_version="0.1.0", git_sha=REVISION,
            source_repository="https://github.com/" + REPO,
            image="ghcr.io/leodotsinc/meeting-ai@sha256:" + "d" * 64,
            build_id="123", build_attempt=1, built_at="2026-09-14T03:00:00Z")
        self.manifest = record_deployment(built, status="verified", deployed_at="2026-09-14T03:01:00Z",
            verified_at="2026-09-14T03:02:00Z", observed_image=built["image"])
        self.artifact.write_text(json.dumps(self.manifest))
        self.environment = {"GITHUB_REPOSITORY": REPO, "RELEASE_SHA": REVISION, "APP_VERSION": "0.1.0"}

    def publish(self, api):
        with patch.object(publish_release.subprocess, "run", side_effect=api):
            return publish_release.publish(self.artifact, self.environment)

    def test_new_tag_is_created_exactly_then_release_asset_verified(self):
        api = FakeGitHub(self.artifact)
        self.assertEqual(self.publish(api)["status"], "published_verified")
        self.assertEqual(len(api.writes()), 2)
        self.assertEqual(api.tag, {"type": "commit", "sha": REVISION})

    def test_matching_tag_after_failed_publication_is_reused_without_force(self):
        api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION})
        self.publish(api)
        self.assertEqual(len(api.writes()), 1)
        self.assertEqual(api.writes()[0][:3], ["gh", "release", "create"])

    def test_tag_pointing_to_other_commit_blocks_before_any_mutation(self):
        api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": OTHER})
        with self.assertRaisesRegex(ValueError, "TAG_COMMIT_MISMATCH"):
            self.publish(api)
        self.assertEqual(api.writes(), [])

    def test_annotated_tag_is_peeled_and_checked(self):
        api = FakeGitHub(self.artifact, tag={"type": "tag", "sha": ANNOTATION})
        api.annotations[ANNOTATION] = {"sha": ANNOTATION, "object": {"type": "commit", "sha": REVISION}}
        self.publish(api)
        self.assertEqual(len(api.writes()), 1)

    def test_annotation_cycle_blocks_without_mutation(self):
        api = FakeGitHub(self.artifact, tag={"type": "tag", "sha": ANNOTATION})
        api.annotations[ANNOTATION] = {"sha": ANNOTATION, "object": {"type": "tag", "sha": ANNOTATION}}
        with self.assertRaisesRegex(ValueError, "TAG_OBJECT_INVALID"):
            self.publish(api)
        self.assertEqual(api.writes(), [])

    def test_annotation_depth_limit_blocks_without_mutation(self):
        identifiers = [f"{number:040x}" for number in range(1, 10)]
        api = FakeGitHub(self.artifact, tag={"type": "tag", "sha": identifiers[0]})
        for current, following in zip(identifiers, identifiers[1:]):
            api.annotations[current] = {"sha": current, "object": {"type": "tag", "sha": following}}
        with self.assertRaisesRegex(ValueError, "ANNOTATED_TAG_DEPTH_EXCEEDED"):
            self.publish(api)
        self.assertEqual(api.writes(), [])

    def test_identical_complete_existing_release_is_verified_without_overwrite(self):
        api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION}, release_exists=True)
        result = self.publish(api)
        self.assertTrue(result["already_published"])
        self.assertEqual(result["status"], "published_verified")
        self.assertEqual(api.writes(), [])

    def test_existing_release_with_missing_or_duplicate_asset_requires_review(self):
        for field in ("missing_asset", "duplicate_asset"):
            with self.subTest(field=field):
                api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION}, release_exists=True)
                setattr(api, field, True)
                with self.assertRaisesRegex(ValueError, "PUBLISHED_MANIFEST_ASSET_MISMATCH"):
                    self.publish(api)
                self.assertEqual(api.writes(), [])

    def test_existing_release_with_changed_bytes_never_gets_replaced(self):
        api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION}, release_exists=True)
        api.changed_asset = True
        with self.assertRaisesRegex(ValueError, "PUBLISHED_MANIFEST_BYTES_MISMATCH"):
            self.publish(api)
        self.assertEqual(api.writes(), [])

    def test_tag_alone_is_not_a_completed_publication(self):
        api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION})
        with patch.object(publish_release.subprocess, "run", side_effect=api):
            with self.assertRaisesRegex(ValueError, "TAGGED_RELEASE_PUBLICATION_INCOMPLETE"):
                publish_release.verify_published_release(REPO, TAG, REVISION)
        self.assertEqual(api.writes(), [])

    def test_selection_publication_verification_is_get_only(self):
        api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION}, release_exists=True)
        with patch.object(publish_release.subprocess, "run", side_effect=api):
            result = publish_release.verify_published_release(REPO, TAG, REVISION)
        self.assertEqual(result["status"], "published_verified")
        self.assertEqual(api.writes(), [])
        self.assertGreaterEqual(sum(any("application/octet-stream" in arg for arg in args) for args in api.calls), 1)

    def test_selection_checks_verified_manifest_version_sha_and_image(self):
        for change in ("version", "sha", "image", "status", "service"):
            with self.subTest(change=change):
                manifest = json.loads(json.dumps(self.manifest))
                if change == "version":
                    manifest["release_version"] = manifest["app_version"] = "0.1.1"
                if change == "sha": manifest["git_sha"] = OTHER
                if change == "image":
                    manifest["image"] = manifest["deployment"]["observed_image"] = "ghcr.io/other/app@sha256:" + "d" * 64
                if change == "status":
                    manifest["deployment"] = {"status":"built","deployed_at":None,"verified_at":None,"observed_image":None}
                if change == "service": manifest["service"] = "other"
                self.artifact.write_text(json.dumps(manifest))
                api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION}, release_exists=True)
                with patch.object(publish_release.subprocess, "run", side_effect=api):
                    with self.assertRaises(ValueError):
                        publish_release.verify_published_release(REPO, TAG, REVISION)
                self.assertEqual(api.writes(), [])

    def test_selection_rejects_duplicate_manifest_fields_and_draft_release(self):
        for fault in ("duplicate_field", "draft"):
            with self.subTest(fault=fault):
                self.artifact.write_text(json.dumps(self.manifest))
                api = FakeGitHub(self.artifact, tag={"type": "commit", "sha": REVISION}, release_exists=True)
                if fault == "duplicate_field":
                    raw = self.artifact.read_text()
                    self.artifact.write_text(raw[:-1] + ',"git_sha":"' + REVISION + '"}')
                else:
                    api.draft = True
                with patch.object(publish_release.subprocess, "run", side_effect=api):
                    with self.assertRaises(ValueError):
                        publish_release.verify_published_release(REPO, TAG, REVISION)
                self.assertEqual(api.writes(), [])

    def test_auth_and_server_errors_never_mean_absence(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                api = FakeGitHub(self.artifact)
                def fail_release_lookup(args, **kwargs):
                    if f"repos/{REPO}/releases/tags/{TAG}" in args:
                        api.calls.append(args)
                        return response(status, {"message": "redacted"})
                    return api(args, **kwargs)
                with self.assertRaisesRegex(ValueError, "GITHUB_API_REQUEST_FAILED"):
                    self.publish(fail_release_lookup)
                self.assertEqual(api.writes(), [])

    def test_network_error_blocks_without_mutation(self):
        with patch.object(publish_release.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            with self.assertRaisesRegex(ValueError, "GITHUB_API_UNAVAILABLE"):
                publish_release.publish(self.artifact, self.environment)

    def test_unrecognized_404_body_is_not_absence(self):
        with patch.object(publish_release.subprocess, "run", return_value=response(404, {"message": "proxy failure"})):
            with self.assertRaisesRegex(ValueError, "GITHUB_API_INVALID_RESPONSE"):
                publish_release.api_json("repos/example/release", allow_missing=True)

    def test_create_failure_preserves_explicit_tag_for_inspection_or_retry(self):
        api = FakeGitHub(self.artifact)
        api.release_error = True
        with self.assertRaisesRegex(ValueError, "RELEASE_CREATE_FAILED_TAG_PRESERVED"):
            self.publish(api)
        self.assertEqual(api.tag, {"type": "commit", "sha": REVISION})
        self.assertFalse(any("DELETE" in args or "PATCH" in args for args in api.calls))

    def test_post_publication_tag_movement_or_asset_mismatch_is_not_success(self):
        for failure, code in (("changed_after_publish", "TAG_COMMIT_MISMATCH"),
                              ("changed_asset", "PUBLISHED_MANIFEST_BYTES_MISMATCH")):
            with self.subTest(failure=failure):
                api = FakeGitHub(self.artifact)
                setattr(api, failure, True)
                with self.assertRaisesRegex(ValueError, code):
                    self.publish(api)

    def test_mismatched_source_version_or_identity_never_calls_github(self):
        for key, value in (("APP_VERSION", "0.1.1"), ("RELEASE_SHA", OTHER), ("GITHUB_REPOSITORY", "other/repo")):
            with self.subTest(key=key):
                original = self.environment[key]
                self.environment[key] = value
                with patch.object(publish_release.subprocess, "run") as run:
                    with self.assertRaises(ValueError):
                        publish_release.publish(self.artifact, self.environment)
                    run.assert_not_called()
                self.environment[key] = original


if __name__ == "__main__":
    unittest.main()
