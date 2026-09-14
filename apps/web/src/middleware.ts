import { NextResponse, type NextRequest } from "next/server";

/**
 * A cheap first gate: the presence of the refresh cookie. The API is the real
 * authority — this only spares unauthenticated visitors a dashboard flash.
 */
export function middleware(request: NextRequest) {
  const hasSession = request.cookies.has("refresh_token");
  if (!hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/dashboard/:path*"] };
