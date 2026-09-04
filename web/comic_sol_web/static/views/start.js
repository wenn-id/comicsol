import {
  MAX_ARCHIVE_BYTES,
  MigrationValidationError,
  StudioApiError,
  createProject,
  getPlanningOptions,
  importProject,
  queuePlanning,
} from "../api.js";

const MAX_SOURCE_BYTES = 200 * 1024;

export function retryOperation(previous, identity) {
  if (previous?.identity === identity) return previous;
  return Object.freeze({ identity, idempotencyKey: crypto.randomUUID() });
}

function element(tag, attributes = {}, text = "") {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") node.className = value;
    else if (typeof value === "boolean") node.toggleAttribute(name, value);
    else node.setAttribute(name, value);
  }
  if (text) node.textContent = text;
  return node;
}

function labelFor(identifier, text) {
  return element("label", { for: identifier }, text);
}

function field(identifier, label, control, help = "") {
  const wrapper = element("div", { className: "field" });
  wrapper.append(labelFor(identifier, label), control);
  if (help) wrapper.append(element("p", { className: "field-help", id: `${identifier}-help` }, help));
  return wrapper;
}

function setBusy(form, busy) {
  for (const control of form.elements) control.disabled = busy;
  form.setAttribute("aria-busy", String(busy));
}

function safeFailure(error, announce) {
  if (error instanceof MigrationValidationError || error instanceof StudioApiError) {
    announce(error.message, "error");
  } else {
    announce("The project request could not be completed safely.", "error");
  }
}

function creationCard({ store, announce, navigate }) {
  const card = element("section", {
    className: "card start-create-card",
    "aria-labelledby": "create-heading",
  });
  card.append(element("h3", { id: "create-heading" }, "Create a project"));
  const form = element("form", { id: "create-project-form", className: "start-create-form" });
  let retry = null;

  const title = element("input", {
    id: "project-title",
    name: "title",
    type: "text",
    required: true,
    maxlength: "160",
    autocomplete: "off",
  });
  const source = element("textarea", {
    id: "project-source",
    name: "source",
    required: true,
    maxlength: "204800",
    "aria-describedby": "project-source-help",
  });
  const language = element("input", {
    id: "project-language",
    name: "language",
    type: "text",
    value: "en",
    required: true,
    maxlength: "16",
  });
  const pageCount = element("input", {
    id: "project-page-count",
    name: "page_count",
    type: "number",
    value: "2",
    min: "1",
    max: "4",
    required: true,
  });

  const planningProvider = element("select", {
    id: "project-planning-provider",
    name: "planningProvider",
  });
  const planningModel = element("select", {
    id: "project-planning-model",
    name: "planningModel",
  });
  const imageModel = element("select", {
    id: "project-image-model",
    name: "imageModel",
  });
  imageModel.append(
    element("option", { value: "dall-e-3" }, "DALL-E 3"),
    element("option", { value: "gpt-image-1" }, "GPT Image 1"),
  );

  let planningOptionsCache = [];
  async function loadPlanningSelections() {
    try {
      const res = await getPlanningOptions();
      planningOptionsCache = res.options || [];
      updatePlanningSelections();
    } catch { /* optional local session */ }
  }
  function updatePlanningSelections() {
    planningProvider.replaceChildren();
    planningModel.replaceChildren();
    const providers = [...new Set(planningOptionsCache.map((opt) => opt.provider))];
    for (const p of providers) {
      planningProvider.append(element("option", { value: p }, p));
    }
    const currentProvider = planningProvider.value;
    const models = planningOptionsCache.filter((opt) => opt.provider === currentProvider);
    for (const m of models) {
      const opt = element("option", { value: m.model }, m.model);
      if (!m.enabled && m.required_environment_variable) {
        opt.disabled = true;
        opt.textContent = `${m.model} (missing ${m.required_environment_variable})`;
      }
      planningModel.append(opt);
    }
  }
  planningProvider.addEventListener("change", updatePlanningSelections);
  void loadPlanningSelections();

  const mode = element("fieldset");
  mode.append(element("legend", {}, "Source format"));
  const choices = element("div", { className: "choice-row" });
  for (const [id, value, text, checked] of [
    ["source-mode-prompt", "short_prompt", "Short prompt", true],
    ["source-mode-story", "pasted_story", "Full story", false],
  ]) {
    const input = element("input", {
      id,
      name: "source_mode",
      type: "radio",
      value,
      checked,
    });
    const choice = labelFor(id, text);
    choice.prepend(input);
    choices.append(choice);
  }
  mode.append(choices);

  const projectMetaFields = element("div", { className: "compact-fields" });
  projectMetaFields.append(
    field("project-language", "Language code", language),
    field("project-page-count", "Page count", pageCount),
  );
  const planningFields = element("div", { className: "compact-fields" });
  planningFields.append(
    field("project-planning-provider", "Planning provider", planningProvider),
    field("project-planning-model", "Planning model", planningModel),
  );

  form.append(
    field("project-title", "Project title", title),
    mode,
    field(
      "project-source",
      "Prompt or story",
      source,
      "At most 200 KiB of UTF-8. Content is sent only to the project API.",
    ),
    projectMetaFields,
    planningFields,
    field("project-image-model", "Image model", imageModel),
  );
  const actions = element("div", { className: "actions" });
  actions.append(element("button", { className: "button primary", type: "submit" }, "Create project"));
  form.append(actions);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const sourceValue = source.value.trim();
    if (new TextEncoder().encode(sourceValue).byteLength > MAX_SOURCE_BYTES) {
      announce("The prompt or story must be at most 200 KiB of UTF-8.", "error");
      source.focus();
      return;
    }
    setBusy(form, true);
    announce("Creating project…");
    const request = {
      title: title.value.trim(),
      prompt: sourceValue,
      language: language.value.trim(),
      mode: form.elements.source_mode.value,
      page_count: Number(pageCount.value),
    };
    try {
      const fingerprint = JSON.stringify(request);
      retry = retryOperation(retry, fingerprint);
      const project = await createProject(request, retry.idempotencyKey);
      store.setProject(project);
      let planningStatus = "Planning was skipped because no planning provider is available.";
      if (planningProvider.value && planningModel.value) {
        try {
          const planJob = await queuePlanning(project.project_id, project.revision, {
            provider: planningProvider.value,
            model: planningModel.value,
          });
          store.setPlanningJob(planJob.job || planJob);
          planningStatus = "Planning queued. The Plan will be ready for review when processing completes.";
        } catch {
          planningStatus = "Project created, but planning could not be queued. Retry planning before review.";
        }
      }
      announce(`Project created. ${planningStatus}`, "success");
      navigate("plan");
    } catch (error) {
      safeFailure(error, announce);
    } finally {
      setBusy(form, false);
    }
  });
  card.append(form);
  return card;
}

function importCard({ store, announce, navigate }) {
  const card = element("section", { className: "card", "aria-labelledby": "import-heading" });
  card.append(
    element("h3", { id: "import-heading" }, "Import an archive"),
    element("p", {}, "Open a portable .comic-sol-handoff archive. Validation and migration remain server-authoritative."),
  );
  const form = element("form", { id: "import-project-form" });
  let retry = null;
  const archive = element("input", {
    id: "project-archive",
    name: "archive",
    type: "file",
    accept: ".comic-sol-handoff",
    required: true,
    "aria-describedby": "project-archive-help",
  });
  form.append(
    field(
      "project-archive",
      "Comic Sol archive",
      archive,
      "Only one bounded portable archive is accepted. The original file is never modified.",
    ),
  );
  const actions = element("div", { className: "actions" });
  actions.append(element("button", { className: "button primary", type: "submit" }, "Validate and import"));
  form.append(actions);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const file = archive.files[0];
    if (!file || !file.name.endsWith(".comic-sol-handoff")) {
      announce("Choose a .comic-sol-handoff archive.", "error");
      archive.focus();
      return;
    }
    if (file.size > MAX_ARCHIVE_BYTES) {
      announce("The archive is larger than the accepted import bound.", "error");
      archive.focus();
      return;
    }
    setBusy(form, true);
    announce("Validating and importing archive…");
    try {
      retry = retryOperation(retry, file);
      const project = await importProject(file, retry.idempotencyKey);
      store.setProject(project);
      announce("Archive imported. Review the current plan before continuing.", "success");
      navigate("plan");
    } catch (error) {
      safeFailure(error, announce);
    } finally {
      setBusy(form, false);
    }
  });
  card.append(form);
  return card;
}

export function renderStartView(context) {
  const view = element("section", {
    className: "start-view",
    "aria-labelledby": "start-heading",
  });
  const heading = element("div", { className: "view-heading" });
  heading.append(
    element("h2", { id: "start-heading" }, "Start a comic"),
    element("p", {}, "Create from a prompt or story, or safely import an existing portable project."),
  );
  const grid = element("div", { className: "card-grid" });
  grid.append(creationCard(context), importCard(context));
  view.append(heading, grid);
  return view;
}
