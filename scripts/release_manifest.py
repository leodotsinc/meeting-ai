#!/usr/bin/env python3
"""Stable SemVer and public release metadata; no Git, Docker, network or deploy.

`version --current 1.2.3 --commits-file commits.json` reads an array of complete
commit messages since the last *successfully deployed* reachable release tag.
An empty range reuses that version. The caller resolves Git history and uses its
reviewed initial version when there is no release tag. Tags are created only
after deployment verification, never by this tool. Build retries have distinct
build attempts, not a new app version.

`create` emits an allowlisted manifest with deployment.status=built. `deployment`
records caller-supplied verified/failed evidence. `validate` checks consistency,
not live health, migration safety, provenance, signatures or actual deployment.
Only a manifest externally verified against the running image may become the
current-release record. Keep failed attempt records separate from current.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys


SCHEMA_VERSION = 1
SEMVER = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
SERVICE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+\Z")
ROOT_KEYS = {
    "schema_version", "service", "compose_project", "application_kind",
    "app_version", "release_version", "version_scheme", "source_repository",
    "git_sha", "image", "build", "deployment",
}


class ContractError(ValueError):
    """Errors deliberately name fields rather than echoing supplied values."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def exact_keys(value, expected, field):
    require(isinstance(value, dict) and set(value) == expected, f"invalid {field} fields")


def stable_version(value):
    require(matches(SEMVER, value), "version must be stable SemVer X.Y.Z without prefix or leading zeroes")
    return tuple(int(component) for component in value.split("."))


def next_version(current, messages):
    """Routine changes PATCH, feat MINOR, conventional breaking changes MAJOR."""
    major, minor, patch = stable_version(current)
    require(isinstance(messages, list), "commit messages must be a JSON array")
    require(all(isinstance(message, str) and message.strip() and "\x00" not in message
                for message in messages), "commit messages must be nonempty strings without NUL")
    if not messages:
        return current
    bump = 0
    for message in messages:
        subject = message.splitlines()[0]
        breaking_subject = re.match(r"^[a-z][a-z0-9-]*(?:\([^()\r\n]+\))?!:\s+", subject, re.I)
        breaking_footer = re.search(r"^BREAKING[ -]CHANGE:\s*\S", message, re.M)
        if breaking_subject or breaking_footer:
            bump = 2
        elif re.match(r"^feat(?:\([^()\r\n]+\))?:\s+", subject, re.I):
            bump = max(bump, 1)
    result = f"{major + 1}.0.0" if bump == 2 else f"{major}.{minor + 1}.0" if bump == 1 else f"{major}.{minor}.{patch + 1}"
    stable_version(result)
    return result


def timestamp(value, field):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value), f"{field} must be a UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError(f"invalid {field} timestamp") from None


def validate(manifest):
    exact_keys(manifest, ROOT_KEYS, "manifest")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == SCHEMA_VERSION, "unsupported schema_version")
    for field in ("service", "compose_project"):
        require(matches(SERVICE, manifest[field]), f"invalid {field}")
    kind = manifest["application_kind"]
    require(kind in ("first_party", "third_party"), "invalid application_kind")
    require(matches(TOKEN, manifest["app_version"]), "invalid app_version")
    if kind == "first_party":
        require(manifest["version_scheme"] == "semver", "first-party version_scheme must be semver")
        stable_version(manifest["release_version"])
        require(matches(SHA, manifest["git_sha"]), "first-party git_sha must be a full lowercase Git SHA")
    else:
        require(manifest["version_scheme"] == "upstream", "third-party version_scheme must be upstream")
        require(manifest["release_version"] == manifest["app_version"], "third-party release_version must preserve app_version upstream")
        require(manifest["git_sha"] is None or matches(SHA, manifest["git_sha"]), "invalid git_sha")
    require(manifest["source_repository"] is None or matches(REPOSITORY, manifest["source_repository"]), "source_repository must be a credential-free HTTPS repository URL")
    require(matches(IMAGE, manifest["image"]), "image must be an exact registry/repository@sha256 reference without a tag")
    build = manifest["build"]
    built_at = None
    if build is None:
        require(kind == "third_party", "first-party build metadata is required")
    else:
        exact_keys(build, {"id", "attempt", "built_at"}, "build")
        require(matches(TOKEN, build["id"]), "invalid build.id")
        require(type(build["attempt"]) is int and 1 <= build["attempt"] <= 999999, "build.attempt must be a positive integer")
        built_at = timestamp(build["built_at"], "build.built_at")
    deployment = manifest["deployment"]
    exact_keys(deployment, {"status", "deployed_at", "verified_at", "observed_image"}, "deployment")
    status = deployment["status"]
    require(status in ("built", "verified", "failed"), "invalid deployment.status")
    if status == "built":
        require(all(deployment[key] is None for key in ("deployed_at", "verified_at", "observed_image")), "built metadata must not claim deployment evidence")
    else:
        deployed_at = timestamp(deployment["deployed_at"], "deployment.deployed_at")
        require(built_at is None or deployed_at >= built_at, "deployment precedes the build")
        if status == "verified":
            verified_at = timestamp(deployment["verified_at"], "deployment.verified_at")
            require(verified_at >= deployed_at, "verification precedes deployment")
            require(deployment["observed_image"] == manifest["image"], "verified deployment must observe the expected immutable image")
        else:
            require(deployment["verified_at"] is None, "failed deployment cannot claim verified_at")
            require(deployment["observed_image"] is None or matches(IMAGE, deployment["observed_image"]), "invalid failed deployment observed_image")
    return manifest


def create(*, service, compose_project, application_kind, app_version, release_version,
           image, git_sha=None, source_repository=None, build_id=None,
           build_attempt=None, built_at=None):
    build_values = (build_id, build_attempt, built_at)
    require(all(value is None for value in build_values) or all(value is not None for value in build_values), "build metadata must be complete or absent")
    return validate({
        "schema_version": SCHEMA_VERSION,
        "service": service,
        "compose_project": compose_project,
        "application_kind": application_kind,
        "app_version": app_version,
        "release_version": release_version,
        "version_scheme": "semver" if application_kind == "first_party" else "upstream",
        "source_repository": source_repository,
        "git_sha": git_sha,
        "image": image,
        "build": None if build_id is None else {"id": build_id, "attempt": build_attempt, "built_at": built_at},
        "deployment": {"status": "built", "deployed_at": None, "verified_at": None, "observed_image": None},
    })


def record_deployment(manifest, *, status, deployed_at, verified_at=None, observed_image=None):
    validate(manifest)
    require(manifest["deployment"]["status"] == "built", "record deployment from an unmodified build manifest; preserve previous evidence")
    require(status in ("verified", "failed"), "deployment status must be verified or failed")
    result = {**manifest, "deployment": {"status": status, "deployed_at": deployed_at,
              "verified_at": verified_at, "observed_image": observed_image}}
    return validate(result)


def read_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    try:
        content = Path(path).read_text()
        require(len(content) <= 2_000_000, "JSON input exceeds size limit")
        return json.loads(content, object_pairs_hook=unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ContractError("cannot read valid JSON input") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="derive stable SemVer from commit messages since a successful release")
    version.add_argument("--current", required=True)
    version.add_argument("--commits-file", required=True)
    build = commands.add_parser("create", help="emit public build identity, without deployment claims")
    for field in ("service", "compose-project", "application-kind", "app-version", "release-version", "image"):
        build.add_argument(f"--{field}", required=True)
    for field in ("git-sha", "source-repository", "build-id", "built-at"):
        build.add_argument(f"--{field}")
    build.add_argument("--build-attempt", type=int)
    check = commands.add_parser("validate", help="validate declared metadata; does not inspect the running app")
    check.add_argument("manifest")
    deploy = commands.add_parser("deployment", help="record caller-supplied deployment evidence")
    deploy.add_argument("manifest")
    deploy.add_argument("--status", choices=("verified", "failed"), required=True)
    deploy.add_argument("--deployed-at", required=True)
    deploy.add_argument("--verified-at")
    deploy.add_argument("--observed-image")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    try:
        if command == "version":
            print(next_version(args["current"], read_json(args["commits_file"])))
        elif command == "create":
            print(json.dumps(create(**args), indent=2) + "\n", end="")
        elif command == "deployment":
            manifest = read_json(args.pop("manifest"))
            print(json.dumps(record_deployment(manifest, **args), indent=2) + "\n", end="")
        else:
            validate(read_json(args["manifest"]))
            print("PASS: release metadata is consistent; live deployment is not checked by this tool.")
    except ContractError as error:
        print(f"release contract: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
