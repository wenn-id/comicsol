const EMPTY_PLAN = Object.freeze({
  storyPlan: "",
  characterBible: "",
  storyboard: "",
  visualIdentityPack: "",
});

function freezePlan(value = EMPTY_PLAN) {
  return Object.freeze({
    storyPlan: typeof value.storyPlan === "string" ? value.storyPlan : "",
    characterBible: typeof value.characterBible === "string" ? value.characterBible : "",
    storyboard: typeof value.storyboard === "string" ? value.storyboard : "",
    visualIdentityPack:
      typeof value.visualIdentityPack === "string" ? value.visualIdentityPack : "",
  });
}

function projectPlan(project) {
  return freezePlan(project?.summary?.plan);
}

function initialState() {
  return Object.freeze({
    view: "start",
    project: null,
    workingPlan: EMPTY_PLAN,
    draft: null,
  });
}

export async function restoreCurrentProject(store, loadCurrentProject) {
  const initial = store.getState();
  const project = await loadCurrentProject();
  if (!project || store.getState() !== initial) return false;
  store.setProject(project);
  return true;
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
    setProject(project) {
      publish({ view: "plan", project, workingPlan: projectPlan(project), draft: null });
    },
    replaceProject(project) {
      publish({ ...state, project, workingPlan: projectPlan(project) });
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
    promoteDraft(project) {
      if (!state.project || !state.draft || !project) return false;
      if (state.project.revision !== state.draft.expectedRevision) {
        publish({ ...state, draft: null });
        return false;
      }
      publish({
        ...state,
        project,
        workingPlan: projectPlan(project),
        draft: null,
      });
      return true;
    },
  });
}
