import path from "node:path";
import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

// This file is a module, so `__dirname` is not guaranteed to exist. Next
// normally evaluates it in a CommonJS scope where it does, but the WASM SWC
// path (NEXT_TEST_WASM / experimental.useWasmBinary — needed on hosts where
// the native @next/swc binary is incompatible with the system glibc) loads it
// as a real ES module, where `__dirname` throws:
//
//   ReferenceError: __dirname is not defined in ES module scope
//   ⨯ Failed to load next.config.ts
//
// Deriving the directory from import.meta.url works in both scopes.
const packageDir = path.dirname(fileURLToPath(import.meta.url));

// Note: backend proxying is handled by `src/app/api/backend/[...path]/route.ts`
// so streaming SSE responses aren't buffered. Don't add a `rewrites()` rule
// here for `/api/backend/*` — it would re-introduce buffering.
// `next dev` blocks cross-origin requests to /_next/* dev resources unless the
// origin is listed here. Reaching a dev server on a LAN IP therefore loads the
// HTML but not the client bundle, so the page never hydrates and freezes on
// whatever the server rendered — a component showing "Loading…" stays there
// forever with only a console warning to explain it.
//
// Comma-separated hosts, e.g. ALLOWED_DEV_ORIGINS=192.168.86.36,openexec.lan
// Dev-only: `next start` serves no dev resources and ignores this.
const allowedDevOrigins = (process.env.ALLOWED_DEV_ORIGINS ?? "")
  .split(",")
  .map((o) => o.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  ...(allowedDevOrigins.length > 0 ? { allowedDevOrigins } : {}),
  // Emit a self-contained server bundle so the production Docker image
  // can run `node server.js` without copying node_modules.
  output: "standalone",
  // Pin the file-tracing root to this package so the standalone output
  // lands at `.next/standalone/server.js`. Without this, Next walks up
  // looking for a workspace root and nests server.js many directories deep.
  outputFileTracingRoot: packageDir,
  // mermaid v11 is ESM-only; Next.js webpack needs to transpile it
  transpilePackages: ["mermaid"],
};

export default nextConfig;
