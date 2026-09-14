import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { stat } from "fs/promises";
import { join } from "path";
import { nanoid } from "nanoid";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../../../../auth";
import { log } from "@/lib/logger";
import {
  combineChunks,
  getChunkPath,
  isValidUploadId,
  readChunkedUploadMetadata,
  removeChunkedUpload,
} from "@/lib/server/chunked-upload";
import {
  createMeetingFromStoredFile,
  getUploadDir,
  UploadValidationError,
} from "@/lib/server/meetings";

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

    let uploadedSize = 0;
    for (let index = 0; index < metadata.totalChunks; index += 1) {
      const chunkStat = await stat(getChunkPath(id, index));
      uploadedSize += chunkStat.size;
    }

    if (uploadedSize !== metadata.fileSize) {
      return NextResponse.json(
        { error: "Uploaded chunks do not match expected file size" },
        { status: 400 }
      );
    }

    const extension = metadata.fileName.split(".").pop() || "m4a";
    const filename = `${nanoid()}.${extension}`;
    const filepath = join(/* turbopackIgnore: true */ getUploadDir(), filename);

    await combineChunks(id, filepath, metadata.totalChunks);

    const meeting = await createMeetingFromStoredFile({
      userId: session.user.id,
      originalFileName: metadata.fileName,
      storagePath: filepath,
      fileSize: metadata.fileSize,
      mimeType: metadata.mimeType,
      title: metadata.title,
      description: metadata.description,
      aiInstructions: metadata.aiInstructions,
      projectId: metadata.projectId,
      tagIds: metadata.tagIds,
    });

    await removeChunkedUpload(id);

    log.fileUpload(metadata.fileName, metadata.fileSize, session.user.id);

    return NextResponse.json({
      id: meeting.id,
      message: "File uploaded successfully",
    });
  } catch (error) {
    if (error instanceof UploadValidationError) {
      return NextResponse.json(
        { error: error.message },
        { status: 400 }
      );
    }

    console.error("Chunked upload completion failed:", error);
    return NextResponse.json(
      { error: "Failed to complete upload" },
      { status: 500 }
    );
  }
}

export const POST = withDeploymentLeaseRoute(handlePOST);
