import { NextResponse, type NextRequest } from "next/server";
import { API_URL } from "@/lib/api";
import { proxyTargetUrl } from "@/lib/auth-proxy";
import { fetchFrameOrigins, frameAncestorsHeader } from "@/lib/frame-policy";

/** What a public key can look like; anything else is not asked about. */
const PUBLIC_KEY = /^[A-Za-z0-9_-]{1,128}$/;

/**
 * `/embed/<key>`: the chat widget's page, framed by the business's own site.
 * Its `frame-ancestors` is the agent's allowed origins (spec §6). Never a
 * login redirect -- a visitor has no session here, and needs none.
 */
async function embed(request: NextRequest): Promise<NextResponse> {
  const key = request.nextUrl.pathname.split("/")[2] ?? "";
  const origins = PUBLIC_KEY.test(key)
    ? await fetchFrameOrigins(proxyTargetUrl(process.env.API_INTERNAL_URL, API_URL), key)
    : [];
  const response = NextResponse.next();
  response.headers.set("Content-Security-Policy", frameAncestorsHeader(origins));
  response.headers.delete("X-Frame-Options");
  return response;
}

/**
 * `/dashboard`: a cheap first gate, the presence of the refresh cookie. The
 * API is the real authority — this only spares unauthenticated visitors a
 * dashboard flash.
 */
export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname === "/embed" || pathname.startsWith("/embed/")) return embed(request);

  const hasSession = request.cookies.has("refresh_token");
  if (!hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/dashboard/:path*", "/embed/:path*"] };
