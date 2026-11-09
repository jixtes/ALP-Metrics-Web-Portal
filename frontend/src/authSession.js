const INDIVIDUAL_REPORT_ROLE = "individual_report_access";

export function sessionAccessKey(user) {
  if (!user) return null;
  return JSON.stringify([user.id, user.email, [...(user.roles ?? [])].sort(), user.accessRevision]);
}

export function getPortalPath(user, currentPath = "/") {
  const roles = user?.roles ?? [];
  if (roles.includes("admin")) {
    return currentPath === "/individual-report" ? "/" : currentPath;
  }
  if (roles.includes(INDIVIDUAL_REPORT_ROLE) && currentPath === "/") {
    return "/individual-report";
  }
  return currentPath;
}

// Reload the page when access changes so cached data, requests and embedded
// report tokens all start again under the current server-side permissions.
export function createAccessMonitor({ user, loadSession, onAccessChange }) {
  const initialKey = sessionAccessKey(user);
  let disposed = false;
  let pending = false;
  let changed = false;

  return {
    async check() {
      if (disposed || pending || changed) return;
      pending = true;
      try {
        const session = await loadSession();
        if (disposed || typeof session?.authenticated !== "boolean" ||
            (session.authenticated && !session.user)) return;
        const nextUser = session.authenticated ? session.user : null;
        if (sessionAccessKey(nextUser) !== initialKey) {
          changed = true;
          onAccessChange(nextUser);
        }
      } catch {
        // A transient network error must not interrupt the user's session.
      } finally {
        pending = false;
      }
    },
    dispose() {
      disposed = true;
    },
  };
}
