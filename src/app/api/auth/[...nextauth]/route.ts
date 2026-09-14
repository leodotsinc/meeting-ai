import { withDeploymentLeaseRoute } from "@/lib/server/deployment-lease";
import { NextRequest } from "next/server";
import { handlers } from "../../../../../auth";
import {
  loginRateLimiter,
  RATE_LIMITS,
  getClientIP,
  rateLimitResponse,
} from "@/lib/rate-limit";

export const { GET } = handlers;

// Wrap POST to add rate limiting for login attempts
async function handlePOST(request: NextRequest) {
  const url = new URL(request.url);
  const isCredentialsCallback =
    url.pathname.includes("/callback/credentials");

  // Only rate limit credential login attempts
  if (isCredentialsCallback) {
    const ip = getClientIP(request);
    const result = loginRateLimiter.check(
      `login:${ip}`,
      RATE_LIMITS.login.limit,
      RATE_LIMITS.login.windowMs
    );

    if (!result.success) {
      console.warn(`Rate limit exceeded for IP: ${ip}`);
      return rateLimitResponse(result.resetIn);
    }
  }

  // Pass to NextAuth handler
  return handlers.POST(request);
}

export const POST = withDeploymentLeaseRoute(handlePOST);
