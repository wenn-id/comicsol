import { StudioApiError, StaleRevisionError, getProject, updatePlan } from "../api.js";

const FIELDS = Object.freeze([
  Object.freeze(["storyPlan", "Story plan"]),
  Object.freeze(["characterBible", "Character bible"]),
  Object.freeze(["storyboard", "Storyboard"]),
  Object.freeze(["visualIdentityPack", "Visual Identity Pack"]),
]);
let activeProposalHandler = null;

function element(tag, attributes = {}, text = "") {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") node.className = value;
    else node.setAttribute(name, value);
  }
  if (text) node.textContent = text;
  return node;
}

function labelFor(identifier, text) {
  return element("label", { for: identifier }, text);
}

export function safeProposal(detail) {
  if (!detail || typeof detail !== "object" || !Number.isInteger(detail.expectedRevision)) return null;
  const changes = detail.changes;
  if (
    !changes ||
    typeof changes !== "object" ||
    !FIELDS.every(([key]) => typeof changes[key] === "string")
  ) return null;
  return {
    expectedRevision: detail.expectedRevision,
    changes: Object.fromEntries(FIELDS.map(([key]) => [key, changes[key].slice(0, 1048576)])),
  };
}

function renderDraftDiff(container, current, draft) {
  container.replaceChildren();
  if (!draft) {
    container.append(element("p", {}, "No draft is waiting for review."));
    return;
  }
  const intro = draft.origin === "agent"
    ? "Agent-proposed changes are a draft only. Review every difference before promotion."
    : "Your edits are a draft only. Review every difference before promotion.";
  container.append(element("p", { className: "notice" }, intro));
  const list = element("div", { className: "diff-list" });
  for (const [key, title] of FIELDS) {
    if (current[key] === draft.changes[key]) continue;
    const item = element("section", { className: "diff-item", "aria-label": `${title} change` });
    item.append(
      element("h3", {}, title),
      element("p", { className: "diff-label" }, "Current"),
      element("p", {}, current[key] || "Not set"),
      element("p", { className: "diff-label" }, "Proposed"),
      element("p", {}, draft.changes[key] || "Remove content"),
    );
    list.append(item);
  }
  if (!list.childElementCount) list.append(element("p", {}, "The draft contains no changes."));
  container.append(list);
}

export async function persistReviewedDraft(store, draft, persist) {
  const persisted = await persist();
  const currentDraft = store.getState().draft;
  if (currentDraft !== draft) {
    store.replaceProject(persisted);
    if (currentDraft) store.createDraft(currentDraft.changes, currentDraft.origin);
    return Object.freeze({ outcome: "replacement-preserved", persisted });
  }
  const outcome = store.promoteDraft(persisted) ? "promoted" : "not-promoted";
  return Object.freeze({ outcome, persisted });
}

export function renderPlanView({ store, announce }) {
  const state = store.getState();
  const project = state.project;
  const view = element("section", { "aria-labelledby": "plan-heading" });
  const heading = element("div", { className: "view-heading" });
  heading.append(
    element("h2", { id: "plan-heading" }, "Plan the comic"),
    element("p", {}, "Edit a revision-bound working copy, review its diff, then promote it without silently overwriting newer project state."),
  );

  const summary = element("ul", { className: "project-summary", "aria-label": "Current project revision" });
  const facts = [
    ["Title", typeof project.summary.title === "string" ? project.summary.title : "Untitled"],
    ["Status", project.status],
    ["Revision", String(project.revision)],
  ];
  for (const [name, value] of facts) {
    const item = element("li");
    item.append(element("strong", {}, `${name}: `), document.createTextNode(value));
    summary.append(item);
  }

  const layout = element("div", { className: "card-grid" });
  const editor = element("section", { className: "card", "aria-labelledby": "editor-heading" });
  editor.append(element("h3", { id: "editor-heading" }, "Working copy"));
  const form = element("form", { id: "plan-editor" });
  const controls = {};
  for (const [key, title] of FIELDS) {
    const identifier = `plan-${key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
    const wrapper = element("div", { className: "field" });
    const control = element("textarea", {
      id: identifier,
      name: key,
      maxlength: "1048576",
      "aria-describedby": `${identifier}-help`,
    });
    control.value = state.workingPlan[key];
    controls[key] = control;
    wrapper.append(
      labelFor(identifier, title),
      control,
      element("p", { className: "field-help", id: `${identifier}-help` }, "Changes remain in memory until reviewed and promoted to the working copy."),
    );
    form.append(wrapper);
  }
  const editActions = element("div", { className: "actions" });
  editActions.append(
    element("button", { className: "button primary", type: "submit" }, "Review changes"),
    element("button", { className: "button", type: "button", id: "refresh-project" }, "Refresh revision"),
  );
  form.append(editActions);
  editor.append(form);

  const review = element("section", {
    className: "card",
    "aria-labelledby": "review-heading",
    tabindex: "-1",
  });
  review.append(element("h3", { id: "review-heading" }, "Draft review"));
  const diff = element("div", { id: "draft-diff" });
  const reviewActions = element("div", { className: "actions" });
  const promote = element("button", { className: "button primary", type: "button" }, "Promote to working copy");
  const discard = element("button", { className: "button", type: "button" }, "Discard draft");
  reviewActions.append(promote, discard);
  review.append(diff, reviewActions);
  layout.append(editor, review);
  view.append(heading, summary, layout);

  let promotionPending = false;

  function updateDraft() {
    const latest = store.getState();
    renderDraftDiff(diff, latest.workingPlan, latest.draft);
    for (const control of form.elements) control.disabled = promotionPending;
    promote.disabled = promotionPending || !latest.draft;
    discard.disabled = promotionPending || !latest.draft;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (promotionPending) return;
    store.createDraft(Object.fromEntries(FIELDS.map(([key]) => [key, controls[key].value])));
    updateDraft();
    announce("Draft created. Review the differences before promotion.");
    review.focus?.();
  });

  async function refreshProject(force = false) {
    if (promotionPending && !force) return;
    announce("Refreshing project revision…");
    try {
      const refreshed = await getProject(project.project_id);
      const previousRevision = store.getState().project.revision;
      store.replaceProject(refreshed);
      if (refreshed.revision !== previousRevision) {
        store.clearDraft();
        for (const [key] of FIELDS) controls[key].value = store.getState().workingPlan[key];
        updateDraft();
        announce("Revision changed. The stale draft was discarded; review the refreshed project.", "error");
      } else {
        announce("Project revision is current.", "success");
      }
    } catch (error) {
      announce(
        error instanceof StudioApiError ? error.message : "The project could not be refreshed safely.",
        "error",
      );
    }
  }

  form.querySelector("#refresh-project").addEventListener("click", () => refreshProject());
  promote.addEventListener("click", async () => {
    if (promotionPending) return;
    const latest = store.getState();
    const draft = latest.draft;
    if (!draft) return;
    if (latest.project.revision !== draft.expectedRevision) {
      store.clearDraft();
      updateDraft();
      announce("Revision changed. The stale draft was discarded; refresh before editing.", "error");
      return;
    }
    promotionPending = true;
    updateDraft();
    announce("Saving the reviewed Plan to the canonical project…");
    try {
      const result = await persistReviewedDraft(store, draft, () => updatePlan(
        latest.project.project_id,
        draft.changes,
        draft.expectedRevision,
      ));
      if (result.outcome === "replacement-preserved") {
        updateDraft();
        announce("The saved Plan was refreshed; newer edits remain a draft for review.", "error");
        return;
      }
      if (result.outcome === "promoted") {
        for (const [key] of FIELDS) controls[key].value = store.getState().workingPlan[key];
        updateDraft();
        announce("Draft committed and promoted to the revision-bound working copy.", "success");
      }
    } catch (error) {
      if (error instanceof StaleRevisionError) {
        await refreshProject(true);
      } else {
        announce(
          error instanceof StudioApiError
            ? error.message
            : "The reviewed Plan could not be saved safely.",
          "error",
        );
      }
    } finally {
      promotionPending = false;
      updateDraft();
    }
  });
  discard.addEventListener("click", () => {
    if (promotionPending) return;
    store.clearDraft();
    updateDraft();
    announce("Draft discarded.");
  });

  const proposalHandler = (event) => {
    const proposal = safeProposal(event.detail);
    if (!proposal) return;
    if (proposal.expectedRevision !== store.getState().project.revision) {
      announce("Revision changed. The agent proposal is stale and was not opened.", "error");
      return;
    }
    if (store.getState().draft) {
      announce("A draft is already waiting for review. Discard or promote it first.", "error");
      return;
    }
    store.createDraft(proposal.changes, "agent");
    updateDraft();
    announce("Agent-proposed changes are ready for review.");
  };
  if (!activeProposalHandler) {
    activeProposalHandler = proposalHandler;
    document.addEventListener("comic-sol:plan-proposal", activeProposalHandler);
  }
  updateDraft();
  return view;
}
