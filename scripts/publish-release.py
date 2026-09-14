#!/usr/bin/env python3
"""Publish immutable tags and manifests after deployment; never overwrite them."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from release_manifest import read_json, validate


REPOSITORY = "leodotsinc/meeting-ai"
SHA = re.compile(r"[0-9a-f]{40}\Z")
MANIFEST_NAME = "deployed-release.json"
MANIFEST_LIMIT = 65536
IMAGE = re.compile(r"ghcr\.io/leodotsinc/meeting-ai@sha256:[0-9a-f]{64}\Z")


def require(condition, code):
    if not condition:
        raise ValueError(code)


def api_json(endpoint, *, method="GET", payload=None, allow_missing=False):
    """Only an explicit JSON HTTP 404 is absence; raw API output stays private."""
    command = ["gh", "api", "--include", "--method", method,
               "-H", "Accept: application/vnd.github+json", endpoint]
    if payload is not None:
        command += ["--input", "-"]
    try:
        result = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("GITHUB_API_UNAVAILABLE") from None
    header, separator, body = (result.stdout or "").replace("\r\n", "\n").partition("\n\n")
    first_line = header.split("\n", 1)[0]
    match = re.fullmatch(r"HTTP/(?:1\.[01]|2(?:\.0)?|3(?:\.0)?) ([0-9]{3})(?: .*)?", first_line)
    require(separator and match, "GITHUB_API_STATUS_UNAVAILABLE")
    status = int(match.group(1))
    if allow_missing and method == "GET" and status == 404:
        try:
            error = json.loads(body)
        except (ValueError, TypeError):
            raise ValueError("GITHUB_API_INVALID_RESPONSE") from None
        require(isinstance(error, dict) and error.get("message") == "Not Found", "GITHUB_API_INVALID_RESPONSE")
        return None
    require(result.returncode == 0 and status in (200, 201), "GITHUB_API_REQUEST_FAILED")
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        raise ValueError("GITHUB_API_INVALID_RESPONSE") from None
    require(isinstance(value, dict), "GITHUB_API_INVALID_RESPONSE")
    return value


def tag_commit(repository, tag, reference):
    """Peel annotated tags with a depth limit and cycle rejection."""
    require(reference.get("ref") == "refs/tags/" + tag, "TAG_REFERENCE_MISMATCH")
    target = reference.get("object")
    seen = set()
    for _ in range(8):
        require(isinstance(target, dict) and isinstance(target.get("sha"), str)
                and SHA.fullmatch(target["sha"]), "TAG_OBJECT_INVALID")
        if target.get("type") == "commit":
            return target["sha"]
        require(target.get("type") == "tag" and target["sha"] not in seen, "TAG_OBJECT_INVALID")
        seen.add(target["sha"])
        annotated = api_json(f"repos/{repository}/git/tags/{target['sha']}")
        require(annotated.get("sha") == target["sha"], "ANNOTATED_TAG_IDENTITY_MISMATCH")
        target = annotated.get("object")
    raise ValueError("ANNOTATED_TAG_DEPTH_EXCEEDED")


def verify_tag(repository, tag, revision):
    reference = api_json(f"repos/{repository}/git/ref/tags/{tag}")
    require(tag_commit(repository, tag, reference) == revision, "TAG_COMMIT_MISMATCH")


def verify_published_asset(repository, tag, revision, published, *, expected_bytes=None):
    """Read-only publication check; this is historical evidence, not live health."""
    verify_tag(repository, tag, revision)
    require(published.get("tag_name") == tag and published.get("draft") is False
            and published.get("prerelease") is False, "PUBLISHED_RELEASE_IDENTITY_MISMATCH")
    assets = published.get("assets")
    require(isinstance(assets, list) and all(isinstance(asset, dict) for asset in assets), "PUBLISHED_MANIFEST_ASSET_MISMATCH")
    assets = [asset for asset in assets if asset.get("name") == MANIFEST_NAME]
    require(len(assets) == 1 and type(assets[0].get("id")) is int and assets[0]["id"] > 0
            and type(assets[0].get("size")) is int and 0 < assets[0]["size"] <= MANIFEST_LIMIT,
            "PUBLISHED_MANIFEST_ASSET_MISMATCH")
    if expected_bytes is not None:
        require(assets[0]["size"] == len(expected_bytes), "PUBLISHED_MANIFEST_ASSET_MISMATCH")
    try:
        downloaded = subprocess.run(["gh", "api", "--method", "GET", "-H", "Accept: application/octet-stream",
            f"repos/{repository}/releases/assets/{assets[0]['id']}"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("PUBLISHED_MANIFEST_VERIFICATION_UNAVAILABLE") from None
    require(downloaded.returncode == 0 and isinstance(downloaded.stdout, bytes)
            and len(downloaded.stdout) == assets[0]["size"], "PUBLISHED_MANIFEST_BYTES_MISMATCH")
    if expected_bytes is not None:
        require(downloaded.stdout == expected_bytes, "PUBLISHED_MANIFEST_BYTES_MISMATCH")
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "PUBLISHED_MANIFEST_DUPLICATE_FIELD")
            value[key] = item
        return value
    try:
        manifest = validate(json.loads(downloaded.stdout, object_pairs_hook=unique_keys))
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise ValueError("PUBLISHED_MANIFEST_INVALID") from None
    require(manifest["service"] == manifest["compose_project"] == "meeting-ai"
            and manifest["application_kind"] == "first_party"
            and manifest["source_repository"] == "https://github.com/" + repository,
            "PUBLISHED_MANIFEST_SERVICE_MISMATCH")
    require(manifest["git_sha"] == revision and "v" + manifest["release_version"] == tag
            and manifest["app_version"] == manifest["release_version"]
            and manifest["deployment"]["status"] == "verified"
            and IMAGE.fullmatch(manifest["image"]), "PUBLISHED_MANIFEST_RELEASE_MISMATCH")
    verify_tag(repository, tag, revision)
    return {"ok": True, "tag": tag, "revision": revision, "status": "published_verified",
            "manifest_sha256": hashlib.sha256(downloaded.stdout).hexdigest()}


def verify_published_release(repository, tag, revision):
    """Do not treat a bare tag or an incomplete release as a completed release."""
    require(repository == REPOSITORY and re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag)
            and SHA.fullmatch(revision), "RELEASE_LOOKUP_IDENTITY_INVALID")
    repo = api_json(f"repos/{repository}")
    require(repo.get("full_name") == repository, "GITHUB_REPOSITORY_IDENTITY_MISMATCH")
    published = api_json(f"repos/{repository}/releases/tags/{tag}", allow_missing=True)
    require(published is not None, "TAGGED_RELEASE_PUBLICATION_INCOMPLETE")
    return verify_published_asset(repository, tag, revision, published)


def publish(path, environment=None):
    environment = os.environ if environment is None else environment
    manifest = validate(read_json(path))
    repository = environment.get("GITHUB_REPOSITORY")
    require(repository == REPOSITORY, "UNEXPECTED_REPOSITORY")
    require(manifest["service"] == "meeting-ai" and manifest["compose_project"] == "meeting-ai"
            and manifest["application_kind"] == "first_party"
            and manifest["source_repository"] == "https://github.com/" + repository,
            "RELEASE_SERVICE_IDENTITY_MISMATCH")
    require(manifest["deployment"]["status"] == "verified"
            and manifest["git_sha"] == environment.get("RELEASE_SHA")
            and manifest["release_version"] == environment.get("APP_VERSION")
            and manifest["app_version"] == manifest["release_version"],
            "UNVERIFIED_OR_MISMATCHED_RELEASE")
    artifact = Path(path)
    require(artifact.name == MANIFEST_NAME and artifact.is_file() and not artifact.is_symlink(), "RELEASE_MANIFEST_FILE_REQUIRED")
    expected_bytes = artifact.read_bytes()
    require(0 < len(expected_bytes) <= MANIFEST_LIMIT and IMAGE.fullmatch(manifest["image"]), "RELEASE_MANIFEST_IDENTITY_OR_SIZE_INVALID")
    revision = manifest["git_sha"]
    tag = "v" + manifest["release_version"]
    # Verify repository access before treating endpoint-level 404 as absence.
    repo = api_json(f"repos/{repository}")
    require(repo.get("full_name") == repository, "GITHUB_REPOSITORY_IDENTITY_MISMATCH")
    existing_release = api_json(f"repos/{repository}/releases/tags/{tag}", allow_missing=True)
    if existing_release is not None:
        result = verify_published_asset(repository, tag, revision, existing_release, expected_bytes=expected_bytes)
        return {**result, "already_published": True}
    reference = api_json(f"repos/{repository}/git/ref/tags/{tag}", allow_missing=True)
    if reference is not None:
        require(tag_commit(repository, tag, reference) == revision, "TAG_COMMIT_MISMATCH")
    else:
        # First mutation: exact commit, only after the preceding read-only checks.
        reference = api_json(f"repos/{repository}/git/refs", method="POST",
                             payload={"ref": "refs/tags/" + tag, "sha": revision})
        require(tag_commit(repository, tag, reference) == revision, "CREATED_TAG_COMMIT_MISMATCH")
    verify_tag(repository, tag, revision)
    notes = (f"Deployed release {tag}\n\nCommit: `{revision}`\n\nImage: `{manifest['image']}`\n\n"
             "Verified by the scoped deployment health and identity checks. "
             "This does not certify full audio processing or disaster recovery.\n")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", suffix=".md", prefix="meeting-release-") as note_file:
        note_file.write(notes)
        note_file.flush()
        try:
            result = subprocess.run(["gh", "release", "create", tag, str(artifact.resolve()),
                "--repo", repository, "--verify-tag", "--title", tag, "--notes-file", note_file.name],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("RELEASE_PUBLICATION_UNCERTAIN_TAG_PRESERVED") from None
        require(result.returncode == 0, "RELEASE_CREATE_FAILED_TAG_PRESERVED")
    published = api_json(f"repos/{repository}/releases/tags/{tag}")
    return verify_published_asset(repository, tag, revision, published, expected_bytes=expected_bytes)


if __name__ == '__main__':
    try:
        require(len(sys.argv) == 2, "EXPECTED_DEPLOYED_MANIFEST_PATH")
        print(json.dumps(publish(sys.argv[1])))
    except (ValueError, KeyError, OSError) as error:
        message = str(error)
        code = message if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", message) else "RELEASE_PUBLICATION_FAILED"
        print(code + "; no tag or release is deleted or overwritten.", file=sys.stderr)
        sys.exit(1)
