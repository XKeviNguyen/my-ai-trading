// Saved analysis reports: save the current analysis, list saved ones, download Markdown.

import type { App, View } from "../app.js";
import { h, setChildren, status } from "../dom.js";
import { fmtMs } from "../format.js";
import type { ReportSummary } from "../types.js";

/** Save text as a file via a temporary object URL (no HTML injection, no remote URL). */
function download(filename: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export class ReportsView implements View {
  private readonly app: App;
  private readonly msg: HTMLElement;
  private readonly listEl: HTMLElement;

  constructor(root: HTMLElement, app: App) {
    this.app = app;
    this.msg = h("div", { "aria-live": "polite" });
    this.listEl = h("div");
    const save = h("button", { type: "button", onclick: () => void this.save() }, "Save current analysis");
    const refresh = h("button", { type: "button", class: "secondary", onclick: () => void this.load() }, "Refresh");
    setChildren(
      root,
      h("section", { "aria-labelledby": "reports-h" },
        h("h3", { id: "reports-h" }, "Reports"),
        h("p", { class: "small muted" }, "A report freezes the current analysis (facts, events, perspectives) so you can review or share it later."),
        h("div", { class: "row" }, save, refresh),
        this.msg, this.listEl),
    );
  }

  show(): void {
    void this.load();
  }

  private async save(): Promise<void> {
    const inst = this.app.instrument;
    if (!inst) {
      setChildren(this.msg, status("info", "Choose a symbol first."));
      return;
    }
    setChildren(this.msg, status("loading", "Saving report…"));
    try {
      const r = await this.app.client.saveReport(inst.symbol, inst.market, this.app.analysisTf);
      setChildren(this.msg, status("ok", `Saved: ${r.title}`));
      await this.load();
    } catch (err) {
      setChildren(this.msg, this.app.errorView(err));
    }
  }

  private async load(): Promise<void> {
    try {
      const { reports } = await this.app.client.reports();
      setChildren(this.listEl, reports.length === 0
        ? h("p", { class: "muted" }, "No saved reports yet.")
        : h("ul", { class: "plain" }, reports.map((r) => this.row(r))));
    } catch (err) {
      setChildren(this.listEl, this.app.errorView(err));
    }
  }

  private row(r: ReportSummary): HTMLElement {
    const btn = h("button", { type: "button", class: "secondary", "aria-label": `Download ${r.title} as Markdown`,
      onclick: async () => {
        try {
          download(`report-${r.id}.md`, await this.app.client.reportMarkdown(r.id));
        } catch (err) {
          setChildren(this.msg, this.app.errorView(err));
        }
      } }, "Download .md");
    return h("li", null, h("strong", null, r.title), h("span", { class: "small muted" }, ` · ${fmtMs(r.created_at)} `), btn);
  }
}
