import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../auth";
import { prisma } from "@/lib/db/prisma";

// GET /api/tags - List all tags
export async function GET() {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const tags = await prisma.tag.findMany({
      where: { userId: session.user.id },
      include: {
        _count: {
          select: { meetings: true },
        },
      },
      orderBy: { name: "asc" },
    });

    return NextResponse.json(tags);
  } catch (error) {
    console.error("Get tags error:", error);
    return NextResponse.json(
      { error: "Failed to fetch tags" },
      { status: 500 }
    );
  }
}

// POST /api/tags - Create a new tag
async function handlePOST(request: NextRequest) {
  try {
    const session = await auth();
    if (!session?.user?.id) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const body = await request.json();
    const { name, color } = body;

    if (!name || typeof name !== "string" || name.trim().length === 0) {
      return NextResponse.json(
        { error: "Tag name is required" },
        { status: 400 }
      );
    }

    // Check if tag with same name already exists
    const existing = await prisma.tag.findUnique({
      where: {
        userId_name: {
          userId: session.user.id,
          name: name.trim(),
        },
      },
    });

    if (existing) {
      return NextResponse.json(
        { error: "A tag with this name already exists" },
        { status: 409 }
      );
    }

    const tag = await prisma.tag.create({
      data: {
        userId: session.user.id,
        name: name.trim(),
        color: color || "#71717a",
      },
    });

    return NextResponse.json(tag, { status: 201 });
  } catch (error) {
    console.error("Create tag error:", error);
    return NextResponse.json(
      { error: "Failed to create tag" },
      { status: 500 }
    );
  }
}

export const POST = withDeploymentLeaseRoute(handlePOST);
