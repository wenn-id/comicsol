import { getCurrentProject } from "./api.js";
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
void registerWebMcp();

async function restoreProject() {
  try {
    if (await restoreCurrentProject(store, getCurrentProject)) {
      announce("Restored your current project.", "success");
    }
  } catch (_error) {
    announce("Your saved project could not be restored safely.", "error");
  }
}

void restoreProject();
