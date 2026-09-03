import { createProject, getCurrentProject, updatePlan } from "./api.js";
import { registerWebMcp } from "./webmcp.js";
import { createStore, restoreCurrentProject } from "./state.js";
import { renderStartView } from "./views/start.js";
import { renderPlanView } from "./views/plan.js";
import { renderGenerateView } from "./views/generate.js";
import { disposeReviewView, renderReviewView } from "./views/review.js";

const store = createStore();
const outlet = document.getElementById("studio-view");
const main = document.getElementById("studio-main");
const status = document.getElementById("studio-status");
const tabs = Array.from(document.querySelectorAll(".step-tab"));
const CREATOR_LOCAL_STORAGE_KEY = "comic-sol:webmcp-creator-v1";
let renderedView = null;

function announce(message, tone = "") {
  status.textContent = message;
  status.dataset.tone = tone;
}

function navigate(view, { focus = true } = {}) {
  store.setView(view);
  if (focus) main.focus();
}

function render(state) {
  if (renderedView === "review") {
    disposeReviewView({ preservePendingExport: state.view === "review" });
  }
  renderedView = state.view;
  const canPlan = Boolean(state.project);
  const canGenerate = Boolean(state.project);
  const canReview = Boolean(state.project);
  for (const tab of tabs) {
    const selected = tab.dataset.view === state.view;
    tab.setAttribute("aria-current", selected ? "page" : "false");
    if (tab.dataset.view === "plan") tab.disabled = !canPlan;
    if (tab.dataset.view === "generate") tab.disabled = !canGenerate;
    if (tab.dataset.view === "review") tab.disabled = !canReview;
  }
  outlet.replaceChildren();
  const context = { store, announce, navigate };
  if (state.view === "plan" && canPlan) outlet.append(renderPlanView(context));
  else if (state.view === "generate" && canGenerate) outlet.append(renderGenerateView(context));
  else if (state.view === "review" && canReview) outlet.append(renderReviewView(context));
  else outlet.append(renderStartView(context));
}

class CreatorInputError extends Error {}

function creatorSchema(properties, required) {
  return Object.freeze({
    type: "object",
    properties: Object.freeze(properties),
    required: Object.freeze(required),
    additionalProperties: false,
  });
}

function assertCreatorObject(input, required) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new CreatorInputError();
  }
  const keys = Object.keys(input).sort();
  const expected = [...required].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new CreatorInputError();
  }
  return input;
}

function creatorString(value, minimum, maximum) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum) {
    throw new CreatorInputError();
  }
  return value;
}

function creatorPageCount(value) {
  if (!Number.isInteger(value) || value < 1 || value > 4) throw new CreatorInputError();
  return value;
}

function creatorPlan(value) {
  const plan = assertCreatorObject(value, [
    "storyPlan",
    "characterBible",
    "storyboard",
    "visualIdentityPack",
  ]);
  return {
    storyPlan: creatorString(plan.storyPlan, 0, 1048576),
    characterBible: creatorString(plan.characterBible, 0, 1048576),
    storyboard: creatorString(plan.storyboard, 0, 1048576),
    visualIdentityPack: creatorString(plan.visualIdentityPack, 0, 1048576),
  };
}

function creatorSafeExecute(operation) {
  return async (input) => {
    try {
      return { ok: true, data: await operation(input ?? {}) };
    } catch (error) {
      if (error instanceof CreatorInputError) {
        return { ok: false, error: { code: "invalid_input" } };
      }
      const statusCode = Number(error?.status || 0);
      if (statusCode === 401 || statusCode === 403) {
        return { ok: false, error: { code: "session_unavailable" } };
      }
      if (statusCode === 409) return { ok: false, error: { code: "project_changed" } };
      return { ok: false, error: { code: "studio_operation_failed" } };
    }
  };
}

function hasStudioSession() {
  if (typeof document === "undefined") return false;
  return document.cookie
    .split(";")
    .some((part) => part.trim().startsWith("comic_sol_csrf="));
}

function isBrowserLocalProject(project) {
  return Boolean(project && typeof project.project_id === "string" && project.project_id.startsWith("local:"));
}

function validateLocalCreatorProject(project) {
  if (!project || typeof project !== "object") return null;
  if (typeof project.project_id !== "string" || !Number.isInteger(project.revision)) return null;
  if (typeof project.status !== "string" || !project.summary || typeof project.summary !== "object") {
    return null;
  }
  try {
    creatorPlan(project.summary.plan);
  } catch (_error) {
    return null;
  }
  return project;
}

function loadLocalCreatorProject() {
  if (typeof localStorage === "undefined") return null;
  try {
    const raw = localStorage.getItem(CREATOR_LOCAL_STORAGE_KEY);
    return raw ? validateLocalCreatorProject(JSON.parse(raw)) : null;
  } catch (_error) {
    return null;
  }
}

function saveLocalCreatorProject(project) {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(CREATOR_LOCAL_STORAGE_KEY, JSON.stringify(project));
  } catch (_error) {
    // Browser-local creator mode is best-effort; the in-memory Store still works.
  }
}

function makeLocalCreatorProject({ title, concept, language, pageCount, visualStyle, plan }) {
  return {
    project_id: `local:${crypto.randomUUID()}`,
    revision: 1,
    status: "STORYBOARDED",
    summary: {
      title,
      plan,
      creator: {
        concept,
        language,
        page_count: pageCount,
        visual_style: visualStyle,
      },
    },
  };
}

function creatorProjectSummary(project) {
  return {
    project_id: project.project_id,
    revision: project.revision,
    status: project.status,
    plan_available: Boolean(project.summary?.plan),
    mode: isBrowserLocalProject(project) ? "browser-local" : "studio",
  };
}

async function currentCreatorProject() {
  try {
    const project = await getCurrentProject();
    if (project) return project;
  } catch (error) {
    if (Number(error?.status || 0) !== 404) throw error;
  }
  return loadLocalCreatorProject();
}

async function getComicContext(input) {
  assertCreatorObject(input, []);
  const project = await currentCreatorProject();
  if (!project) return { available: false };
  return {
    available: true,
    project_id: project.project_id,
    revision: project.revision,
    status: project.status,
    mode: isBrowserLocalProject(project) ? "browser-local" : "studio",
    plan: {
      storyPlan: project.summary.plan.storyPlan,
      characterBible: project.summary.plan.characterBible,
      storyboard: project.summary.plan.storyboard,
      visualIdentityPack: project.summary.plan.visualIdentityPack,
    },
  };
}

async function createComic(input) {
  const request = assertCreatorObject(input, [
    "title",
    "concept",
    "language",
    "page_count",
    "visual_style",
    "plan",
  ]);
  const title = creatorString(request.title, 1, 160);
  const concept = creatorString(request.concept, 1, 200000);
  const language = creatorString(request.language, 1, 16);
  const pageCount = creatorPageCount(request.page_count);
  const visualStyle = creatorString(request.visual_style, 1, 4000);
  const plan = creatorPlan(request.plan);
  const prompt = [
    concept,
    "",
    `Visual direction: ${visualStyle}`,
    `Create a coherent ${pageCount}-page comic or manga plan with reusable character details, storyboard beats, and visual identity.`,
  ].join("\n");
  let project = null;
  if (hasStudioSession()) {
    try {
      project = await createProject(
        { title, prompt, language, mode: "short_prompt", page_count: pageCount },
        crypto.randomUUID(),
      );
    } catch (error) {
      if (Number(error?.status || 0) !== 404) throw error;
    }
  }
  if (!project) {
    project = makeLocalCreatorProject({
      title,
      concept,
      language,
      pageCount,
      visualStyle,
      plan,
    });
    saveLocalCreatorProject(project);
  }
  store.setProject(project);
  navigate("plan", { focus: false });
  announce("Comic plan created. Review or revise it with your agent.", "success");
  return creatorProjectSummary(project);
}

async function reviseComic(input) {
  const request = assertCreatorObject(input, ["instruction", "plan"]);
  creatorString(request.instruction, 1, 20000);
  const plan = creatorPlan(request.plan);
  const current = await currentCreatorProject();
  if (!current) throw new CreatorInputError();
  let project = null;
  if (!isBrowserLocalProject(current) && hasStudioSession()) {
    try {
      project = await updatePlan(current.project_id, plan, current.revision, crypto.randomUUID());
    } catch (error) {
      if (Number(error?.status || 0) !== 404) throw error;
    }
  }
  if (!project) {
    project = {
      ...current,
      revision: current.revision + 1,
      status: "STORYBOARDED",
      summary: { ...current.summary, plan },
    };
    saveLocalCreatorProject(project);
  }
  store.setProject(project);
  navigate("plan", { focus: false });
  announce("Comic plan revised from your creative direction.", "success");
  return creatorProjectSummary(project);
}

const CREATOR_PLAN_SCHEMA = creatorSchema(
  {
    storyPlan: { type: "string", maxLength: 1048576 },
    characterBible: { type: "string", maxLength: 1048576 },
    storyboard: { type: "string", maxLength: 1048576 },
    visualIdentityPack: { type: "string", maxLength: 1048576 },
  },
  ["storyPlan", "characterBible", "storyboard", "visualIdentityPack"],
);

const CREATOR_TOOL_DEFINITIONS = Object.freeze([
  {
    name: "get_comic_context",
    description:
      "Read the active creator-owned ComicSol story plan, character bible, storyboard, and visual identity so the agent can reason about coherent revisions.",
    inputSchema: creatorSchema({}, []),
    annotations: Object.freeze({ readOnlyHint: true }),
    execute: creatorSafeExecute(getComicContext),
  },
  {
    name: "create_comic",
    description:
      "Create a new ComicSol comic or manga from a creator concept. Draft the four-part Plan from the user's request and pass it here; ComicSol handles project mechanics and can fall back to browser-local mode on the hosted static Studio.",
    inputSchema: creatorSchema(
      {
        title: { type: "string", minLength: 1, maxLength: 160 },
        concept: { type: "string", minLength: 1, maxLength: 200000 },
        language: { type: "string", minLength: 1, maxLength: 16 },
        page_count: { type: "integer", minimum: 1, maximum: 4 },
        visual_style: { type: "string", minLength: 1, maxLength: 4000 },
        plan: CREATOR_PLAN_SCHEMA,
      },
      ["title", "concept", "language", "page_count", "visual_style", "plan"],
    ),
    annotations: Object.freeze({ readOnlyHint: false }),
    execute: creatorSafeExecute(createComic),
  },
  {
    name: "revise_comic",
    description:
      "Apply a creator-requested story, character, storyboard, or visual revision after reading the current comic context. Pass the fully revised four-part Plan; project revision mechanics stay inside ComicSol.",
    inputSchema: creatorSchema(
      {
        instruction: { type: "string", minLength: 1, maxLength: 20000 },
        plan: creatorSchema(
          {
            storyPlan: { type: "string", maxLength: 1048576 },
            characterBible: { type: "string", maxLength: 1048576 },
            storyboard: { type: "string", maxLength: 1048576 },
            visualIdentityPack: { type: "string", maxLength: 1048576 },
          },
          ["storyPlan", "characterBible", "storyboard", "visualIdentityPack"],
        ),
      },
      ["instruction", "plan"],
    ),
    annotations: Object.freeze({ readOnlyHint: false }),
    execute: creatorSafeExecute(reviseComic),
  },
]);

function creatorModelContext() {
  const navigatorContext = typeof navigator === "undefined" ? null : navigator.modelContext;
  if (navigatorContext && typeof navigatorContext.registerTool === "function") {
    return navigatorContext;
  }
  const documentContext = typeof document === "undefined" ? null : document.modelContext;
  if (documentContext && typeof documentContext.registerTool === "function") {
    return documentContext;
  }
  return null;
}

export async function registerCreatorWebMcp() {
  const context = creatorModelContext();
  if (!context) return false;
  try {
    for (const definition of CREATOR_TOOL_DEFINITIONS) {
      await context.registerTool(definition);
    }
    return true;
  } catch (_error) {
    return false;
  }
}

for (const tab of tabs) {
  tab.addEventListener("click", () => navigate(tab.dataset.view));
  tab.addEventListener("keydown", (event) => {
    const enabled = tabs.filter((item) => !item.disabled);
    const position = enabled.indexOf(event.currentTarget);
    let next = null;
    if (event.key === "ArrowRight") next = enabled[(position + 1) % enabled.length];
    if (event.key === "ArrowLeft") next = enabled[(position - 1 + enabled.length) % enabled.length];
    if (event.key === "Home") next = enabled[0];
    if (event.key === "End") next = enabled[enabled.length - 1];
    if (!next) return;
    event.preventDefault();
    next.focus();
  });
}

store.subscribe(render);
render(store.getState());
document.addEventListener("comic-sol:project-selected", (event) => {
  const project = event.detail?.project;
  if (!project || typeof project.project_id !== "string" || !Number.isInteger(project.revision)) {
    return;
  }
  store.setProject(project);
  event.detail.accepted = true;
  announce("Project opened. Plan is ready for review.", "success");
});
document.addEventListener("comic-sol:qa-completed", (event) => {
  const project = event.detail?.project;
  if (!project || typeof project.project_id !== "string" || !Number.isInteger(project.revision)) {
    return;
  }
  const current = store.getState().project;
  if (!current || current.project_id !== project.project_id || current.revision !== project.revision) {
    return;
  }
  store.setQa(project);
  event.detail.accepted = true;
  announce("QA completed.", "success");
});
document.addEventListener("comic-sol:generation-refreshed", (event) => {
  const project = event.detail?.project;
  if (!project || typeof project.project_id !== "string" || !Number.isInteger(project.revision)) {
    return;
  }
  const current = store.getState().project;
  if (!current || current.project_id !== project.project_id) {
    return;
  }
  store.replaceProjectAndGenerationJobs(
    project,
    Array.isArray(event.detail.jobs) ? event.detail.jobs : [],
    event.detail.acceptedJob ?? undefined,
  );
  event.detail.accepted = true;
  announce("Generation queued and refreshed.", "success");
});
void registerWebMcp();
void registerCreatorWebMcp();

async function restoreProject() {
  try {
    if (await restoreCurrentProject(store, getCurrentProject)) {
      announce("Restored your current project.", "success");
      return;
    }
  } catch (error) {
    if (Number(error?.status || 0) !== 404) {
      announce("Your saved project could not be restored safely.", "error");
      return;
    }
  }
  const localProject = loadLocalCreatorProject();
  if (localProject) {
    store.setProject(localProject);
    announce("Restored your browser-local creator project.", "success");
  }
}

void restoreProject();
