import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../auth";
import { apiRateLimiter, RATE_LIMITS } from "@/lib/rate-limit";
import { log } from "@/lib/logger";
import {
  createMeetingFromUpload,
  UploadValidationError,
  validateAudioUpload,
} from "@/lib/server/meetings";

async function handlePOST(request: NextRequest) {
  try {
    // Check authentication
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    // Rate limit uploads per user
    const rateLimitResult = apiRateLimiter.check(
      `upload:${session.user.id}`,
      RATE_LIMITS.upload.limit,
      RATE_LIMITS.upload.windowMs
    );

    if (!rateLimitResult.success) {
      log.rateLimitExceeded(session.user.id, "upload");
      return NextResponse.json(
        { error: `Upload limit reached. Try again in ${rateLimitResult.resetIn} seconds.` },
        { status: 429, headers: { "Retry-After": String(rateLimitResult.resetIn) } }
      );
    }

    // Parse form data
    const formData = await request.formData();
    const file = formData.get("file") as File | null;
    const title = formData.get("title") as string | null;
    const description = formData.get("description") as string | null;
    const aiInstructions = formData.get("aiInstructions") as string | null;
    const projectId = formData.get("projectId") as string | null;
    const tagIdsRaw = formData.get("tagIds") as string | null;
    const tagIds = tagIdsRaw ? JSON.parse(tagIdsRaw) as string[] : [];

    // Validate file
    if (!file) {
      return NextResponse.json({ error: "No file provided" }, { status: 400 });
    }

    const validationError = validateAudioUpload(file);
    if (validationError) {
      return NextResponse.json(
        { error: validationError },
        { status: 400 }
      );
    }

    // Validate title
    if (!title || title.trim().length === 0) {
      return NextResponse.json({ error: "Title is required" }, { status: 400 });
    }

    // Create meeting record in database (language will be detected during transcription)
    const meeting = await createMeetingFromUpload({
      userId: session.user.id,
      file,
      title,
      description,
      aiInstructions,
      projectId,
      tagIds,
    });

    log.fileUpload(file.name, file.size, session.user.id);

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

    log.error("Upload failed", error);
    return NextResponse.json(
      { error: "Failed to upload file" },
      { status: 500 }
    );
  }
}

export const POST = withDeploymentLeaseRoute(handlePOST);
