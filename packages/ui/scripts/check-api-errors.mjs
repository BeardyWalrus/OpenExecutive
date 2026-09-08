/**
 * Contract test for the error path in `src/lib/api.ts`.
 *
 * Every call in that module funnels non-OK responses through `apiError()`.
 * Before it existed each call threw a fixed sentence ("Failed to start
 * onboarding"), so a backend that wasn't running, a 401 from the proxy and a
 * genuine 500 were indistinguishable in the UI — which cost real debugging
 * time more than once. These cases pin the message format and the fallbacks.
 *
 * The UI has no test runner, so this stays dependency-free: transpile the one
 * module with the TypeScript already in devDependencies, stub `fetch`, and
 * assert on what the exported functions throw. Run with `npm run test:api`.
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, renameSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const uiRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const outDir = mkdtempSync(join(tmpdir(), "api-errors-"));

try {
  execFileSync(
    "npx",
    [
      "tsc", "--ignoreConfig", join(uiRoot, "src/lib/api.ts"),
      "--target", "es2022", "--module", "esnext",
      "--moduleResolution", "bundler", "--skipLibCheck",
      "--outDir", outDir,
    ],
    { cwd: uiRoot, stdio: "inherit" },
  );
  // Node needs the .mjs extension to load it as an ES module from a temp dir
  // that has no package.json of its own.
  renameSync(join(outDir, "api.js"), join(outDir, "api.mjs"));

  const api = await import(pathToFileURL(join(outDir, "api.mjs")).href);
  const { ApiError, MAX_DETAIL_CHARS, startOnboarding, listDepartments, getCompanyProfile } = api;

  const json = (body, status, statusText) =>
    new Response(JSON.stringify(body), { status, statusText });

  const cases = [
    {
      name: "proxy 502 names the unreachable backend",
      res: () => json(
        { detail: "Cannot reach the API at http://127.0.0.1:8000 (fetch failed: connect ECONNREFUSED 127.0.0.1:8000). Start the backend, or set BACKEND_BASE_URL if it listens on another port." },
        502, "Bad Gateway"),
      call: startOnboarding,
      expect: (e) =>
        e.status === 502 &&
        e.message.startsWith("Failed to start onboarding (HTTP 502): Cannot reach the API"),
    },
    {
      name: "a FastAPI string detail is surfaced",
      res: () => json({ detail: "Unknown department" }, 404, "Not Found"),
      call: listDepartments,
      expect: (e) => e.message === "Failed to load departments (HTTP 404): Unknown department",
    },
    {
      name: "a FastAPI validation detail (a list) is joined, not stringified as [object Object]",
      res: () => json({ detail: [{ loc: ["body", "x"], msg: "field required" }] }, 422, "Unprocessable Entity"),
      call: startOnboarding,
      expect: (e) => e.status === 422 && e.message.includes("field required"),
    },
    {
      name: "an HTML error page falls back to statusText rather than dumping tag soup",
      res: () => new Response("<!DOCTYPE html><html><body>Internal Server Error</body></html>",
        { status: 500, statusText: "Internal Server Error" }),
      call: startOnboarding,
      expect: (e) => e.message === "Failed to start onboarding (HTTP 500): Internal Server Error",
    },
    {
      name: "an empty body falls back to statusText",
      res: () => new Response(null, { status: 401, statusText: "Unauthorized" }),
      call: startOnboarding,
      expect: (e) => e.message === "Failed to start onboarding (HTTP 401): Unauthorized",
    },
    {
      name: "a plain-text body is used verbatim",
      res: () => new Response("upstream connect error", { status: 503, statusText: "Service Unavailable" }),
      call: startOnboarding,
      expect: (e) => e.message === "Failed to start onboarding (HTTP 503): upstream connect error",
    },
    {
      name: "an oversized body is truncated",
      res: () => new Response("x".repeat(5000), { status: 500, statusText: "Internal Server Error" }),
      call: startOnboarding,
      expect: (e) => e.detail.length === MAX_DETAIL_CHARS,
    },
    {
      name: "a missing company profile is branchable on status, not on message text",
      res: () => json({ detail: "no profile" }, 404, "Not Found"),
      call: getCompanyProfile,
      expect: (e) => e instanceof ApiError && e.status === 404,
    },
    {
      name: "an OK response still parses normally",
      res: () => json({ session_id: "s1", current_step: 1 }, 200, "OK"),
      call: startOnboarding,
      ok: (v) => v.session_id === "s1",
    },
  ];

  let failed = 0;
  for (const c of cases) {
    globalThis.fetch = async () => c.res();
    let threw, value;
    try {
      value = await c.call();
    } catch (e) {
      threw = e;
    }
    let pass, got;
    if (c.ok) {
      pass = !threw && c.ok(value);
      got = threw ? `threw ${threw.message}` : JSON.stringify(value);
    } else {
      pass = threw instanceof ApiError && c.expect(threw);
      got = threw ? `${threw.name}: ${threw.message}` : `returned ${JSON.stringify(value)}`;
    }
    console.log(`${pass ? "PASS" : "FAIL"}  ${c.name}`);
    if (!pass) {
      console.log(`      got: ${got}`);
      failed++;
    }
  }

  console.log(failed === 0 ? `\nALL PASS (${cases.length} cases)` : `\n${failed} FAILED`);
  process.exitCode = failed === 0 ? 0 : 1;
} finally {
  rmSync(outDir, { recursive: true, force: true });
}
