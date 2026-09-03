import {
  approveProposal,
  cancelGeneration,
  getGenerationOptions,
  getGenerationRecommendations,
  getProject,
  getWorkflow,
  listGenerationJobs,
  pauseForSwitch,
  pauseWorkflow,
  queueGeneration,
  rejectProposal,
  resumeWorkflow,
  retryGeneration,
  submitStagedRaster,
} from "../api.js";

const JOB_STATES = new Set([
  "queued", "running", "polling", "validating", "accepted",
  "awaiting_provider_confirmation", "paused", "failed", "cancelled",
]);
const RETRY_STATES = new Set(["failed"]);
const SWITCH_STATES = new Set(["failed", "paused"]);
const ACTIVE_STATES = new Set([
  "queued", "running", "polling", "awaiting_provider_confirmation",
]);
const REFRESH_DELAY_MS = 2000;
let optionCache = null;

function element(tag, text, attributes = {}) {
  const node = document.createElement(tag);
  if (text !== null) node.textContent = text;
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function button(text, enabled, action) {
  const control = element("button", text, { type: "button", class: "button" });
  control.disabled = !enabled;
  if (enabled) control.addEventListener("click", action);
  return control;
}

function focusStudioMain() {
  document.getElementById("studio-main")?.focus();
}

function showSwitchDialog(proposal, projectId, expectedRevision, refresh, announce, trigger) {
  const previousFocus = trigger;
  const dialog = document.createElement("dialog");
  dialog.setAttribute("aria-labelledby", "switch-dialog-heading");
  dialog.append(
    element("h3", "Confirm provider switch", { id: "switch-dialog-heading" }),
    element(
      "p",
      `Switch from ${proposal.from_provider} to ${proposal.to_provider} / ${proposal.to_model}?`,
    ),
  );
  const actions = element("div", null, { class: "actions" });
  const confirm = button("Confirm switch", true, async () => {
    confirm.disabled = true;
    try {
      await approveProposal(proposal.proposal_id, projectId, expectedRevision);
      dialog.close();
      announce("Provider switch approved.", "success");
      await refresh();
      focusStudioMain();
    } catch (error) {
      announce(error.message, "error");
      confirm.disabled = false;
    }
  });
  const stay = button("Keep current provider", true, async () => {
    stay.disabled = true;
    try {
      await rejectProposal(proposal.proposal_id, projectId, expectedRevision);
      dialog.close();
      announce("Provider switch rejected; the current route was preserved.");
      await refresh();
      focusStudioMain();
    } catch (error) {
      announce(error.message, "error");
      stay.disabled = false;
    }
  });
  actions.append(confirm, stay);
  dialog.append(actions);
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (previousFocus?.isConnected) previousFocus.focus();
  });
  document.body.append(dialog);
  dialog.showModal();
  confirm.focus();
}

function showPromotionDialog(job, project, refresh, announce, trigger) {
  const previousFocus = trigger;
  const dialog = document.createElement("dialog");
  dialog.setAttribute("aria-labelledby", "promotion-dialog-heading");
  dialog.append(
    element("h3", "Confirm promotion", { id: "promotion-dialog-heading" }),
    element(
      "p",
      "Promote this staged raster as the current accepted project raster?",
    ),
  );
  const actions = element("div", null, { class: "actions" });
  const confirm = button("Confirm promotion", true, async () => {
    confirm.disabled = true;
    try {
      await submitStagedRaster(job.job_id, project.revision);
      dialog.close();
      announce("Staged raster promoted.", "success");
      await refresh();
      focusStudioMain();
    } catch (error) {
      announce(error.message, "error");
      confirm.disabled = false;
    }
  });
  const keepStaged = button("Keep staged raster", true, () => dialog.close());
  actions.append(confirm, keepStaged);
  dialog.append(actions);
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (previousFocus?.isConnected) previousFocus.focus();
  });
  document.body.append(dialog);
  dialog.showModal();
  confirm.focus();
}

function renderJob(job, project, refresh, announce, workflow) {
  const displayState = JOB_STATES.has(job.state) ? job.state : "unknown";
  const revisionCurrent = job.project_revision === project.revision;
  const card = element("article", null, { class: "card job-card" });
  card.append(
    element("h3", `${job.provider} / ${job.model}`),
    element("p", `Status: ${displayState}`, { class: `job-state state-${displayState}` }),
    element("p", `Subject: ${job.subject_kind || "panel"}/${job.subject_id || job.job_id}`),
    element("p", `Authentication mode: ${job.auth_mode}`),
    element(
      "p",
      `Attempt ${job.attempt || 1}; retries ${job.retry_count || 0}/${job.max_retries || 0}`,
    ),
  );
  const actions = element("div", null, {
    class: "actions", "aria-label": "Generation actions",
  });
  const run = async (operation, success) => {
    try {
      await operation();
      announce(success, "success");
      await refresh();
      focusStudioMain();
    } catch (error) {
      announce(error.message, "error");
    }
  };
  if (revisionCurrent && RETRY_STATES.has(job.state)) {
    actions.append(button("Retry", true, () => run(
      () => retryGeneration(job.job_id, project.revision), "Generation retry queued.",
    )));
  }
  if (revisionCurrent && job.can_cancel === true) {
    actions.append(button("Cancel", true, () => run(
      () => cancelGeneration(job.job_id, project.revision), "Generation cancelled.",
    )));
  }
  if (
    revisionCurrent && job.state === "validating" && job.artifact_state === "staged"
  ) {
    actions.append(button("Promote", true, (event) => {
      showPromotionDialog(job, project, refresh, announce, event.currentTarget);
    }));
  }
  if (revisionCurrent && SWITCH_STATES.has(job.state)) {
    actions.append(button("Switch provider", true, async (event) => {
      const trigger = event.currentTarget;
      try {
        const proposal = await pauseForSwitch(job.job_id, project.revision);
        showSwitchDialog(proposal, project.project_id, project.revision, refresh, announce, trigger);
      } catch (error) {
        announce(error.message, "error");
      }
    }));
  }
  if (!actions.childElementCount && !workflow) {
    actions.append(element("p", "No actions are available for this historical or terminal job."));
  }
  card.append(actions);
  return card;
}

function groupBy(items, key) {
  const map = new Map();
  for (const item of items) {
    const k = key(item);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(item);
  }
  return map;
}

export function renderGenerateView({ store, announce, navigate }) {
  const state = store.getState();
  const project = state.project;
  const section = element("section", null, { "aria-labelledby": "generate-heading" });
  section.append(element("div", null, { class: "view-heading" }));
  section.firstElementChild.append(
    element("h2", "Generate", { id: "generate-heading" }),
    element(
      "p",
      "Choose one curated route, review its disclosed basis, and confirm cost before queueing.",
    ),
  );
  const localStatus = element("p", "Loading generation state…", {
    class: "notice", role: "status", "aria-atomic": "true",
  });
  localStatus.setAttribute("aria-live", "polite");
  section.append(localStatus);

  const form = element("form", null, {
    class: "card", "aria-describedby": "cost-guidance",
  });
  const providerLabel = element("label", "Provider", { for: "generation-provider" });
  const provider = element("select", null, {
    id: "generation-provider", name: "provider",
  });
  const modelLabel = element("label", "Model", { for: "generation-model" });
  const model = element("select", null, { id: "generation-model", name: "model" });
  const authLabel = element("label", "Authentication mode", {
    for: "generation-auth-mode",
  });
  const authMode = element("select", null, {
    id: "generation-auth-mode", name: "auth_mode",
  });
  const capabilities = element("p", "Capabilities: loading", {
    id: "generation-capabilities",
  });
  const reasonHeading = element("h3", "Why this recommendation");
  const reasons = element("ul", null, { id: "generation-reasons" });
  const defaultReason = "Recommendation reasons appear after durable generation state exists.";
  reasons.append(element("li", defaultReason));
  const unknownCost = "Estimated cost is unknown. Confirm only if you accept that uncertainty.";
  const cost = element("p", unknownCost, {
    id: "cost-guidance", class: "notice warning",
  });
  const costLabel = element(
    "label",
    "I explicitly confirm generation with the displayed cost status.",
  );
  const costConfirmation = element("input", null, {
    id: "cost-confirmation", name: "cost_confirmation", type: "checkbox",
  });
  costLabel.prepend(costConfirmation);
  const submit = element("button", "Start generation", {
    type: "submit", class: "button primary",
  });
  submit.disabled = true;
  form.append(
    providerLabel, provider, modelLabel, model, authLabel, authMode, capabilities,
    reasonHeading, reasons, cost, costLabel, submit,
  );
  section.append(form);

  const queueHeading = element("h3", "Generation queue");
  const queue = element("div", null, { class: "card-grid", "aria-live": "polite" });
  const workflowControls = element("div", null, { class: "card workflow-controls" });
  workflowControls.append(element("h3", "Workflow controls"));
  const pauseBtn = element("button", "Pause workflow", { type: "button", class: "button" });
  const resumeBtn = element("button", "Resume workflow", { type: "button", class: "button" });
  pauseBtn.disabled = true;
  resumeBtn.disabled = true;
  workflowControls.append(pauseBtn, resumeBtn);
  section.append(queueHeading, queue, workflowControls, button("Open Review", true, () => navigate("review")));

  let confirmedSelection = null;
  const selectedOption = () => optionCache?.find(
    (item) => item.provider === provider.value && item.model === model.value,
  );
  const selectionFingerprint = () => {
    const selected = selectedOption();
    if (!selected || !authMode.value) return null;
    return `${selected.provider}\u0000${selected.model}\u0000${authMode.value}`;
  };
  function resetGenerationConfirmation() {
    costConfirmation.checked = false;
    confirmedSelection = null;
    submit.disabled = true;
    reasons.replaceChildren(element("li", defaultReason));
    cost.textContent = unknownCost;
  }
  const updateOptionDetails = () => {
    const selected = selectedOption();
    capabilities.textContent = `Capabilities: ${(selected?.capabilities || []).join(", ") || "none declared"}`;
    const modes = Array.isArray(selected?.auth_modes) ? selected.auth_modes : [];
    authMode.replaceChildren(...modes.map((value) => element("option", value, { value })));
  };
  const updateModels = () => {
    const available = (optionCache || []).filter((item) => item.provider === provider.value);
    model.replaceChildren(
      ...available.map((item) => element("option", item.model, { value: item.model })),
    );
    updateOptionDetails();
  };
  costConfirmation.addEventListener("change", () => {
    confirmedSelection = costConfirmation.checked ? selectionFingerprint() : null;
    submit.disabled = !confirmedSelection;
  });
  provider.addEventListener("change", resetGenerationConfirmation);
  provider.addEventListener("change", updateModels);
  model.addEventListener("change", resetGenerationConfirmation);
  model.addEventListener("change", updateOptionDetails);
  authMode.addEventListener("change", resetGenerationConfirmation);

  async function syncProjectAndJobs() {
    const current = await getProject(project.project_id);
    const result = await listGenerationJobs(current.project_id, current.revision);
    if (!section.isConnected) return current;
    store.replaceProjectAndGenerationJobs(
      current,
      Array.isArray(result.jobs) ? result.jobs : [],
      result.accepted_job ?? null,
    );
    return current;
  }

  async function refresh() {
    try {
      return await syncProjectAndJobs();
    } catch (error) {
      localStatus.textContent = error.message;
      announce(error.message, "error");
      return null;
    }
  }

  function scheduleRefresh(jobs) {
    if (!jobs.some((job) => ACTIVE_STATES.has(job.state))) return;
    setTimeout(async () => {
      if (!section.isConnected) return;
      await refresh();
    }, REFRESH_DELAY_MS);
  }

  function renderQueue(current, workflow) {
    queue.replaceChildren();
    const groups = groupBy(current.generation.jobs, (job) => `${job.subject_kind || "panel"}`);
    for (const [subject, jobs] of groups) {
      const groupCard = element("section", null, { class: "card subject-group" });
      groupCard.append(element("h4", `Subject: ${subject}`));
      for (const job of jobs) {
        groupCard.append(renderJob(job, current.project, refresh, announce, workflow));
      }
      queue.append(groupCard);
    }
    if (!current.generation.jobs.length) {
      queue.append(element("p", "No generation jobs yet."));
    }
  }

  async function syncWorkflow() {
    const proj = store.getState().project;
    if (!proj) return null;
    try {
      const res = await getWorkflow(proj.project_id);
      const workflow = res.workflow || res;
      store.setWorkflow(workflow);
      pauseBtn.disabled = !workflow.can_pause;
      resumeBtn.disabled = !workflow.can_resume;
      return workflow;
    } catch (error) {
      pauseBtn.disabled = true;
      resumeBtn.disabled = true;
      return null;
    }
  }
  pauseBtn.addEventListener("click", async () => {
    const proj = store.getState().project;
    if (!proj) return;
    pauseBtn.disabled = true;
    try {
      await pauseWorkflow(proj.project_id, proj.revision);
      announce("Workflow paused.", "success");
      await syncWorkflow();
    } catch (error) {
      announce(error.message, "error");
      pauseBtn.disabled = false;
    }
  });
  resumeBtn.addEventListener("click", async () => {
    const proj = store.getState().project;
    if (!proj) return;
    resumeBtn.disabled = true;
    try {
      await resumeWorkflow(proj.project_id, proj.revision);
      announce("Workflow resumed.", "success");
      await syncWorkflow();
    } catch (error) {
      announce(error.message, "error");
      resumeBtn.disabled = false;
    }
  });

  async function load() {
    try {
      if (!optionCache) {
        const result = await getGenerationOptions();
        optionCache = Array.isArray(result.options) ? result.options : [];
      }
      const providers = [...new Set(optionCache.map((item) => item.provider))];
      provider.replaceChildren(
        ...providers.map((value) => element("option", value, { value })),
      );
      updateModels();
      submit.disabled = !confirmedSelection;
      const current = store.getState();
      if (current.generation.loadedRevision !== current.project.revision) {
        await refresh();
        return;
      }
      renderQueue(current);
      const recommendationJob = current.generation.jobs[0];
      if (recommendationJob) {
        const result = await getGenerationRecommendations(
          current.project.project_id,
          current.project.revision,
          recommendationJob.job_id,
        );
        if (!section.isConnected) return;
        const recommendation = result.recommendations?.[0];
        if (recommendation) {
          reasons.replaceChildren(
            ...recommendation.reasons.map((value) => element("li", value)),
          );
          cost.textContent = recommendation.estimated_cost
            ? `Estimated cost: ${recommendation.estimated_cost.amount} ${recommendation.estimated_cost.currency} per ${recommendation.estimated_cost.unit}.`
            : unknownCost;
        }
      }
      localStatus.textContent = "Generation state is current.";
      scheduleRefresh(current.generation.jobs);
    } catch (error) {
      localStatus.textContent = error.message;
      announce(error.message, "error");
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fingerprint = selectionFingerprint();
    if (!costConfirmation.checked || !fingerprint || confirmedSelection !== fingerprint) {
      announce("Explicit cost confirmation is required for the current selection.", "error");
      resetGenerationConfirmation();
      costConfirmation.focus();
      return;
    }
    const selected = selectedOption();
    if (!selected) return;
    submit.disabled = true;
    try {
      await queueGeneration(project.project_id, project.revision, {
        provider: selected.provider,
        model: selected.model,
        auth_mode: authMode.value,
      });
      announce("Generation queued.", "success");
      await refresh();
      focusStudioMain();
    } catch (error) {
      announce(error.message, "error");
      submit.disabled = false;
    }
  });

  void load();
  return section;
}
