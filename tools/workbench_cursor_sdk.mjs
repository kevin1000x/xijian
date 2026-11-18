import { createHash } from "node:crypto";
import { open, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const MAX_CANDIDATE_BYTES = 65536;
const VERSION = "1.4.0";

function requireCondition(value, code) {
  if (!value) throw new Error(code);
}

export function createOptions(sdk, { cwd, storeRoot, model }) {
  requireCondition(typeof model === "string" && /^[a-z0-9][a-z0-9._-]{0,159}$/i.test(model)
    && !["auto", "default"].includes(model.toLowerCase()), "sdk_model_required");
  return {
    model: { id: model },
    tools: [],
    disallowedTools: ["mcp", "webFetch", "webSearch", "shell", "task"],
    local: {
      cwd,
      reportedWorkspacePath: "/",
      fs: sdk.emptyFs(),
      settingSources: [],
      enableAgentRetries: false,
      store: new sdk.JsonlLocalAgentStore(storeRoot),
    },
  };
}

async function refuseRun(run, code) {
  try { await run.cancel(); } catch { /* Preserve the admission failure. */ }
  throw new Error(code);
}

export async function receiveRun(run, model) {
  let reportedEmptyTools = false;
  for await (const event of run.stream()) {
    const assistantContent = event.type === "assistant" ? event.message?.content : null;
    const advertisesTools = event.type === "system" && event.tools !== undefined;
    if (event.type === "tool_call" || event.type === "request"
        || (advertisesTools && (!Array.isArray(event.tools) || event.tools.length !== 0))
        || (event.type === "assistant" && (!Array.isArray(assistantContent)
          || assistantContent.some(block => block.type !== "text")))) {
      await refuseRun(run, "sdk_tool_policy_violation");
    }
    // SDK 1.4.0 can label a partial transport response FINISHED with a warning.
    if (event.type === "status" && event.status === "FINISHED" && event.message !== undefined) {
      await refuseRun(run, "sdk_terminal_warning");
    }
    if (event.type === "system" && event.model && event.model.id !== model) {
      await refuseRun(run, "sdk_model_changed");
    }
    reportedEmptyTools ||= advertisesTools;
  }

  const result = await run.wait();
  requireCondition(result.status === "finished", "sdk_generation_unresolved");
  requireCondition(result.model?.id === model, "sdk_model_changed");
  const text = result.result;
  requireCondition(typeof text === "string" && text.trim() && text.isWellFormed()
    && Buffer.byteLength(text, "utf8") <= MAX_CANDIDATE_BYTES, "sdk_candidate_invalid");
  const candidate = Buffer.from(text, "utf8");
  const observedTokens = result.usage?.totalTokens;
  return {
    model,
    tool_events: 0,
    reported_empty_tools: reportedEmptyTools,
    request_id: /^[a-f0-9-]{36}$/i.test(result.requestId ?? "") ? result.requestId : null,
    observed_tokens: Number.isSafeInteger(observedTokens) && observedTokens > 0 ? observedTokens : null,
    candidate_b64: candidate.toString("base64"),
    candidate_sha256: createHash("sha256").update(candidate).digest("hex"),
  };
}

async function readRequest() {
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    requireCondition(length <= 2 * MAX_CANDIDATE_BYTES, "sdk_request_too_large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function installedSdk(root) {
  const modules = join(root, "node_modules", "@cursor");
  const sdkRoot = join(modules, "sdk");
  for (const name of ["sdk", "sdk-win32-x64"]) {
    const pkg = JSON.parse(await readFile(join(modules, name, "package.json"), "utf8"));
    requireCondition(pkg.name === "@cursor/" + name && pkg.version === VERSION, "sdk_version_changed");
  }
  return import(pathToFileURL(join(sdkRoot, "dist", "esm", "index.js")).href);
}

async function writeExclusive(path, bytes) {
  const file = await open(path, "wx");
  try {
    await file.writeFile(bytes);
    await file.sync();
  } finally {
    await file.close();
  }
}

async function main() {
  const [root, mode] = process.argv.slice(2);
  console.log = console.info = (...parts) => console.error(...parts);
  const sdk = await installedSdk(root);
  requireCondition(resolve(sdk.getDefaultSdkAuthPath()) === resolve(root, "sdk-profile", ".cursor", "sdk", "auth.json"),
    "sdk_auth_scope_changed");
  if (mode === "offline") {
    const sentinel = await readFile(join(process.cwd(), "sentinel.txt"), "utf8");
    requireCondition(sentinel === "synthetic-host-canary", "sdk_host_canary_missing");
    const options = createOptions(sdk, { cwd: process.cwd(), storeRoot: process.cwd(), model: "composer-2.5" });
    let entries = 0;
    for await (const unused of await options.local.fs.list("/")) entries++;
    requireCondition(entries === 0, "sdk_virtual_workspace_not_empty");
    let hostFileDenied = false;
    try { await options.local.fs.read("/sentinel.txt"); }
    catch (error) { hostFileDenied = sdk.isFileNotFoundError(error); }
    requireCondition(hostFileDenied, "sdk_virtual_workspace_read_allowed");
    return { status: "ready", version: VERSION, tools: options.tools, excluded: options.disallowedTools,
      setting_sources: options.local.settingSources, virtual_entries: entries, host_file_unavailable: hostFileDenied };
  }

  const auth = await sdk.Cursor.auth.status();
  requireCondition(auth.status === "logged-in", "sdk_login_required");
  if (mode === "catalog") {
    const models = await sdk.Cursor.models.list();
    return { status: "ready", models: models.map(item => item.id).filter(id => typeof id === "string") };
  }
  requireCondition(mode === "prepare" || mode === "generate", "sdk_mode");
  const input = await readRequest();
  const sdkAgent = await sdk.Agent.create(createOptions(sdk, {
    cwd: process.cwd(),
    storeRoot: input.store_root,
    model: input.model,
  }));
  if (mode === "prepare") return { status: "ready", model: input.model };
  requireCondition(typeof input.public_bundle === "string" && input.public_bundle.length <= MAX_CANDIDATE_BYTES,
    "sdk_public_bundle");
  await writeExclusive(input.send_marker, JSON.stringify({ model: input.model, operation: "send" }) + "\n");
  const run = await sdkAgent.send("Return only original UTF-8 Python source defining analyze(df). "
    + "Produce code matching the approved public contract. Return code directly, without Markdown. "
    + "Use no tools.\nThe approved public bundle follows:\n" + input.public_bundle);
  const result = await receiveRun(run, input.model);
  await writeExclusive(input.candidate_file, Buffer.from(result.candidate_b64, "base64"));
  return result;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    const value = await main();
    process.stdout.write(JSON.stringify(value) + "\n", () => process.exit(0));
  } catch (error) {
    const name = /^(?:sdk_[a-z_]+|[A-Za-z][A-Za-z]+Error)$/.test(error?.message ?? "")
      ? error.message : error?.name;
    process.stdout.write(JSON.stringify({ error: name ?? "Error" }) + "\n", () => process.exit(1));
  }
}
