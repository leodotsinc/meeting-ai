export interface ReleaseMetadata {
  version: string;
  revision: string;
  build_id: string;
}

type ReleaseEnvironment = Record<string, string | undefined>;

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$/;
const REVISION = /^[0-9a-f]{40}$/;
const BUILD_ID = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/;

/** Read only the public release identifiers supplied to the server image. */
export function readReleaseMetadata(
  environment: ReleaseEnvironment = process.env,
): ReleaseMetadata | null {
  const version = environment.APP_VERSION;
  const revision = environment.APP_REVISION;
  const buildId = environment.APP_BUILD_ID;

  if (
    !version || version.length > 128 || !SEMVER.test(version) ||
    !revision || !REVISION.test(revision) ||
    !buildId || !BUILD_ID.test(buildId)
  ) {
    return null;
  }

  return { version, revision, build_id: buildId };
}
