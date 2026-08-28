import { createStore } from "./state.js";
import { renderStartView } from "./views/start.js";
import { renderPlanView } from "./views/plan.js";

const store = createStore();
const outlet = document.getElementById("studio-view");
const main = document.getElementById("studio-main");
const status = document.getElementById("studio-status");
const tabs = Array.from(document.querySelectorAll(".step-tab"));

function announce(message, tone = "") {
  status.textContent = message;
  status.dataset.tone = tone;
}

function navigate(view, { focus = true } = {}) {
  store.setView(view);
  if (focus) main.focus();
}

function render(state) {
  const canPlan = Boolean(state.project);
  for (const tab of tabs) {
    const selected = tab.dataset.view === state.view;
    tab.setAttribute("aria-current", selected ? "page" : "false");
    if (tab.dataset.view === "plan") tab.disabled = !canPlan;
  }
  outlet.replaceChildren();
  const context = { store, announce, navigate };
  outlet.append(
    state.view === "plan" && canPlan
      ? renderPlanView(context)
      : renderStartView(context),
  );
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
