import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../../auth";
import { prisma } from "@/lib/db/prisma";

// PATCH /api/tags/[id] - Update a tag
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
    const { name, color } = body;

    // Check ownership
    const tag = await prisma.tag.findUnique({
      where: { id },
      select: { userId: true },
    });

    if (!tag) {
      return NextResponse.json({ error: "Tag not found" }, { status: 404 });
    }

    if (tag.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    // If renaming, check for duplicate
    if (name && typeof name === "string") {
      const existing = await prisma.tag.findFirst({
        where: {
          userId: session.user.id,
          name: name.trim(),
          NOT: { id },
        },
      });

      if (existing) {
        return NextResponse.json(
          { error: "A tag with this name already exists" },
          { status: 409 }
        );
      }
    }

    const updated = await prisma.tag.update({
      where: { id },
      data: {
        ...(name && { name: name.trim() }),
        ...(color && { color }),
      },
    });

    return NextResponse.json(updated);
  } catch (error) {
    console.error("Update tag error:", error);
    return NextResponse.json(
      { error: "Failed to update tag" },
      { status: 500 }
    );
  }
}

// DELETE /api/tags/[id] - Delete a tag
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

    // Check ownership
    const tag = await prisma.tag.findUnique({
      where: { id },
      select: { userId: true },
    });

    if (!tag) {
      return NextResponse.json({ error: "Tag not found" }, { status: 404 });
    }

    if (tag.userId !== session.user.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 403 });
    }

    // Delete tag (MeetingTag entries will be cascade deleted)
    await prisma.tag.delete({
      where: { id },
    });

    return NextResponse.json({ message: "Tag deleted successfully" });
  } catch (error) {
    console.error("Delete tag error:", error);
    return NextResponse.json(
      { error: "Failed to delete tag" },
      { status: 500 }
    );
  }
}

export const PATCH = withDeploymentLeaseRoute(handlePATCH);
export const DELETE = withDeploymentLeaseRoute(handleDELETE);
