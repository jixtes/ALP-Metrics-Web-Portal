import assert from "node:assert/strict";
import { test } from "node:test";
import { createPowerBIReportSession } from "./powerbiSession.js";

function setup(t, minutes = 60) {
  let now = Date.parse("2026-09-22T12:00:00Z");
  t.mock.method(Date, "now", () => now);
  const config = (remaining, token = "original") => ({
    reportId: "report-1", embedUrl: "https://example.test/report",
    accessToken: token, tokenExpiration: new Date(now + remaining * 60000).toISOString(),
  });
  const update = t.mock.fn(async () => {});
  const embed = t.mock.fn(() => ({ setAccessToken: update }));
  const renew = t.mock.fn(async () => config(60, "renewed"));
  const reset = t.mock.fn();
  const onError = t.mock.fn();
  const session = createPowerBIReportSession({ report: config(minutes), embed, renew, reset, onError });
  return { session, embed, renew, reset, onError, update, config, advance(minutes) { now += minutes * 60000; } };
}

test("valid tokens keep the same embed across repeated checks", async (t) => {
  const s = setup(t);
  await s.session.check();
  await s.session.check();
  assert.equal(s.embed.mock.callCount(), 1);
  assert.equal(s.renew.mock.callCount(), 0);
});

test("renew before expiry without re-embedding; track the new expiry", async (t) => {
  const s = setup(t);
  await s.session.check();
  s.advance(56);
  await s.session.check();
  assert.deepEqual(s.update.mock.calls[0].arguments, ["renewed"]);
  assert.equal(s.embed.mock.callCount(), 1);
  s.advance(5);
  await s.session.check();
  assert.equal(s.renew.mock.callCount(), 1);
});

test("reload with a fresh token after sleep or a long absence", async (t) => {
  const s = setup(t);
  await s.session.check();
  s.advance(61);
  await s.session.check();
  assert.equal(s.embed.mock.callCount(), 2);
  assert.equal(s.embed.mock.calls[1].arguments[0].accessToken, "renewed");
  assert.equal(s.update.mock.callCount(), 0);
});

test("first visit after expiry never embeds the expired token", async (t) => {
  const s = setup(t, -1);
  await s.session.check();
  assert.equal(s.embed.mock.callCount(), 1);
  assert.equal(s.embed.mock.calls[0].arguments[0].accessToken, "renewed");
});

test("reload if the SDK cannot update the token in place", async (t) => {
  const s = setup(t);
  await s.session.check();
  s.update.mock.mockImplementation(async () => { throw new Error("SDK failure"); });
  s.advance(56);
  await s.session.check();
  assert.equal(s.embed.mock.callCount(), 2);
  assert.equal(s.embed.mock.calls[1].arguments[0].accessToken, "renewed");
});

test("failed renewal backs off and then recovers", async (t) => {
  const s = setup(t, -1);
  s.renew.mock.mockImplementationOnce(async () => { throw new Error("Offline"); });
  await s.session.check();
  assert.match(s.onError.mock.calls.at(-1).arguments[0], /Retrying automatically/);
  await s.session.check();
  assert.equal(s.renew.mock.callCount(), 1);
  s.advance(0.5);
  await s.session.check();
  assert.equal(s.renew.mock.callCount(), 2);
  assert.equal(s.embed.mock.callCount(), 1);
  assert.equal(s.onError.mock.calls.at(-1).arguments[0], "");
});

test("overlapping checks share the pending renewal and disposal ignores its response", async (t) => {
  const s = setup(t, -1);
  let finish;
  s.renew.mock.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const pending = s.session.check();
  s.advance(1);
  await s.session.check();
  assert.equal(s.renew.mock.callCount(), 1);
  s.session.dispose();
  finish(s.config(60, "renewed"));
  await pending;
  await s.session.check();
  assert.equal(s.embed.mock.callCount(), 0);
  assert.equal(s.onError.mock.callCount(), 0);
  assert.equal(s.reset.mock.callCount(), 1);
});

for (const status of [401, 403]) {
  test(`authorization failure ${status} clears the report and stops retries`, async (t) => {
    const s = setup(t);
    await s.session.check();
    s.advance(56);
    s.renew.mock.mockImplementation(async () => { throw Object.assign(new Error("Access denied"), { status }); });
    await s.session.check();
    s.advance(60);
    await s.session.check();
    assert.equal(s.reset.mock.callCount(), 1);
    assert.equal(s.renew.mock.callCount(), 1);
    assert.equal(s.onError.mock.calls.at(-1).arguments[0], "Access denied");
  });
}

test("reject expired, missing, or wrong-report tokens without embedding", async (t) => {
  const s = setup(t, -1);
  for (const response of [s.config(-1), { ...s.config(60), accessToken: null },
    { ...s.config(60), reportId: "other-report" }, { ...s.config(60), tokenExpiration: null }]) {
    s.renew.mock.mockImplementation(async () => response);
    await s.session.check();
    s.advance(1);
  }
  assert.equal(s.renew.mock.callCount(), 4);
  assert.equal(s.embed.mock.callCount(), 0);
});
