import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "../../../../auth";
import {
  getAllApiKeys,
  setApiKey,
  deleteApiKey,
} from "@/lib/services/settings";
import { API_KEYS, type ApiKeyType } from "@/lib/config/models";

export async function GET() {
  const session = await auth();

  if (!session?.user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const apiKeys = await getAllApiKeys();
    return NextResponse.json({ apiKeys });
  } catch (error) {
    console.error("Failed to get API keys:", error);
    return NextResponse.json(
      { error: "Failed to get API keys" },
      { status: 500 }
    );
  }
}

async function handlePOST(request: NextRequest) {
  const session = await auth();

  if (!session?.user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const { key, value } = await request.json();

    if (!key || !value) {
      return NextResponse.json(
        { error: "Key and value are required" },
        { status: 400 }
      );
    }

    const validKeys = Object.values(API_KEYS);
    if (!validKeys.includes(key as ApiKeyType)) {
      return NextResponse.json({ error: "Invalid key type" }, { status: 400 });
    }

    await setApiKey(key as ApiKeyType, value);

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Failed to set API key:", error);
    return NextResponse.json(
      { error: "Failed to set API key" },
      { status: 500 }
    );
  }
}

async function handleDELETE(request: NextRequest) {
  const session = await auth();

  if (!session?.user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const { key } = await request.json();

    if (!key) {
      return NextResponse.json({ error: "Key is required" }, { status: 400 });
    }

    const validKeys = Object.values(API_KEYS);
    if (!validKeys.includes(key as ApiKeyType)) {
      return NextResponse.json({ error: "Invalid key type" }, { status: 400 });
    }

    await deleteApiKey(key as ApiKeyType);

    return NextResponse.json({ success: true });
  } catch (error) {
    console.error("Failed to delete API key:", error);
    return NextResponse.json(
      { error: "Failed to delete API key" },
      { status: 500 }
    );
  }
}

export const POST = withDeploymentLeaseRoute(handlePOST);
export const DELETE = withDeploymentLeaseRoute(handleDELETE);
