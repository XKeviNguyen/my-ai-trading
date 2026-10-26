// chrome.storage.local wrappers. The API token lives ONLY in storage.local (never sync) and is never logged.

import { ApiClient } from "./api.js";
import type { Alert } from "./types.js";

export const KEYS = {
  token: "apiToken",
  alerts: "alertsCache",
  lastSeq: "alertsLastSeq",
  baselineDone: "alertsBaselineDone",
  notify: "desktopNotifications",
  lastPoll: "alertsLastPoll",
  lastInstrument: "lastInstrument",
} as const;

export async function getToken(): Promise<string | null> {
  const v = (await chrome.storage.local.get(KEYS.token))[KEYS.token];
  return typeof v === "string" && v.length > 0 ? v : null;
}

export async function setToken(token: string): Promise<void> {
  await chrome.storage.local.set({ [KEYS.token]: token.trim() });
}

export async function clearToken(): Promise<void> {
  await chrome.storage.local.remove(KEYS.token);
}

export async function getAlerts(): Promise<Alert[]> {
  const v = (await chrome.storage.local.get(KEYS.alerts))[KEYS.alerts];
  return Array.isArray(v) ? (v as Alert[]) : [];
}

export interface PollStatus {
  at: number;
  ok: boolean;
  error?: string;
  newCount?: number;
}

export async function getPollStatus(): Promise<PollStatus | null> {
  const v = (await chrome.storage.local.get(KEYS.lastPoll))[KEYS.lastPoll];
  return v && typeof v === "object" ? (v as PollStatus) : null;
}

export async function getNotifyEnabled(): Promise<boolean> {
  return (await chrome.storage.local.get(KEYS.notify))[KEYS.notify] === true;
}

export async function setNotifyEnabled(on: boolean): Promise<void> {
  await chrome.storage.local.set({ [KEYS.notify]: on });
}

export function makeClient(): ApiClient {
  return new ApiClient({ fetch: (input, init) => fetch(input, init), getToken });
}
