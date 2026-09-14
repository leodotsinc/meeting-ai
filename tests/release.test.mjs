import assert from "node:assert/strict";
import { test } from "node:test";
import { readReleaseMetadata } from "../src/lib/server/release.ts";

const release = {
  APP_VERSION: "1.2.3",
  APP_REVISION: "0123456789abcdef0123456789abcdef01234567",
  APP_BUILD_ID: "34650601965",
};

test("returns only the image release identifiers, never other environment values", () => {
  assert.deepEqual(readReleaseMetadata({ ...release, AUTH_SECRET: "not-for-output" }), {
    version: "1.2.3",
    revision: release.APP_REVISION,
    build_id: "34650601965",
  });
});

test("accepts SemVer prerelease and build metadata", () => {
  for (const version of ["0.0.0", "2.0.0-rc.1", "1.2.3+build.7", "1.2.3-alpha.0+local"]) {
    assert.equal(readReleaseMetadata({ ...release, APP_VERSION: version })?.version, version);
  }
});

test("does not invent a version when image metadata is missing", () => {
  assert.equal(readReleaseMetadata({}), null);
  for (const field of Object.keys(release)) {
    const missing = { ...release };
    delete missing[field];
    assert.equal(readReleaseMetadata(missing), null);
    assert.equal(readReleaseMetadata({ ...release, [field]: "" }), null);
  }
});

test("rejects tags and malformed SemVer instead of displaying a false release", () => {
  for (const version of ["latest", "v1.2.3", "1.2", "01.2.3", "1.2.3-01", "1.2.3-", "1.2.3 ", "1.2.3\n", "1.2.3+", "1.2.3+" + "x".repeat(128)]) {
    assert.equal(readReleaseMetadata({ ...release, APP_VERSION: version }), null, version);
  }
});

test("requires the full lowercase Git revision", () => {
  for (const revision of ["abc1234", "main", "g".repeat(40), "a".repeat(39), "a".repeat(41), "A".repeat(40), "a".repeat(40) + "\n"]) {
    assert.equal(readReleaseMetadata({ ...release, APP_REVISION: revision }), null, revision);
  }
});

test("accepts safe build identifiers and rejects paths, controls and oversized input", () => {
  assert.equal(readReleaseMetadata({ ...release, APP_BUILD_ID: "local-20260913.1" })?.build_id, "local-20260913.1");
  for (const buildId of ["../build", "run id", "build\n", "<script>", "x".repeat(129)]) {
    assert.equal(readReleaseMetadata({ ...release, APP_BUILD_ID: buildId }), null, buildId);
  }
});
