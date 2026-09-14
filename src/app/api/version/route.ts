import { NextResponse } from "next/server";
import { auth } from "../../../../auth";
import { readReleaseMetadata } from "@/lib/server/release";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const headers = { "Cache-Control": "private, no-store" };

export async function GET() {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers });
    }

    const release = readReleaseMetadata();
    if (!release) {
      return NextResponse.json(
        { error: "Release metadata unavailable" },
        { status: 503, headers },
      );
    }

    return NextResponse.json(release, { headers });
  } catch {
    return NextResponse.json(
      { error: "Release metadata unavailable" },
      { status: 503, headers },
    );
  }
}
