import { auth } from "@/auth";

// Single-user / trusted-network escape hatch. When DISABLE_AUTH=true there is
// no sign-in gate at all: anyone who can reach this port is served as the
// principal, because the backend falls back to the principal Person when no
// `x-caller-email` header arrives (see api/routes/chat.py
// _resolve_caller_person_id — the same path the CLI and direct curl use).
//
// Exists because Google OAuth cannot be registered against a raw LAN IP over
// http, so a self-hosted single-user install otherwise has to tunnel to
// localhost or terminate real HTTPS just to reach its own machine.
//
// Read once at module scope so the cost is not paid per request.
const AUTH_DISABLED = process.env.DISABLE_AUTH === "true";

if (AUTH_DISABLED) {
  // Module scope: logged once per server instance, not per request. A wide-open
  // app that looks normal is the failure mode worth being noisy about.
  console.warn(
    "[auth] DISABLE_AUTH=true — sign-in is OFF. Every visitor is served as the " +
      "principal. Acceptable for a single-user install on a trusted network; " +
      "never expose this port to the internet.",
  );
}

// Gate every page + non-auth API route. `auth` from NextAuth v5 wraps a
// handler that injects req.auth; here we use it directly as middleware, which
// makes unauthenticated requests redirect to the configured sign-in page.
export default auth((req) => {
  if (AUTH_DISABLED) return;
  if (req.auth) return;

  // For API routes, return JSON 401 instead of an HTML redirect so the
  // browser doesn't follow it and break fetch() callers.
  if (req.nextUrl.pathname.startsWith("/api/")) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  const signInUrl = new URL("/signin", req.nextUrl.origin);
  signInUrl.searchParams.set("callbackUrl", req.nextUrl.pathname + req.nextUrl.search);
  return Response.redirect(signInUrl);
});

// Exclude Auth.js's own routes, Next internals, static assets, and exactly
// `/signin` (with optional trailing slash). Using `signin/?` rather than the
// looser `signin` keeps unrelated paths like `/signin-help` gated.
export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico|signin/?$).*)"],
};
