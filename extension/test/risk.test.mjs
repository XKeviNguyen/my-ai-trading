// Risk calculator helpers (no DOM needed).
import test from "node:test";
import assert from "node:assert/strict";
import { percentToFraction, toRequest, validateRisk } from "../build/src/views/risk.js";

const ok = { capital: "25000", riskPct: "0.8", entry: "100", stop: "95", target: "115", step: "" };

test("percentToFraction moves the decimal point without float error", () => {
  assert.equal(percentToFraction("0.8"), "0.008");
  assert.equal(percentToFraction("1"), "0.01");
  assert.equal(percentToFraction("2"), "0.02");
  assert.equal(percentToFraction("0.25"), "0.0025");
  assert.equal(percentToFraction("0.1"), "0.001");
  assert.equal(percentToFraction("12.5"), "0.125");
  assert.equal(percentToFraction("100"), "1");
});

test("validateRisk accepts a long and builds a decimal-string request", () => {
  assert.deepEqual(validateRisk(ok), {});
  assert.deepEqual(toRequest(ok), { capital: "25000", entry: "100", stop: "95", risk_fraction: "0.008", target: "115" });
  assert.deepEqual(toRequest({ ...ok, target: "", step: "0.001" }),
    { capital: "25000", entry: "100", stop: "95", risk_fraction: "0.008", quantity_step: "0.001" });
});

test("validateRisk reports errors per field", () => {
  assert.ok(validateRisk({ ...ok, capital: "" })["capital"]);
  assert.ok(validateRisk({ ...ok, capital: "-5" })["capital"]);
  assert.ok(validateRisk({ ...ok, entry: "1e5" })["entry"]); // scientific notation is not accepted
  assert.ok(validateRisk({ ...ok, riskPct: "2.5" })["riskPct"]);
  assert.ok(validateRisk({ ...ok, riskPct: "0.001" })["riskPct"]); // below 0.01%
  assert.deepEqual(validateRisk({ ...ok, riskPct: "0.01" }), {});
  assert.ok(validateRisk({ ...ok, stop: "100" })["stop"]);
  assert.ok(validateRisk({ ...ok, target: "90" })["target"]);       // long with target below entry
  assert.deepEqual(validateRisk({ ...ok, stop: "105", target: "90" }), {}); // a short
  assert.ok(validateRisk({ ...ok, stop: "105", target: "110" })["target"]);
  assert.ok(validateRisk({ ...ok, step: "abc" })["step"]);
});
