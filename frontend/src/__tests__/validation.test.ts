import { describe, it, expect } from "vitest";
import fc from "fast-check";
import { validateNamePrefix } from "../utils/validation";

/**
 * Validates: Requirements 2.1, 2.3 (Property 5 — name-prefix validator grammar)
 */
describe("validateNamePrefix", () => {
  it("accepts empty string, letters/digits/hyphens up to 32 chars, rejects everything else (property)", () => {
    fc.assert(
      fc.property(fc.string(), (s) => {
        const expected = s === "" || /^[a-zA-Z0-9-]{1,32}$/.test(s);
        expect(validateNamePrefix(s).valid).toBe(expected);
      }),
      { numRuns: 200 },
    );
  });

  const accepted = ["", "a", "A-1", "abc-123", "a".repeat(32)];
  for (const s of accepted) {
    it(`accepts: ${JSON.stringify(s)}`, () => {
      expect(validateNamePrefix(s).valid).toBe(true);
    });
  }

  const rejected = [" ", "a".repeat(33), "a!b", "a_b", "a b", "emoji🙂"];
  for (const s of rejected) {
    it(`rejects: ${JSON.stringify(s)}`, () => {
      expect(validateNamePrefix(s).valid).toBe(false);
    });
  }
});
