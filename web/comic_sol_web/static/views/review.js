import {
  acceptedRasterUrl,
  exportProject,
  getProject,
  listGenerationJobs,
  retryGeneration,
  runQa,
  submitStagedRaster,
} from "../api.js";

const ACTIVE_STATES = new Set([
  "queued", "running", "polling", "awaiting_provider_confirmation",
]);
const REFRESH_DELAY_MS = 2000;
let exportObjectUrl = null;
let pendingReviewExport = null;
let reviewSessionEpoch = 0;
let reviewViewEpoch = 0;
let activeExportRequestHandler = null;

function element(tag, text, attributes = {}) {
  const node = document.createElement(tag);
  if (text !== null) node.textContent = text;
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function button(text, action, attributes = {}) {
  const control = element("button", text, {
    type: "button", class: "button", ...attributes,
  });
  control.addEventListener("click", action);
  return control;
}

function focusStudioMain() {
  document.getElementById("studio-main")?.focus();
}

function revokeExportObjectUrl() {
  if (!exportObjectUrl) return;
  URL.revokeObjectURL(exportObjectUrl);
  exportObjectUrl = null;
}

function reviewViewIsCurrent(viewEpoch, section) {
  return viewEpoch === reviewViewEpoch && section.isConnected;
}

function reviewSessionIsCurrent(sessionEpoch) {
  return sessionEpoch === reviewSessionEpoch;
}

export function disposeReviewView({ preservePendingExport = false } = {}) {
  reviewViewEpoch += 1;
  if (activeExportRequestHandler && typeof document.removeEventListener === "function") {
    document.removeEventListener("comic-sol:export-request", activeExportRequestHandler);
    activeExportRequestHandler = null;
  }
  revokeExportObjectUrl();
  if (!preservePendingExport) {
    reviewSessionEpoch += 1;
    pendingReviewExport = null;
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => disposeReviewView());
}

function attachExportDownload(exportCard, result, format, announce) {
  revokeExportObjectUrl();
  exportObjectUrl = URL.createObjectURL(result.blob);
  const download = element("a", "Download private export", {
    href: exportObjectUrl,
    download: format === "pdf"
      ? "comic-sol-export.pdf"
      : "comic-sol-export.comic-sol-handoff",
  });
  download.addEventListener("click", () => {
    setTimeout(revokeExportObjectUrl, 0);
  });
  exportCard.querySelector("a[download]")?.remove();
  exportCard.append(download);
  announce("Private export is ready for download.", "success");
  return download;
}

function consumePendingReviewExport(exportCard, announce) {
  if (!pendingReviewExport) return null;
  const completed = pendingReviewExport;
  pendingReviewExport = null;
  return attachExportDownload(exportCard, completed.result, completed.format, announce);
}

function confirmationDialog({ heading, message, confirmText, trigger, onConfirm }) {
  const previousFocus = trigger;
  const dialog = document.createElement("dialog");
  dialog.setAttribute("aria-labelledby", "review-confirm-heading");
  dialog.append(
    element("h3", heading, { id: "review-confirm-heading" }),
    element("p", message),
  );
  const actions = element("div", null, { class: "actions" });
  const confirm = button(confirmText, async () => {
    confirm.disabled = true;
    await onConfirm(dialog, confirm);
  }, { class: "button primary" });
  const cancel = button("Cancel", () => dialog.close());
  actions.append(confirm, cancel);
  dialog.append(actions);
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (previousFocus?.isConnected) previousFocus.focus();
  });
  document.body.append(dialog);
  dialog.showModal();
  confirm.focus();
}

export function renderReviewView({ store, announce, navigate }) {
  const viewEpoch = ++reviewViewEpoch;
  const exportSessionEpoch = reviewSessionEpoch;
  const state = store.getState();
  const project = state.project;
  const generation = state.generation;
  const section = element("section", null, { "aria-labelledby": "review-heading" });
  const heading = element("div", null, { class: "view-heading" });
  heading.append(
    element("h2", "Review", { id: "review-heading" }),
    element(
      "p",
      "Review durable staged and accepted results without changing raster bytes in the browser.",
    ),
    element("p", `Project revision: ${project.revision}`, { class: "revision-label" }),
  );
  section.append(heading);
  const live = element("p", "Loading Review state…", {
    class: "notice", role: "status", "aria-live": "polite", "aria-atomic": "true",
  });
  section.append(live);

  async function syncProjectAndJobs(
    exportResult = null,
    exportFormat = null,
    requiredSessionEpoch = null,
  ) {
    const current = await getProject(project.project_id);
    const result = await listGenerationJobs(current.project_id, current.revision);
    const exportRefresh = requiredSessionEpoch !== null;
    if (exportRefresh) {
      if (!reviewSessionIsCurrent(requiredSessionEpoch)) return null;
    } else if (!reviewViewIsCurrent(viewEpoch, section)) {
      return null;
    }
    revokeExportObjectUrl();
    if (exportResult && exportFormat) {
      pendingReviewExport = Object.freeze({ result: exportResult, format: exportFormat });
    }
    store.replaceProjectAndGenerationJobs(
      current,
      Array.isArray(result.jobs) ? result.jobs : [],
      result.accepted_job ?? null,
    );
    return current;
  }

  async function refresh(
    exportResult = null,
    exportFormat = null,
    requiredSessionEpoch = null,
  ) {
    try {
      return await syncProjectAndJobs(
        exportResult,
        exportFormat,
        requiredSessionEpoch,
      );
    } catch (error) {
      live.textContent = `Review error: ${error.message}`;
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

  const artifacts = element("div", null, {
    class: "card-grid", "aria-label": "Raster review",
  });
  const acceptedCard = element("article", null, { class: "card accepted-raster" });
  acceptedCard.append(element("h3", "Accepted raster"));
  if (generation.accepted?.artifact_job_id) {
    const image = element("img", null, {
      src: acceptedRasterUrl(
        project.project_id,
        project.revision,
        generation.accepted.artifact_job_id,
      ),
      alt: "Last accepted raster for the current canonical panel",
      class: "raster-preview",
    });
    acceptedCard.append(
      element("p", "Last accepted raster remains visible until a later promotion succeeds."),
      image,
    );
  } else {
    acceptedCard.append(element("p", "No accepted raster is available yet."));
  }

  const stagedCard = element("article", null, { class: "card staged-raster" });
  stagedCard.append(element("h3", "Staged raster"));
  if (generation.staged) {
    stagedCard.append(
      element("p", "A provider result is staged and has not replaced the accepted raster."),
    );
    const stagedIsCurrent = generation.staged.project_revision === project.revision;
    if (!stagedIsCurrent) {
      stagedCard.append(
        element("p", "This staged result belongs to an earlier project revision."),
      );
    }
    const promote = button("Confirm promotion", (event) => {
      confirmationDialog({
        heading: "Confirm promotion",
        message: "Promote this staged raster through the canonical server boundary?",
        confirmText: "Promote staged raster",
        trigger: event.currentTarget,
        onConfirm: async (dialog, control) => {
          try {
            await submitStagedRaster(generation.staged.job_id, project.revision);
            dialog.close();
            announce("Staged raster promoted.", "success");
            await refresh();
            focusStudioMain();
          } catch (error) {
            control.disabled = false;
            announce(error.message, "error");
          }
        },
      });
    });
    if (stagedIsCurrent) stagedCard.append(promote);
  } else {
    stagedCard.append(element("p", "No staged raster is awaiting promotion."));
  }
  artifacts.append(acceptedCard, stagedCard);
  section.append(artifacts);

  const terminal = element("section", null, {
    class: "card", "aria-labelledby": "repair-heading",
  });
  terminal.append(element("h3", "Repair or rerender", { id: "repair-heading" }));
  const failed = generation.jobs.filter((job) => job.state === "failed");
  const actionableFailed = failed.filter(
    (job) => job.project_revision === project.revision,
  );
  if (!failed.length) {
    terminal.append(
      element("p", "No failed result currently needs an existing retry operation."),
    );
  } else if (!actionableFailed.length) {
    terminal.append(
      element("p", "Failed historical results are read-only at this revision."),
    );
  }
  for (const job of actionableFailed) {
    terminal.append(button(`Retry ${job.provider} / ${job.model}`, async () => {
      try {
        await retryGeneration(job.job_id, project.revision);
        announce("Repair or rerender retry queued.", "success");
        await refresh();
        focusStudioMain();
      } catch (error) {
        announce(error.message, "error");
      }
    }));
  }
  section.append(terminal);

  const qaCard = element("section", null, {
    class: "card", "aria-labelledby": "qa-heading",
  });
  qaCard.append(element("h3", "QA findings", { id: "qa-heading" }));
  const qaResults = element("div", null, { "aria-live": "polite" });
  const qa = generation.qa || project.summary.qa;
  if (qa) {
    qaResults.append(element("p", qa.valid ? "QA passed." : "QA reported findings."));
    const issues = element("ul", null);
    for (const issue of qa.issues || []) {
      issues.append(element("li", `${issue.path}: ${issue.message}`));
    }
    qaResults.append(issues);
  } else {
    qaResults.append(element("p", "QA has not been run for this revision."));
  }
  qaCard.append(qaResults, button("Run QA", async (event) => {
    const control = event.currentTarget;
    control.disabled = true;
    try {
      const checked = await runQa(project.project_id, project.revision);
      store.setQa(checked);
      announce("QA completed.", "success");
      focusStudioMain();
    } catch (error) {
      control.disabled = false;
      announce(error.message, "error");
    }
  }));
  section.append(qaCard);

  const exportCard = element("section", null, {
    class: "card", "aria-labelledby": "export-heading",
    "aria-describedby": "export-guidance",
  });
  exportCard.append(element("h3", "Export", { id: "export-heading" }));
  const formatLabel = element("label", "Supported format", { for: "export-format" });
  const format = element("select", null, { id: "export-format", name: "format" });
  format.append(
    element("option", "Portable archive", { value: "archive" }),
    element("option", "PDF", { value: "pdf" }),
  );
  const guidance = element(
    "p",
    "Confirm overwrite explicitly before creating a private export.",
    { id: "export-guidance", class: "field-help" },
  );
  const overwriteLabel = element("label", "Confirm overwrite");
  const overwrite = element("input", null, {
    type: "checkbox", name: "overwrite_confirmation",
  });
  overwriteLabel.prepend(overwrite);
  const exportButton = button("Export selected format", (event) => {
    if (!overwrite.checked) {
      announce("Explicit overwrite confirmation is required.", "error");
      overwrite.focus();
      return;
    }
    confirmationDialog({
      heading: "Confirm overwrite",
      message: `Create a private ${format.value} export for revision ${project.revision}?`,
      confirmText: "Create export",
      trigger: event.currentTarget,
      onConfirm: async (dialog, control) => {
        try {
          const result = await exportProject(
            project.project_id,
            project.revision,
            format.value,
            overwrite.checked,
          );
          if (!reviewSessionIsCurrent(exportSessionEpoch)) {
            dialog.close();
            return;
          }
          dialog.close();
          const revisionChanged = result.revision !== project.revision;
          if (revisionChanged) {
            const refreshed = await refresh(
              result,
              format.value,
              exportSessionEpoch,
            );
            if (refreshed || !reviewSessionIsCurrent(exportSessionEpoch)) return;
          }
          if (!reviewSessionIsCurrent(exportSessionEpoch)) return;
          const currentExportCard = reviewViewIsCurrent(viewEpoch, section)
            ? exportCard
            : document.getElementById("export-heading")?.closest(".card");
          if (!currentExportCard) return;
          const download = attachExportDownload(
            currentExportCard,
            result,
            format.value,
            announce,
          );
          if (revisionChanged || currentExportCard !== exportCard) focusStudioMain();
          else download.focus();
        } catch (error) {
          if (!reviewSessionIsCurrent(exportSessionEpoch)) {
            dialog.close();
            return;
          }
          control.disabled = false;
          announce(error.message, "error");
        }
      },
    });
  });

  const exportRequestHandler = (event) => {
    if (!reviewViewIsCurrent(viewEpoch, section)) {
      event.preventDefault();
      return;
    }
    const requestedFormat = event.detail?.format;
    if (!["archive", "pdf"].includes(requestedFormat)) {
      event.preventDefault();
      return;
    }
    format.value = requestedFormat;
    overwrite.checked = false;
    event.detail.accepted = true;
    announce("Review the export settings and confirm the private download.");
    exportButton.focus();
  };
  if (activeExportRequestHandler && typeof document.removeEventListener === "function") {
    document.removeEventListener("comic-sol:export-request", activeExportRequestHandler);
  }
  if (typeof document.addEventListener === "function") {
    activeExportRequestHandler = exportRequestHandler;
    document.addEventListener("comic-sol:export-request", exportRequestHandler);
  }
  exportCard.append(formatLabel, format, guidance, overwriteLabel, exportButton);
  const transferredDownload = consumePendingReviewExport(exportCard, announce);
  if (transferredDownload) {
    setTimeout(() => {
      if (transferredDownload.isConnected) focusStudioMain();
    }, 0);
  }
  section.append(
    exportCard,
    button("Back to Generate", () => {
      revokeExportObjectUrl();
      navigate("generate");
    }),
  );

  async function load() {
    try {
      const current = store.getState();
      if (current.generation.loadedRevision !== current.project.revision) {
        await refresh();
        return;
      }
      const states = current.generation.jobs.map((job) => job.state);
      live.textContent = states.length
        ? `Review state: ${states.join(", ")}. Accepted results remain separate from staged, paused, failed, and cancelled results.`
        : "Review is empty; no durable generation jobs exist for this project.";
      scheduleRefresh(current.generation.jobs);
    } catch (error) {
      live.textContent = `Review error: ${error.message}`;
      announce(error.message, "error");
    }
  }
  void load();
  return section;
}
