// Pure rules for the order journal (no DOM): which actions a row offers and what its verification says.
// Same rules and wording as the desktop client (desktop/my_ai_trading_desktop/formatting.py).
// Nothing here ever suggests placing an order again: a missing listing is never proof an order did not execute.

import { fmtMs } from "./format.js";
import type { JournalRow } from "./types.js";

/** States the engine never changes again. NOT_FOUND (older versions) is NOT final: its outcome is unsettled. */
export const FINAL_STATES: readonly string[] = ["FILLED", "CANCELED", "REJECTED", "EXPIRED", "USER_RESOLVED"];
/** Never-acknowledged states a user may mark resolved after checking the exchange's order history. */
export const RESOLVABLE_STATES: readonly string[] = ["SENDING", "UNKNOWN", "NOT_FOUND"];

export const NOT_LISTED_TEXT = "Exchange did not list this order at the last check — do not place it again.";

export const RESOLVE_CONFIRM_TEXT =
  "Mark this order resolved?\n\n" +
  "Only do this after you have checked this order in the exchange's order history (on the exchange's website or app). " +
  "The app then stops tracking it. Nothing is sent to the exchange: this does not cancel anything there.";

export const CANCEL_CONFIRM_TEXT =
  "Cancel this testnet order?\n\nIf its status is uncertain, the app asks the exchange to cancel it and then looks it up.";

export function isFinal(state: string): boolean {
  return FINAL_STATES.includes(state);
}

/** Cancel lowers risk, so every unsettled order offers it (also UNKNOWN / NOT_FOUND / not listed). */
export function canCancel(row: Pick<JournalRow, "state">): boolean {
  return !isFinal(row.state);
}

/** Only for orders the exchange never acknowledged; acknowledged orders get their state from the exchange. */
export function canResolve(row: Pick<JournalRow, "state" | "acknowledged">): boolean {
  return !row.acknowledged && RESOLVABLE_STATES.includes(row.state);
}

/** Plain-text lines describing the last exchange check of a journal row. */
export function verificationLines(row: Pick<JournalRow, "verification" | "acknowledged" | "last_checked_at" | "last_seen_at" | "lookups">): string[] {
  const out: string[] = [];
  if (row.verification === "not_listed") {
    out.push(NOT_LISTED_TEXT);
    out.push(row.acknowledged
      ? "The exchange confirmed this order before. Listings can lag or be purged: check your order history at the exchange."
      : "Listings can lag, so this does not prove it never executed. Check the exchange's order history.");
  } else if (row.verification === "lookup_failed") {
    out.push("The last status check failed. Check again later; do not place it again.");
  } else if (row.verification === "confirmed") {
    out.push("Confirmed by the exchange at the last check.");
  }
  if (row.last_checked_at) out.push(`Last checked: ${fmtMs(row.last_checked_at)}`);
  if (row.last_checked_at || row.last_seen_at) {
    out.push(`Last seen by the exchange: ${row.last_seen_at ? fmtMs(row.last_seen_at) : "never"}`);
  }
  if (typeof row.lookups === "number" && row.lookups > 0) out.push(`Not listed in ${row.lookups} spaced check${row.lookups === 1 ? "" : "s"}.`);
  return out;
}

export function stateTone(row: Pick<JournalRow, "state" | "verification">, stuck: boolean): "ok" | "warn" | "err" | "muted" {
  if (row.state === "REJECTED") return "err";
  if (stuck || row.verification === "not_listed" || row.verification === "lookup_failed" || row.state === "NOT_FOUND") return "warn";
  return "muted";
}
