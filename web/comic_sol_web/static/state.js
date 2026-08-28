const EMPTY_PLAN = Object.freeze({
  storyPlan: "",
  storyboard: "",
  visualIdentityPack: "",
});

function freezePlan(value = EMPTY_PLAN) {
  return Object.freeze({
    storyPlan: typeof value.storyPlan === "string" ? value.storyPlan : "",
    storyboard: typeof value.storyboard === "string" ? value.storyboard : "",
    visualIdentityPack:
      typeof value.visualIdentityPack === "string" ? value.visualIdentityPack : "",
  });
}

function initialState() {
  return Object.freeze({
    view: "start",
    project: null,
    workingPlan: EMPTY_PLAN,
    draft: null,
  });
}

export function createStore() {
  let state = initialState();
  const listeners = new Set();

  function publish(next) {
    state = Object.freeze(next);
    for (const listener of listeners) {
      listener(state);
    }
  }

  return Object.freeze({
    getState() {
      return state;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setView(view) {
      if (view !== "start" && view !== "plan") return;
      if (view === "plan" && !state.project) return;
      publish({ ...state, view });
    },
    setProject(project, source = "") {
      const workingPlan = freezePlan({ storyPlan: source });
      publish({ view: "plan", project, workingPlan, draft: null });
    },
    replaceProject(project) {
      publish({ ...state, project });
    },
    createDraft(changes, origin = "creator") {
      if (!state.project) return;
      publish({
        ...state,
        draft: Object.freeze({
          expectedRevision: state.project.revision,
          origin: origin === "agent" ? "agent" : "creator",
          changes: freezePlan(changes),
        }),
      });
    },
    clearDraft() {
      publish({ ...state, draft: null });
    },
    promoteDraft() {
      if (!state.project || !state.draft) return false;
      if (state.project.revision !== state.draft.expectedRevision) {
        publish({ ...state, draft: null });
        return false;
      }
      publish({ ...state, workingPlan: state.draft.changes, draft: null });
      return true;
    },
  });
}
