import { registerCreatorWebMcp } from "./app.js";

const CORE_TOOL_COUNT = 14;
const CREATOR_TOOL_NAMES = Object.freeze([
  "get_comic_context",
  "create_comic",
  "revise_comic",
]);
const MAX_ATTEMPTS = 40;
const RETRY_DELAY_MS = 25;

function modelContext() {
  const documentContext = typeof document === "undefined" ? null : document.modelContext;
  if (documentContext && typeof documentContext.getTools === "function") return documentContext;
  const navigatorContext = typeof navigator === "undefined" ? null : navigator.modelContext;
  return navigatorContext && typeof navigatorContext.getTools === "function"
    ? navigatorContext
    : null;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function registerCreatorToolsAfterCore() {
  const context = modelContext();
  if (!context) return false;

  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt += 1) {
    const tools = await context.getTools();
    const names = new Set(Array.isArray(tools) ? tools.map((tool) => tool?.name) : []);
    if (CREATOR_TOOL_NAMES.every((name) => names.has(name))) return true;
    if (tools.length >= CORE_TOOL_COUNT) {
      return registerCreatorWebMcp();
    }
    await delay(RETRY_DELAY_MS);
  }
  return false;
}

void registerCreatorToolsAfterCore();
