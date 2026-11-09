import assert from "node:assert/strict";
import { test } from "node:test";
import { createAccessMonitor, getPortalPath, sessionAccessKey } from "./authSession.js";

const restricted = { id: 7, email: "user@example.com", roles: ["restricted"], accessRevision: "restricted-access" };
const admin = { ...restricted, roles: ["admin"], accessRevision: "admin-access" };
const individual = { ...restricted, roles: ["individual_report_access"] };

test("login and existing individual-report links send admins to the main portal", () => {
  assert.equal(getPortalPath(admin), "/");
  assert.equal(getPortalPath(admin, "/individual-report"), "/");
  assert.equal(getPortalPath({ ...admin, roles: ["admin", "individual_report_access"] }), "/");
  assert.equal(getPortalPath(individual), "/individual-report");
  assert.equal(getPortalPath(restricted), "/");
  // Preserve explicit individual-report links for other authenticated users.
  assert.equal(getPortalPath(restricted, "/individual-report"), "/individual-report");
  assert.equal(getPortalPath(admin, "/reset-password"), "/reset-password");
});

for (const [label, before, after] of [
  ["promotion", restricted, admin],
  ["individual-report promotion", individual, admin],
  ["demotion", admin, restricted],
  ["access rule change without a role rename", restricted, { ...restricted, accessRevision: "updated-access" }],
  ["identity change", restricted, { ...restricted, email: "changed@example.com" }],
]) {
  test(`${label} discards the old session once`, async () => {
    const changes = [];
    const monitor = createAccessMonitor({
      user: before,
      loadSession: async () => ({ authenticated: true, user: after }),
      onAccessChange: (user) => changes.push(user),
    });
    await monitor.check();
    await monitor.check();
    assert.deepEqual(changes, [after]);
  });
}

test("unchanged permissions preserve embeds and unsaved work", async () => {
  const before = { ...restricted, roles: ["restricted", "other"] };
  const after = { ...before, roles: ["other", "restricted"], fullName: "New display name" };
  assert.equal(sessionAccessKey(before), sessionAccessKey(after));
  const changes = [];
  const monitor = createAccessMonitor({ user: before,
    loadSession: async () => ({ authenticated: true, user: after }),
    onAccessChange: (user) => changes.push(user) });
  await monitor.check();
  await monitor.check();
  assert.deepEqual(changes, []);
});

test("logout, account deletion or expiration clears the current session", async () => {
  const changes = [];
  const monitor = createAccessMonitor({ user: admin,
    loadSession: async () => ({ authenticated: false, user: null }),
    onAccessChange: (user) => changes.push(user) });
  await monitor.check();
  assert.deepEqual(changes, [null]);
});

test("transient errors and malformed responses leave access intact and allow retry", async () => {
  const responses = [new Error("Offline"), {}, { authenticated: true, user: null },
    { authenticated: true, user: admin }];
  const changes = [];
  const monitor = createAccessMonitor({ user: restricted,
    loadSession: async () => {
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response;
    }, onAccessChange: (user) => changes.push(user) });
  for (let i = 0; i < 3; i++) {
    await monitor.check();
    assert.deepEqual(changes, []);
  }
  await monitor.check();
  assert.deepEqual(changes, [admin]);
});

test("overlapping focus and timer checks share a request and ignore disposed responses", async () => {
  let finish;
  let requests = 0;
  const changes = [];
  const monitor = createAccessMonitor({ user: restricted,
    loadSession: () => {
      requests++;
      return new Promise((resolve) => { finish = resolve; });
    }, onAccessChange: (user) => changes.push(user) });
  const pending = monitor.check();
  await monitor.check();
  assert.equal(requests, 1);
  monitor.dispose();
  finish({ authenticated: true, user: admin });
  await pending;
  await monitor.check();
  assert.deepEqual(changes, []);
  assert.equal(requests, 1);
});
