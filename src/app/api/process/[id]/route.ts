import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../../auth";
import { prisma } from "@/lib/db/prisma";
import { processMeeting } from "@/lib/services/processing";
import { ProcessingStatus } from "@prisma/client";
import { apiRateLimiter, RATE_LIMITS } from "@/lib/rate-limit";

// POST /api/process/[id] - Start processing a meeting
async function handlePOST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    // Rate limit processing requests per user
    const rateLimitResult = apiRateLimiter.check(
      `process:${session.user.id}`,
      RATE_LIMITS.process.limit,
      RATE_LIMITS.process.windowMs
    );

    if (!rateLimitResult.success) {
      return NextResponse.json(
        { error: `Too many processing requests. Try again in ${rateLimitResult.resetIn} seconds.` },
        { status: 429, headers: { "Retry-After": String(rateLimitResult.resetIn) } }
      );
    }

    const { id } = await params;

    // Get meeting
    const meeting = await prisma.meeting.findUnique({
      where: { id },
    });

    if (!meeting) {
      return NextResponse.json({ error: "Meeting not found" }, { status: 404 });
    }

    if (meeting.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    // Check if already processing or completed
    if (meeting.status === ProcessingStatus.COMPLETED) {
      return NextResponse.json(
        { error: "Meeting already processed" },
        { status: 400 }
      );
    }

    if (
      meeting.status === ProcessingStatus.TRANSCRIBING ||
      meeting.status === ProcessingStatus.ANALYZING
    ) {
      return NextResponse.json(
        { error: "Meeting is already being processed" },
        { status: 400 }
      );
    }

    // Start processing in background
    processMeeting(id).catch((error) => {
      console.error(`Background processing failed for ${id}:`, error);
    });

    return NextResponse.json({
      message: "Processing started",
      status: ProcessingStatus.TRANSCRIBING,
    });
  } catch (error) {
    console.error("Process error:", error);
    return NextResponse.json(
      { error: "Failed to start processing" },
      { status: 500 }
    );
  }
}

// GET /api/process/[id] - Get processing status
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { id } = await params;

    const meeting = await prisma.meeting.findUnique({
      where: { id },
      select: {
        id: true,
        status: true,
        processingError: true,
        processedAt: true,
        userId: true,
      },
    });

    if (!meeting) {
      return NextResponse.json({ error: "Meeting not found" }, { status: 404 });
    }

    if (meeting.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    return NextResponse.json({
      status: meeting.status,
      error: meeting.processingError,
      processedAt: meeting.processedAt,
    });
  } catch (error) {
    console.error("Status error:", error);
    return NextResponse.json(
      { error: "Failed to get status" },
      { status: 500 }
    );
  }
}

export const POST = withDeploymentLeaseRoute(handlePOST);
