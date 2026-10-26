import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));

test("Manifest V3 side panel with a module service worker", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.side_panel.default_path, "sidepanel.html");
  assert.equal(manifest.background.service_worker, "background.js");
  assert.equal(manifest.background.type, "module");
});

test("permissions are exactly the allowed set", () => {
  assert.deepEqual([...manifest.permissions].sort(), ["alarms", "notifications", "sidePanel", "storage"]);
  assert.deepEqual([...manifest.host_permissions].sort(), ["http://127.0.0.1:8765/*", "https://www.binance.com/*"]);
  assert.equal(manifest.optional_permissions, undefined);
  assert.equal(manifest.optional_host_permissions, undefined);
});

test("no content scripts, no CSP loosening, no external connectivity", () => {
  for (const key of ["content_scripts", "content_security_policy", "externally_connectable", "web_accessible_resources", "sandbox", "update_url", "key"]) {
    assert.equal(manifest[key], undefined, key);
  }
  const text = JSON.stringify(manifest);
  for (const bad of ["<all_urls>", '"tabs"', "scripting", "debugger", "cookies", "webRequest", "*://*/*"]) {
    assert.ok(!text.includes(bad), bad);
  }
});
