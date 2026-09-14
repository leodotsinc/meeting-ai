import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db/prisma";
import { log } from "@/lib/logger";
import { processMeeting } from "@/lib/services/processing";
import { getBearerToken, isValidInternalToken } from "@/lib/server/internal-auth";
import {
  createMeetingFromUpload,
  UploadValidationError,
  validateAudioUpload,
} from "@/lib/server/meetings";

async function handlePOST(request: NextRequest) {
  try {
    const token = getBearerToken(request.headers.get("authorization"));
    if (!isValidInternalToken(token)) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const userEmail = process.env.JARVIS_UPLOAD_USER_EMAIL || process.env.AUTH_EMAIL;
    if (!userEmail) {
      return NextResponse.json(
        { error: "JARVIS_UPLOAD_USER_EMAIL or AUTH_EMAIL must be configured" },
        { status: 500 }
      );
    }

    const user = await prisma.user.findUnique({ where: { email: userEmail } });
    if (!user) {
      return NextResponse.json(
        { error: `Upload user not found: ${userEmail}` },
        { status: 404 }
      );
    }

    const formData = await request.formData();
    const file = formData.get("file") as File | null;
    const title = formData.get("title") as string | null;
    const description = formData.get("description") as string | null;
    const aiInstructions = formData.get("aiInstructions") as string | null;
    const projectId = formData.get("projectId") as string | null;
    const tagIdsRaw = formData.get("tagIds") as string | null;
    const autoProcess = formData.get("autoProcess") !== "false";
    const tagIds = tagIdsRaw ? (JSON.parse(tagIdsRaw) as string[]) : [];

    if (!file) {
      return NextResponse.json({ error: "No file provided" }, { status: 400 });
    }

    const validationError = validateAudioUpload(file);
    if (validationError) {
      return NextResponse.json({ error: validationError }, { status: 400 });
    }

    if (!title || title.trim().length === 0) {
      return NextResponse.json({ error: "Title is required" }, { status: 400 });
    }

    const meeting = await createMeetingFromUpload({
      userId: user.id,
      file,
      title,
      description,
      aiInstructions,
      projectId,
      tagIds,
    });

    log.fileUpload(file.name, file.size, user.id);

    if (autoProcess) {
      processMeeting(meeting.id).catch((error) => {
        console.error(`Internal upload processing failed for ${meeting.id}:`, error);
      });
    }

    return NextResponse.json({
      id: meeting.id,
      status: meeting.status,
      url: `/meetings/${meeting.id}`,
      processingStarted: autoProcess,
    });
  } catch (error) {
    if (error instanceof UploadValidationError) {
      return NextResponse.json(
        { error: error.message },
        { status: 400 }
      );
    }

    log.error("Internal Jarvis upload failed", error);
    return NextResponse.json(
      { error: "Failed to upload file" },
      { status: 500 }
    );
  }
}

export const POST = withDeploymentLeaseRoute(handlePOST);
