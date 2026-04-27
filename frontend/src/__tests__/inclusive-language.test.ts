import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * Validates: Requirements 5.8 (Property 8 — inclusive-language invariant across
 * Documentation_Set).
 *
 * Scans every required top-level documentation file under `acgr-replication-starter-pack/` for
 * case-insensitive matches of blocked, non-inclusive terms. CHANGELOG.md is optional
 * (it is an optional task in the release-readiness spec); every other file in the
 * Documentation_Set must exist.
 *
 * Code-fenced blocks that are themselves a policy-quoted list of blocked terms are
 * excluded from the scan. A fence qualifies for exclusion when its opening line
 * contains one of the markers "blocked-language" or "inclusive-language".
 */

// Test file lives at acgr-replication-starter-pack/frontend/src/__tests__/, so the
// Documentation_Set root is three directories up.
const DOC_ROOT = path.resolve(__dirname, "..", "..", "..");

const BLOCKED_TERMS = [
  "master",
  "slave",
  "whitelist",
  "blacklist",
  "whiteday",
  "blackday",
] as const;

const REQUIRED_DOCS = [
  "README.md",
  "SECURITY.md",
  "CONTRIBUTING.md",
  "LICENSE",
] as const;

const OPTIONAL_DOCS = ["CHANGELOG.md"] as const;

/** Strip fenced code blocks whose opening line contains an exclusion marker. */
function stripBlockedTermFences(content: string): string {
  const lines = content.split(/\r?\n/);
  const out: string[] = [];
  let inExcludedFence = false;
  let inOtherFence = false;

  for (const line of lines) {
    const fenceMatch = /^\s*```(.*)$/.exec(line);
    if (fenceMatch) {
      if (!inExcludedFence && !inOtherFence) {
        // Opening a new fence. Check the info string for an exclusion marker.
        const info = fenceMatch[1].toLowerCase();
        if (info.includes("blocked-language") || info.includes("inclusive-language")) {
          inExcludedFence = true;
          continue;
        }
        inOtherFence = true;
        out.push(line);
        continue;
      }
      // Closing a fence.
      if (inExcludedFence) {
        inExcludedFence = false;
        continue;
      }
      inOtherFence = false;
      out.push(line);
      continue;
    }

    if (inExcludedFence) continue;
    out.push(line);
  }

  return out.join("\n");
}

interface Offense {
  file: string;
  line: number;
  term: string;
  snippet: string;
}

function scan(content: string, file: string): Offense[] {
  const stripped = stripBlockedTermFences(content);
  const offenses: Offense[] = [];
  const lines = stripped.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (const term of BLOCKED_TERMS) {
      const re = new RegExp(`\\b${term}\\b`, "i");
      if (re.test(line)) {
        offenses.push({ file, line: i + 1, term, snippet: line.trim() });
      }
    }
  }
  return offenses;
}

describe("inclusive-language invariant (Property 8)", () => {
  it("every required documentation file exists and is non-empty", () => {
    for (const rel of REQUIRED_DOCS) {
      const abs = path.resolve(DOC_ROOT, rel);
      expect(fs.existsSync(abs), `Missing required doc: ${rel}`).toBe(true);
      const content = fs.readFileSync(abs, "utf-8");
      expect(content.length, `Doc ${rel} is empty`).toBeGreaterThan(0);
    }
  });

  it("no Documentation_Set file contains any blocked non-inclusive term", () => {
    const allOffenses: Offense[] = [];

    for (const rel of REQUIRED_DOCS) {
      const abs = path.resolve(DOC_ROOT, rel);
      const content = fs.readFileSync(abs, "utf-8");
      allOffenses.push(...scan(content, rel));
    }

    for (const rel of OPTIONAL_DOCS) {
      const abs = path.resolve(DOC_ROOT, rel);
      if (!fs.existsSync(abs)) continue;
      const content = fs.readFileSync(abs, "utf-8");
      allOffenses.push(...scan(content, rel));
    }

    if (allOffenses.length > 0) {
      const report = allOffenses
        .map((o) => `${o.file}:${o.line} [${o.term}] ${o.snippet}`)
        .join("\n");
      throw new Error(`Blocked non-inclusive terms found:\n${report}`);
    }
  });
});
