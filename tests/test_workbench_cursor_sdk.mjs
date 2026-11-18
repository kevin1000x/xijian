import assert from "node:assert/strict";
import test from "node:test";
import { createOptions, receiveRun } from "../tools/workbench_cursor_sdk.mjs";

const model = "composer-2.5";
const original = "def analyze(df):\r\n    return {\"rows\":[]}\r\n";

function fakeResult(events, result = { status: "finished", model: { id: model }, result: original }) {
  return {
    cancelled: 0,
    async *stream() { yield* events; },
    async wait() { return result; },
    async cancel() { this.cancelled++; },
  };
}

test("sets empty tools and filesystem, no ambient settings or retry", () => {
  const fs = {};
  const store = {};
  const data = createOptions({ emptyFs: () => fs, JsonlLocalAgentStore: class { constructor() { return store; } } },
    { cwd: "C:/synthetic", storeRoot: "C:/state", model });
  assert.deepEqual(data.tools, []);
  assert.deepEqual(data.disallowedTools, ["mcp", "webFetch", "webSearch", "shell", "task"]);
  assert.deepEqual(data.local.settingSources, []);
  assert.equal(data.local.fs, fs);
  assert.equal(data.local.reportedWorkspacePath, "/");
  assert.equal(data.local.enableAgentRetries, false);
  assert.equal(data.local.store, store);
  assert.equal(data.model.id, model);
});

test("returns identical original UTF-8 and binds the final model", async () => {
  const run = fakeResult([{ type: "system", subtype: "init", tools: [], model: { id: model } },
    { type: "assistant", message: { content: [{ type: "text", text: original }] } }]);
  const result = await receiveRun(run, model);
  assert.deepEqual(Buffer.from(result.candidate_b64, "base64"), Buffer.from(original, "utf8"));
  assert.equal(result.model, model);
  assert.equal(result.tool_events, 0);
  assert.equal(result.reported_empty_tools, true);
});

for (const event of [
  { type: "tool_call" },
  { type: "request" },
  { type: "system", subtype: "init", tools: ["WebFetch"] },
  { type: "assistant", message: { content: [{ type: "tool_use", name: "WebFetch" }] } },
]) {
  test("cancels unexpected tool or approval event " + JSON.stringify(event), async () => {
    const run = fakeResult([event]);
    await assert.rejects(receiveRun(run, model), /sdk_tool_policy_violation/);
    assert.equal(run.cancelled, 1);
  });
}

test("rejects a switched model and malformed UTF-8 source", async () => {
  await assert.rejects(receiveRun(fakeResult([], { status: "finished", model: { id: "alternate" },
    result: original }), model), /sdk_model_changed/);
  await assert.rejects(receiveRun(fakeResult([], { status: "finished", model: { id: model },
    result: "\uD800" }), model), /sdk_candidate_invalid/);
});

test("rejects retained text when the SDK finishes with a terminal warning", async () => {
  const run = fakeResult([{ type: "status", status: "FINISHED", message: "partial transport output" }]);
  await assert.rejects(receiveRun(run, model), /sdk_terminal_warning/);
  assert.equal(run.cancelled, 1);
});
