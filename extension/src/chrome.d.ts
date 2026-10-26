// Minimal ambient declarations for the subset of the Chrome extension APIs this extension uses.
// Hand-written so the only dev dependency is `typescript`.

declare namespace chrome {
  namespace runtime {
    const lastError: { message?: string } | undefined;
    function getURL(path: string): string;
    function sendMessage<T = unknown>(message: unknown): Promise<T>;
    const onMessage: {
      addListener(
        cb: (message: unknown, sender: unknown, sendResponse: (response?: unknown) => void) => boolean | void,
      ): void;
    };
    const onInstalled: { addListener(cb: () => void): void };
    const onStartup: { addListener(cb: () => void): void };
  }

  namespace storage {
    interface StorageChange {
      oldValue?: unknown;
      newValue?: unknown;
    }
    interface StorageArea {
      get(keys: string | string[] | null): Promise<Record<string, unknown>>;
      set(items: Record<string, unknown>): Promise<void>;
      remove(keys: string | string[]): Promise<void>;
    }
    const local: StorageArea;
    const onChanged: {
      addListener(cb: (changes: Record<string, StorageChange>, areaName: string) => void): void;
    };
  }

  namespace tabs {
    interface Tab {
      id?: number;
      url?: string;
      active: boolean;
    }
    function query(q: { active?: boolean; currentWindow?: boolean; lastFocusedWindow?: boolean }): Promise<Tab[]>;
    const onActivated: { addListener(cb: (info: { tabId: number; windowId: number }) => void): void };
    const onUpdated: {
      addListener(cb: (tabId: number, changeInfo: { url?: string; status?: string }, tab: Tab) => void): void;
    };
  }

  namespace sidePanel {
    function setPanelBehavior(behavior: { openPanelOnActionClick: boolean }): Promise<void>;
  }

  namespace alarms {
    interface Alarm {
      name: string;
    }
    function create(name: string, info: { periodInMinutes?: number; delayInMinutes?: number }): Promise<void>;
    function get(name: string): Promise<Alarm | undefined>;
    const onAlarm: { addListener(cb: (alarm: Alarm) => void): void };
  }

  namespace notifications {
    interface Options {
      type: "basic";
      iconUrl: string;
      title: string;
      message: string;
      contextMessage?: string;
      priority?: number;
    }
    function create(id: string, options: Options): Promise<string>;
  }
}
