// Service worker: opens the side panel on toolbar click and polls the local alerts feed once a minute.
// It is the only writer of the alerts cache; the panel asks it to poll via a runtime message.

import { ApiError } from "./api.js";
import { isQuiet, maxSeq, mergeAlerts, notificationText } from "./format.js";
import { getAlerts, getNotifyEnabled, KEYS, makeClient, type PollStatus } from "./storage.js";

const ALARM = "poll-alerts";
const MAX_NOTIFICATIONS_PER_POLL = 5;

async function setup(): Promise<void> {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);
  if (!(await chrome.alarms.get(ALARM))) {
    await chrome.alarms.create(ALARM, { periodInMinutes: 1, delayInMinutes: 0.1 });
  }
}

let inflight: Promise<PollStatus> | null = null;

function pollAlerts(): Promise<PollStatus> {
  inflight ??= doPoll().finally(() => {
    inflight = null;
  });
  return inflight;
}

async function doPoll(): Promise<PollStatus> {
  const client = makeClient();
  const stored = await chrome.storage.local.get([KEYS.lastSeq, KEYS.baselineDone]);
  const lastSeq = typeof stored[KEYS.lastSeq] === "number" ? (stored[KEYS.lastSeq] as number) : 0;
  const baselineDone = stored[KEYS.baselineDone] === true;
  let status: PollStatus;
  try {
    const { alerts } = await client.alerts(lastSeq, 100);
    const fresh = alerts.filter((a) => a.seq > lastSeq);
    const merged = mergeAlerts(await getAlerts(), fresh);
    await chrome.storage.local.set({
      [KEYS.alerts]: merged,
      [KEYS.lastSeq]: maxSeq(fresh, lastSeq),
      [KEYS.baselineDone]: true,
    });
    // The first successful poll only sets a baseline: no burst of notifications for old alerts.
    if (baselineDone && fresh.length > 0 && (await getNotifyEnabled())) {
      const loud = fresh.filter((a) => !isQuiet(a)).slice(0, MAX_NOTIFICATIONS_PER_POLL);
      for (const a of loud) {
        const { title, message } = notificationText(a);
        await chrome.notifications
          .create(`alert-${a.seq}`, {
            type: "basic",
            iconUrl: chrome.runtime.getURL("icons/icon128.png"),
            title,
            message,
            priority: a.priority >= 3 ? 1 : 0,
          })
          .catch(() => undefined);
      }
    }
    status = { at: Date.now(), ok: true, newCount: fresh.length };
  } catch (err) {
    const msg = err instanceof ApiError ? err.message : "alert poll failed";
    status = { at: Date.now(), ok: false, error: msg };
  }
  await chrome.storage.local.set({ [KEYS.lastPoll]: status });
  return status;
}

chrome.runtime.onInstalled.addListener(() => void setup());
chrome.runtime.onStartup.addListener(() => void setup());
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) void pollAlerts();
});
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && typeof message === "object" && (message as { type?: unknown }).type === "poll-alerts") {
    pollAlerts().then(sendResponse, () => sendResponse({ at: Date.now(), ok: false, error: "poll failed" }));
    return true; // async response
  }
  if (message && typeof message === "object" && (message as { type?: unknown }).type === "reset-alerts") {
    chrome.storage.local
      .remove([KEYS.alerts, KEYS.lastSeq, KEYS.baselineDone, KEYS.lastPoll])
      .then(() => sendResponse({ ok: true }), () => sendResponse({ ok: false }));
    return true;
  }
  return false;
});
void setup();
