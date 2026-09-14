import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../../../auth";
import { apiRateLimiter, RATE_LIMITS } from "@/lib/rate-limit";
import { validateAudioFile } from "@/lib/server/meetings";
import {
  CHUNK_SIZE_BYTES,
  createUploadId,
  saveChunkedUploadMetadata,
} from "@/lib/server/chunked-upload";

async function handlePOST(request: NextRequest) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const rateLimitResult = apiRateLimiter.check(
      `upload:${session.user.id}`,
      RATE_LIMITS.upload.limit,
      RATE_LIMITS.upload.windowMs
    );

    if (!rateLimitResult.success) {
      return NextResponse.json(
        { error: `Upload limit reached. Try again in ${rateLimitResult.resetIn} seconds.` },
        { status: 429, headers: { "Retry-After": String(rateLimitResult.resetIn) } }
      );
    }

    const body = await request.json();
    const fileName = String(body.fileName || "");
    const mimeType = String(body.mimeType || "");
    const fileSize = Number(body.fileSize || 0);
    const title = String(body.title || "");
    const description = body.description ? String(body.description) : null;
    const aiInstructions = body.aiInstructions ? String(body.aiInstructions) : null;
    const projectId = body.projectId ? String(body.projectId) : null;
    const tagIds = Array.isArray(body.tagIds) ? body.tagIds.map(String) : [];

    const validationError = validateAudioFile(mimeType, fileSize);
    if (validationError) {
      return NextResponse.json({ error: validationError }, { status: 400 });
    }

    if (!fileName) {
      return NextResponse.json({ error: "File name is required" }, { status: 400 });
    }

    if (!title.trim()) {
      return NextResponse.json({ error: "Title is required" }, { status: 400 });
    }

    const uploadId = createUploadId();
    const totalChunks = Math.ceil(fileSize / CHUNK_SIZE_BYTES);

    await saveChunkedUploadMetadata({
      uploadId,
      userId: session.user.id,
      fileName,
      mimeType,
      fileSize,
      title,
      description,
      aiInstructions,
      projectId,
      tagIds,
      chunkSize: CHUNK_SIZE_BYTES,
      totalChunks,
      createdAt: new Date().toISOString(),
    });

    return NextResponse.json({
      uploadId,
      chunkSize: CHUNK_SIZE_BYTES,
      totalChunks,
    });
  } catch (error) {
    console.error("Chunked upload init failed:", error);
    return NextResponse.json(
      { error: "Failed to initialize upload" },
      { status: 500 }
    );
  }
}


export const POST = withDeploymentLeaseRoute(handlePOST);
