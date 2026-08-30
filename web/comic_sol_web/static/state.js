const EMPTY_PLAN = Object.freeze({
  storyPlan: "",
  characterBible: "",
  storyboard: "",
  visualIdentityPack: "",
});
const VIEWS = Object.freeze(["start", "plan", "generate", "review"]);
const EMPTY_GENERATION = Object.freeze({
  jobs: Object.freeze([]),
  accepted: null,
  staged: null,
  qa: null,
  loadedRevision: 0,
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

function freezeJob(value) {
  return Object.freeze({
    job_id: String(value.job_id || ""),
    artifact_job_id: String(value.artifact_job_id || ""),
    project_id: String(value.project_id || ""),
    project_revision: Number(value.project_revision || 0),
    state: String(value.state || ""),
    provider: String(value.provider || ""),
    model: String(value.model || ""),
    auth_mode: String(value.auth_mode || ""),
    attempt: Number(value.attempt || 0),
    retry_count: Number(value.retry_count || 0),
    max_retries: Number(value.max_retries || 0),
    accepted_project_revision: Number(value.accepted_project_revision || 0),
    artifact_state: typeof value.artifact_state === "string" ? value.artifact_state : null,
    can_cancel: value.can_cancel === true,
  });
}

function generationState(
  values,
  previous = EMPTY_GENERATION,
  loadedRevision = 0,
  acceptedOverride = undefined,
) {
  const jobs = Object.freeze(Array.from(values || [], freezeJob));
  const acceptedJobs = jobs
    .filter((job) => job.state === "accepted" && job.artifact_state === "accepted")
    .sort((left, right) => right.accepted_project_revision - left.accepted_project_revision);
  const staged = jobs.find(
    (job) => job.state === "validating" && job.artifact_state === "staged",
  ) || null;
  let accepted = acceptedJobs[0] || previous.accepted;
  if (acceptedOverride === null) accepted = null;
  else if (acceptedOverride !== undefined) accepted = freezeJob(acceptedOverride);
  return Object.freeze({
    jobs,
    accepted,
    staged,
    qa: previous.qa,
    loadedRevision,
  });
}

function initialState() {
  return Object.freeze({
    view: "start",
    project: null,
    workingPlan: EMPTY_PLAN,
    draft: null,
    generation: EMPTY_GENERATION,
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
    for (const listener of listeners) listener(state);
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
      if (!VIEWS.includes(view)) return;
      if (view !== "start" && !state.project) return;
      publish({ ...state, view });
    },
    setProject(project) {
      publish({
        view: "plan",
        project,
        workingPlan: projectPlan(project),
        draft: null,
        generation: EMPTY_GENERATION,
      });
    },
    replaceProject(project) {
      publish({ ...state, project, workingPlan: projectPlan(project) });
    },
    replaceProjectAndGenerationJobs(project, jobs, acceptedJob = undefined) {
      publish({
        ...state,
        project,
        workingPlan: projectPlan(project),
        generation: generationState(
          jobs,
          state.generation,
          project?.revision || 0,
          acceptedJob,
        ),
      });
    },
    setGenerationJobs(jobs, acceptedJob = undefined) {
      publish({
        ...state,
        generation: generationState(
          jobs,
          state.generation,
          state.project?.revision || 0,
          acceptedJob,
        ),
      });
    },
    replaceGenerationJob(job) {
      const jobs = state.generation.jobs.filter((item) => item.job_id !== job.job_id);
      jobs.push(job);
      publish({
        ...state,
        generation: generationState(
          jobs,
          state.generation,
          state.generation.loadedRevision,
        ),
      });
    },
    setQa(project) {
      const qa = project?.summary?.qa || null;
      publish({
        ...state,
        project,
        workingPlan: projectPlan(project),
        generation: Object.freeze({ ...state.generation, qa }),
      });
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
