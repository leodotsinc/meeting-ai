import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { unlink } from "fs/promises";
import { auth } from "../../../../../auth";
import { prisma } from "@/lib/db/prisma";

// GET /api/meetings/[id] - Get meeting details
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
      include: {
        transcript: {
          include: {
            utterances: {
              include: {
                speaker: true,
              },
              orderBy: { orderIndex: "asc" },
            },
          },
        },
        analysis: true,
        speakers: {
          orderBy: { speakerIndex: "asc" },
        },
        project: {
          select: {
            id: true,
            name: true,
            color: true,
            icon: true,
          },
        },
        tags: {
          select: {
            tag: {
              select: {
                id: true,
                name: true,
                color: true,
              },
            },
          },
        },
      },
    });

    if (!meeting) {
      return NextResponse.json({ error: "Meeting not found" }, { status: 404 });
    }

    if (meeting.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    return NextResponse.json(meeting);
  } catch (error) {
    console.error("Get meeting error:", error);
    return NextResponse.json(
      { error: "Failed to fetch meeting" },
      { status: 500 }
    );
  }
}

// PATCH /api/meetings/[id] - Update meeting
async function handlePATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { id } = await params;
    const body = await request.json();

    // Validate ownership
    const meeting = await prisma.meeting.findUnique({
      where: { id },
      select: { userId: true },
    });

    if (!meeting) {
      return NextResponse.json({ error: "Meeting not found" }, { status: 404 });
    }

    if (meeting.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    // Build update data
    const updateData: Record<string, unknown> = {};

    if (body.title !== undefined) {
      updateData.title = body.title;
    }
    if (body.description !== undefined) {
      updateData.description = body.description;
    }
    if (body.projectId !== undefined) {
      updateData.projectId = body.projectId; // Can be null to remove project
    }
    if (body.favorite !== undefined) {
      updateData.favorite = body.favorite;
    }

    // Update meeting
    const updated = await prisma.meeting.update({
      where: { id },
      data: updateData,
      include: {
        project: {
          select: {
            id: true,
            name: true,
            color: true,
            icon: true,
          },
        },
        tags: {
          select: {
            tag: {
              select: {
                id: true,
                name: true,
                color: true,
              },
            },
          },
        },
      },
    });

    // Update tags if provided
    if (body.tagIds !== undefined && Array.isArray(body.tagIds)) {
      // Remove existing tags
      await prisma.meetingTag.deleteMany({
        where: { meetingId: id },
      });

      // Add new tags
      if (body.tagIds.length > 0) {
        await prisma.meetingTag.createMany({
          data: body.tagIds.map((tagId: string) => ({
            meetingId: id,
            tagId,
          })),
        });
      }

      // Fetch updated meeting with new tags
      const finalMeeting = await prisma.meeting.findUnique({
        where: { id },
        include: {
          project: {
            select: {
              id: true,
              name: true,
              color: true,
              icon: true,
            },
          },
          tags: {
            select: {
              tag: {
                select: {
                  id: true,
                  name: true,
                  color: true,
                },
              },
            },
          },
        },
      });

      // Update speaker labels if provided
      if (body.speakers && Array.isArray(body.speakers)) {
        for (const speaker of body.speakers) {
          if (speaker.id && speaker.label !== undefined) {
            await prisma.speaker.update({
              where: { id: speaker.id },
              data: { label: speaker.label },
            });
          }
        }
      }

      return NextResponse.json(finalMeeting);
    }

    // Update speaker labels if provided
    if (body.speakers && Array.isArray(body.speakers)) {
      for (const speaker of body.speakers) {
        if (speaker.id && speaker.label !== undefined) {
          await prisma.speaker.update({
            where: { id: speaker.id },
            data: { label: speaker.label },
          });
        }
      }
    }

    return NextResponse.json(updated);
  } catch (error) {
    console.error("Update meeting error:", error);
    return NextResponse.json(
      { error: "Failed to update meeting" },
      { status: 500 }
    );
  }
}

// DELETE /api/meetings/[id] - Delete meeting
async function handleDELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { id } = await params;

    // Get meeting to verify ownership and get file path
    const meeting = await prisma.meeting.findUnique({
      where: { id },
      select: { userId: true, storagePath: true },
    });

    if (!meeting) {
      return NextResponse.json({ error: "Meeting not found" }, { status: 404 });
    }

    if (meeting.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    // Delete meeting (cascades to transcript, utterances, speakers, analysis, logs)
    await prisma.meeting.delete({
      where: { id },
    });

    // Delete audio file
    try {
      await unlink(meeting.storagePath);
    } catch (fileError) {
      console.warn(`Failed to delete audio file: ${meeting.storagePath}`);
    }

    return NextResponse.json({ message: "Meeting deleted successfully" });
  } catch (error) {
    console.error("Delete meeting error:", error);
    return NextResponse.json(
      { error: "Failed to delete meeting" },
      { status: 500 }
    );
  }
}

export const PATCH = withDeploymentLeaseRoute(handlePATCH);
export const DELETE = withDeploymentLeaseRoute(handleDELETE);
