import {
  MAX_ARCHIVE_BYTES,
  MigrationValidationError,
  StudioApiError,
  createProject,
  importProject,
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
  const card = element("section", { className: "card", "aria-labelledby": "create-heading" });
  card.append(element("h3", { id: "create-heading" }, "Create a project"));
  const form = element("form", { id: "create-project-form" });
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

  form.append(
    field("project-title", "Project title", title),
    mode,
    field(
      "project-source",
      "Prompt or story",
      source,
      "At most 200 KiB of UTF-8. Content is sent only to the project API.",
    ),
    field("project-language", "Language code", language),
    field("project-page-count", "Page count", pageCount),
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
      announce("Project created. Plan is ready for review.", "success");
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
  const view = element("section", { "aria-labelledby": "start-heading" });
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
