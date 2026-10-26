// Static checks on the TypeScript source and the built extension.
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}
const code = [...walk(join(root, "src")), ...walk(dist).filter((f) => /\.(js|html)$/.test(f))].map((f) => ({
  f,
  text: readFileSync(f, "utf8"),
}));

test("no HTML-string sinks or dynamic code execution", () => {
  const banned = [
    /\.innerHTML\s*[+]?=/,
    /\.outerHTML\s*=/,
    /insertAdjacentHTML/,
    /document\.write/,
    /\beval\s*\(/,
    /new\s+Function\s*\(/,
    /setTimeout\s*\(\s*["'`]/,
    /setInterval\s*\(\s*["'`]/,
    /createContextualFragment/,
    /DOMParser/,
  ];
  for (const { f, text } of code) for (const re of banned) assert.ok(!re.test(text), `${re} found in ${f}`);
});

test("token is never synced, logged or sent anywhere but the local API", () => {
  for (const { f, text } of code) {
    assert.ok(!/storage\.sync/.test(text), `storage.sync in ${f}`);
    assert.ok(!/console\.(log|info|debug|warn|error)\([^)]*[Tt]oken/.test(text), `token logged in ${f}`);
  }
  const urls = new Set();
  for (const { text } of code) for (const m of text.matchAll(/\bhttps?:\/\/[^\s"'`)<>]+/g)) urls.add(m[0].replace(/[.,;]+$/, ""));
  const allowed = [/^http:\/\/127\.0\.0\.1:8765/, /^https:\/\/www\.binance\.com/];
  for (const u of urls) assert.ok(allowed.some((re) => re.test(u)), `unexpected URL ${u}`);
});

test("no remote scripts or styles in the side panel HTML", () => {
  const html = readFileSync(join(dist, "sidepanel.html"), "utf8");
  for (const m of html.matchAll(/(?:src|href)="([^"]+)"/g)) assert.ok(!/^[a-z]+:|^\/\//i.test(m[1]), `remote ref ${m[1]}`);
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/.test(html), "inline script present");
  assert.ok(!/\son[a-z]+=/i.test(html), "inline event handler present");
});

test("dist contains the files the manifest references", () => {
  const manifest = JSON.parse(readFileSync(join(dist, "manifest.json"), "utf8"));
  for (const p of [manifest.side_panel.default_path, manifest.background.service_worker, ...Object.values(manifest.icons)]) {
    assert.ok(existsSync(join(dist, p)), `missing ${p}`);
  }
});
