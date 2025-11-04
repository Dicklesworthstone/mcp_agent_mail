const STATUS_ENDPOINT = "/__preview__/status";
const POLL_INTERVAL_MS = 2000;
let lastSignature = null;
let failureCount = 0;
const MAX_FAILURES = 5;

async function pollStatus() {
  try {
    const response = await fetch(STATUS_ENDPOINT, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`status ${response.status}`);
    }
    const payload = await response.json();
    const signature = payload.signature;
    if (typeof signature === "string") {
      if (lastSignature && lastSignature !== signature) {
        console.info("Preview change detected – refreshing page.");
        window.location.reload();
        return;
      }
      lastSignature = signature;
    }
    failureCount = 0;
  } catch (error) {
    failureCount += 1;
    if (failureCount >= MAX_FAILURES) {
      console.debug("Preview status polling disabled:", error);
      clearInterval(intervalId);
    }
  }
}

const intervalId = window.setInterval(pollStatus, POLL_INTERVAL_MS);
pollStatus().catch(() => {
  /* ignore initial error */
});
