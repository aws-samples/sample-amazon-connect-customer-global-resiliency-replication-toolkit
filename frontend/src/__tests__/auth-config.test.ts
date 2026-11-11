import { describe, it, expect, vi } from "vitest";
import { loadAuthConfig, parseAuthConfig } from "../auth/config";

const GOOD = {
  region: "eu-west-2",
  userPoolId: "eu-west-2_AbCdEf123",
  clientId: "1abc2def3ghi4jkl5mno6pqr7s",
  domain: "my-prefix.auth.eu-west-2.amazoncognito.com",
};

const reply = (body: unknown, ok = true) =>
  vi.fn().mockResolvedValue({ ok, json: async () => body }) as unknown as typeof fetch;

describe("parseAuthConfig", () => {
  it("accepts a well-formed config", () => {
    expect(parseAuthConfig(GOOD)).toEqual(GOOD);
  });

  it.each([
    ["not an object", "x"],
    ["null", null],
    ["missing field", { ...GOOD, clientId: undefined }],
    ["bad region", { ...GOOD, region: "europe" }],
    ["bad user pool id", { ...GOOD, userPoolId: "not-a-pool" }],
    ["pool in another region", { ...GOOD, userPoolId: "us-east-1_AbCdEf123" }],
    ["domain with scheme", { ...GOOD, domain: "https://x.auth.eu-west-2.amazoncognito.com" }],
    ["domain with path", { ...GOOD, domain: "x.example.com/evil" }],
    ["domain with credentials", { ...GOOD, domain: "user@x.example.com" }],
  ])("rejects %s", (_name, value) => {
    expect(parseAuthConfig(value)).toBeNull();
  });
});

describe("loadAuthConfig", () => {
  it("returns the config when valid", async () => {
    expect(await loadAuthConfig(reply(GOOD), false)).toEqual({ status: "ok", config: GOOD });
  });

  it("fails closed in a production build when the file is missing", async () => {
    const r = await loadAuthConfig(reply({}, false), false);
    expect(r.status).toBe("error");
  });

  it("fails closed in a production build when the file is invalid", async () => {
    const r = await loadAuthConfig(reply({ region: "x" }), false);
    expect(r.status).toBe("error");
  });

  it("fails closed in a production build when the request throws", async () => {
    const r = await loadAuthConfig(vi.fn().mockRejectedValue(new Error("offline")) as unknown as typeof fetch, false);
    expect(r.status).toBe("error");
  });

  it("disables authentication only in development", async () => {
    expect((await loadAuthConfig(reply({}, false), true)).status).toBe("disabled");
    expect((await loadAuthConfig(reply({ region: "x" }), true)).status).toBe("disabled");
  });

  it("does not use the browser cache", async () => {
    const f = reply(GOOD);
    await loadAuthConfig(f, false);
    expect((f as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1]).toMatchObject({ cache: "no-store" });
  });
});
