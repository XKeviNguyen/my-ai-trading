// Tiny DOM builder. Text is always inserted as text nodes; there is no HTML-string path at all.

export type Child = Node | string | number | null | undefined | false | Child[];
type AttrValue = string | number | boolean | null | undefined;
type Attrs = Record<string, AttrValue | ((ev: Event) => void)>;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs | null = null,
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (typeof value === "function") {
      if (!key.startsWith("on")) throw new Error(`handler for non-event attribute ${key}`);
      el.addEventListener(key.slice(2).toLowerCase(), value);
      continue;
    }
    if (key.toLowerCase().startsWith("on")) throw new Error("inline event handler attributes are not allowed");
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = String(value);
    else if (key === "value" && "value" in el) (el as HTMLInputElement).value = String(value);
    else if (key === "checked" && el instanceof HTMLInputElement) el.checked = value === true;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

export function append(parent: Node, children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(parent, c);
    else if (typeof c === "string" || typeof c === "number") parent.appendChild(document.createTextNode(String(c)));
    else parent.appendChild(c);
  }
}

export function clear(el: Element): void {
  el.replaceChildren();
}

export function setChildren(el: Element, ...children: Child[]): void {
  el.replaceChildren();
  append(el, children);
}

let uid = 0;
export function nextId(prefix: string): string {
  uid += 1;
  return `${prefix}-${uid}`;
}

/** A labeled form control wrapper. */
export function field(label: string, control: HTMLElement, hint?: string): HTMLElement {
  const id = control.id || nextId("f");
  control.id = id;
  const hintEl = hint ? h("small", { class: "hint", id: `${id}-hint` }, hint) : null;
  if (hintEl) control.setAttribute("aria-describedby", hintEl.id);
  return h("div", { class: "field" }, h("label", { for: id }, label), control, hintEl);
}

export function badge(text: string, tone: "ok" | "warn" | "err" | "muted" | "info" = "muted"): HTMLElement {
  return h("span", { class: `badge badge-${tone}` }, text);
}

export function list(items: string[] | undefined, empty = "None"): HTMLElement {
  if (!items || items.length === 0) return h("p", { class: "muted" }, empty);
  return h("ul", { class: "plain" }, items.map((t) => h("li", null, t)));
}

/** Status area: loading / error / info messages announced to assistive tech. */
export function status(kind: "loading" | "error" | "info" | "ok", text: string): HTMLElement {
  return h(
    "p",
    { class: `status status-${kind}`, role: kind === "error" ? "alert" : "status" },
    kind === "loading" ? "⏳ " : "",
    text,
  );
}
