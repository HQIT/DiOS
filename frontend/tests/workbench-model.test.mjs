import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  initialModel,
  reduceModel,
  currentRun,
  selectWorks,
  exportSnapshot,
  SCENARIOS,
} from "../src/apps/workbench/model.ts";

const apply = (m, type, id = "demo-w3", role = "employee") =>
  reduceModel(m, { type, id, role });
test("technical success requires separate supervisor business acceptance", () => {
  const m = initialModel();
  assert.equal(currentRun(m.works[2]).phase, "succeeded");
  assert.equal(m.works[2].state, "awaiting_review");
  assert.equal(apply(m, "accept").works[2].state, "awaiting_review");
  assert.equal(
    apply(m, "accept", "demo-w3", "admin").works[2].state,
    "awaiting_review",
  );
  const accepted = apply(m, "accept", "demo-w3", "supervisor");
  assert.equal(accepted.works[2].state, "completed");
  assert.equal(
    apply(accepted, "return", "demo-w3", "supervisor").works[2].state,
    "completed",
  );
});
test("business revision preserves old run and artifact and creates a new run", () => {
  let m = apply(initialModel(), "return", "demo-w3", "supervisor");
  const original = structuredClone(m.works[2].runs[0]);
  m = apply(m, "revise");
  assert.equal(m.works[2].runs.length, 2);
  assert.deepEqual(m.works[2].runs[0], original);
  assert.notEqual(currentRun(m.works[2]).id, original.id);
});
test("transport retry retains logical run, previous session and partial artifact", () => {
  const before = initialModel();
  const old = currentRun(before.works[3]);
  const after = apply(before, "retry", "demo-w4");
  const r = currentRun(after.works[3]);
  assert.equal(r.id, old.id);
  assert.equal(r.attemptIds.length, 2);
  assert.equal(r.previousAttempts[0].sessionId, old.sessionId);
  assert.equal(r.previousAttempts[0].output, old.output);
  assert.ok(old.output);
  assert.equal(r.sessionId, null);
  assert.equal(old.attemptIds.length, 1);
});
test("stale observation never starts a new attempt or changes last-known phase", () => {
  let m = apply(initialModel(), "stale", "demo-w4");
  m = apply(m, "retry", "demo-w4");
  assert.equal(currentRun(m.works[3]).phase, "cooling");
  assert.equal(currentRun(m.works[3]).attemptIds.length, 1);
  m = apply(m, "stale", "demo-w4");
  m = apply(m, "retry", "demo-w4");
  assert.equal(currentRun(m.works[3]).attemptIds.length, 2);
});
test("cancel request is not cancellation until execution confirmation", () => {
  let m = apply(initialModel(), "cancel", "demo-w2");
  assert.equal(currentRun(m.works[1]).phase, "cancel_requested");
  assert.equal(m.works[1].state, "in_progress");
  m = apply(m, "advance", "demo-w2");
  assert.equal(currentRun(m.works[1]).phase, "cancel_requested");
  m = apply(m, "confirm_cancel", "demo-w2");
  assert.equal(m.works[1].state, "cancelled");
});
test("approval and business acceptance remain separate and role constrained", () => {
  let m = initialModel();
  assert.equal(apply(m, "approve", "demo-w1").works[0].approval, "pending");
  assert.equal(
    currentRun(apply(m, "advance", "demo-w1").works[0]).phase,
    "validating",
  );
  m = apply(m, "approve", "demo-w1", "supervisor");
  assert.equal(m.works[0].state, "in_progress");
  m = apply(m, "advance", "demo-w1");
  assert.equal(m.works[0].state, "awaiting_review");
  const denied = apply(initialModel(), "deny", "demo-w1", "supervisor");
  assert.equal(denied.works[0].approval, "denied");
});
test("shared query and watermark drive list, counts, contributors and export", () => {
  let m = reduceModel(initialModel(), {
    type: "create",
    scenario: "education",
    title: "来源复核",
  });
  const q = { scenario: "education", state: "all", search: "来源" };
  const snapshot = exportSnapshot(m, q);
  assert.deepEqual(snapshot.works, selectWorks(m, q));
  assert.equal(snapshot.total, 1);
  assert.equal(snapshot.contributors[0].work_count, 1);
  assert.equal(snapshot.watermark, `demo-v${m.revision}`);
  assert.equal(exportSnapshot(m, { ...q, search: "不存在" }).total, 0);
  m = reduceModel(m, { type: "reset" });
  assert.equal(m.works.length, 4);
});
test("all four scenarios complete a new demo work through six observable phases", () => {
  for (const s of SCENARIOS) {
    let m = reduceModel(initialModel(), {
      type: "create",
      scenario: s.id,
      title: s.brief,
    });
    const id = m.works[0].id;
    for (const phase of [
      "preparing",
      "creating_session",
      "waiting_model",
      "streaming",
      "validating",
      "succeeded",
    ]) {
      m = apply(m, "advance", id);
      assert.equal(currentRun(m.works[0]).phase, phase);
    }
    assert.equal(m.works[0].state, "awaiting_review");
    assert.ok(currentRun(m.works[0]).output);
  }
});
test("work states match domain contract and prototype has no data service imports", () => {
  const schema = JSON.parse(
    readFileSync(
      new URL("../../contracts/v1/domain.schema.json", import.meta.url),
      "utf8",
    ),
  );
  const allowed = schema.$defs.WorkItem.properties.state.enum;
  for (const work of initialModel().works)
    assert.ok(allowed.includes(work.state));
  for (const file of ["model.ts", "WorkbenchPage.tsx"]) {
    const code = readFileSync(
      new URL(`../src/apps/workbench/${file}`, import.meta.url),
      "utf8",
    );
    assert.doesNotMatch(
      code,
      /\bfetch\s*\(|\bWebSocket\b|\blocalStorage\b|from\s+["'][^"']*(?:api|auth)[^"']*["']/i,
    );
  }
});
