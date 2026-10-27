// Shared side-panel state and helpers used by every view.

import { ApiClient, ApiError } from "./api.js";
import { h, status } from "./dom.js";
import type { Instrument } from "./symbol.js";
import type { Profile, UserSettings } from "./types.js";

export type TabId = "analysis" | "copilot" | "studio" | "accounts" | "alerts" | "reports" | "usage" | "methods" | "settings";

export interface View {
  /** Called when the tab becomes visible. */
  show?(): void;
  /** Called when the active instrument or analysis timeframe changes. */
  onInstrument?(): void;
}

type Listener = () => void;

export class App {
  readonly client: ApiClient;
  instrument: Instrument | null = null;
  instrumentSource: "tab" | "manual" | null = null;
  analysisTf = "1h";
  private settingsCache: UserSettings | null = null;
  /** Version tag (ETag) of `settingsCache`; saves send it as If-Match. */
  private settingsTag: string | null = null;
  private profilesCache: Profile[] | null = null;
  private listeners: Listener[] = [];
  private settingsListeners: Listener[] = [];
  selectTab: (id: TabId) => void = () => undefined;

  constructor(client: ApiClient) {
    this.client = client;
  }

  onInstrumentChange(fn: Listener): void {
    this.listeners.push(fn);
  }
  onSettingsChange(fn: Listener): void {
    this.settingsListeners.push(fn);
  }

  setInstrument(inst: Instrument | null, source: "tab" | "manual" | null): void {
    const changed =
      inst?.symbol !== this.instrument?.symbol || inst?.market !== this.instrument?.market || source !== this.instrumentSource;
    this.instrument = inst;
    this.instrumentSource = source;
    if (changed) this.listeners.forEach((fn) => fn());
  }

  setAnalysisTf(tf: string): void {
    if (tf === this.analysisTf) return;
    this.analysisTf = tf;
    this.listeners.forEach((fn) => fn());
  }

  get cachedSettings(): UserSettings | null {
    return this.settingsCache;
  }

  get settingsEtag(): string | null {
    return this.settingsTag;
  }

  async settings(force = false): Promise<UserSettings> {
    if (!this.settingsCache || force) {
      const v = await this.client.getSettingsVersioned();
      this.settingsCache = v.data;
      this.settingsTag = v.etag;
    }
    return this.settingsCache;
  }

  /**
   * GET the current settings (with their version tag), apply `mutate` to a copy and PUT the full object back with
   * If-Match. `baseEtag` is the tag of the settings a form was rendered from: pass it so a form that shows stale
   * values cannot overwrite a newer change. On 412 (kind "changed_elsewhere") nothing is saved and nothing is
   * retried: the cache is reloaded, listeners are told, and the error is rethrown for the view to show.
   */
  async updateSettings(mutate: (s: UserSettings) => void, baseEtag?: string | null): Promise<UserSettings> {
    const current = await this.client.getSettingsVersioned();
    const copy = structuredClone(current.data);
    mutate(copy);
    try {
      const saved = await this.client.putSettingsVersioned(copy, baseEtag ?? current.etag);
      this.settingsCache = saved.data;
      this.settingsTag = saved.etag;
    } catch (err) {
      if (err instanceof ApiError && err.kind === "changed_elsewhere") {
        try {
          await this.settings(true);
          this.settingsListeners.forEach((fn) => fn());
        } catch {
          /* keep the original error */
        }
      }
      throw err;
    }
    this.settingsListeners.forEach((fn) => fn());
    return this.settingsCache;
  }

  async profiles(): Promise<Profile[]> {
    if (!this.profilesCache) this.profilesCache = (await this.client.methodologies()).profiles;
    return this.profilesCache;
  }

  /** Readable error block; offers a shortcut to Settings for token problems. */
  errorView(err: unknown): HTMLElement {
    const msg = err instanceof ApiError ? err.message : "Unexpected error in the extension.";
    const box = h("div", { class: "error-box" }, status("error", msg));
    if (err instanceof ApiError && err.kind === "unauthorized") {
      box.appendChild(h("button", { type: "button", onclick: () => this.selectTab("settings") }, "Open Settings"));
    }
    if (!(err instanceof ApiError)) console.error("[my-ai-trading]", err instanceof Error ? err.name : "error");
    return box;
  }
}
