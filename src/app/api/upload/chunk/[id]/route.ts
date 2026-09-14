import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../../../auth";
import {
  CHUNK_SIZE_BYTES,
  isValidUploadId,
  readChunkedUploadMetadata,
  writeChunk,
} from "@/lib/server/chunked-upload";

async function handlePOST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { id } = await params;
    if (!isValidUploadId(id)) {
      return NextResponse.json({ error: "Invalid upload id" }, { status: 400 });
    }

    const metadata = await readChunkedUploadMetadata(id);
    if (metadata.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    const formData = await request.formData();
    const chunk = formData.get("chunk") as File | null;
    const chunkIndex = Number(formData.get("chunkIndex"));

    if (!chunk || !Number.isInteger(chunkIndex)) {
      return NextResponse.json({ error: "Chunk and chunkIndex are required" }, { status: 400 });
    }

    if (chunkIndex < 0 || chunkIndex >= metadata.totalChunks) {
      return NextResponse.json({ error: "Invalid chunk index" }, { status: 400 });
    }

    if (chunk.size > CHUNK_SIZE_BYTES) {
      return NextResponse.json({ error: "Chunk is too large" }, { status: 400 });
    }

    await writeChunk(id, chunkIndex, chunk);

    return NextResponse.json({ ok: true, chunkIndex });
  } catch (error) {
    console.error("Chunk upload failed:", error);
    return NextResponse.json(
      { error: "Failed to upload chunk" },
      { status: 500 }
    );
  }
}


export const POST = withDeploymentLeaseRoute(handlePOST);
