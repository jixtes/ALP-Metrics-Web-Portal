const RENEW_BEFORE_MS = 5 * 60 * 1000;
const RETRY_DELAY_MS = 30 * 1000;

// Keep token changes local to this embed so renewing one report does not remount others.
export function createPowerBIReportSession({ report, embed, renew, reset, onError }) {
  let current = report;
  let embedded;
  let disposed = false;
  let blocked = false;
  let pending = false;
  let retryAfter = 0;

  async function check() {
    if (disposed || blocked || pending || Date.now() < retryAfter) return;
    pending = true;
    try {
      const expiration = Date.parse(current.tokenExpiration);
      if (Number.isFinite(expiration) && expiration - Date.now() <= RENEW_BEFORE_MS) {
        // Also throttle short-lived tokens and failed requests to avoid a renewal loop.
        retryAfter = Date.now() + RETRY_DELAY_MS;
        const fresh = await renew(current.reportId);
        if (disposed) return;
        const freshExpiration = Date.parse(fresh?.tokenExpiration);
        if (!fresh?.accessToken || fresh.error || fresh.reportId !== current.reportId ||
            !Number.isFinite(freshExpiration) || freshExpiration <= Date.now()) {
          throw new Error(fresh?.error || "Power BI did not return a valid report token.");
        }

        if (!embedded || expiration <= Date.now() || fresh.embedUrl !== current.embedUrl) {
          embedded = embed(fresh);
        } else {
          try {
            await embedded.setAccessToken(fresh.accessToken);
          } catch {
            if (disposed) return;
            embedded = embed(fresh);
          }
        }
        if (disposed) return;
        current = fresh;
      } else if (!embedded) {
        embedded = embed(current);
      }
      onError("");
    } catch (error) {
      if (disposed) return;
      retryAfter = Date.now() + RETRY_DELAY_MS;
      if (error.status === 401 || error.status === 403) {
        blocked = true;
        reset();
        embedded = null;
        onError(error.message);
      } else {
        onError(`Unable to renew or load this Power BI report. Retrying automatically. ${error.message}`);
      }
    } finally {
      pending = false;
    }
  }

  return {
    check,
    dispose() {
      disposed = true;
      reset();
      embedded = null;
    },
  };
}
