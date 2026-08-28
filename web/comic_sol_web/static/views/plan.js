import { StudioApiError, getProject } from "../api.js";

const FIELDS = Object.freeze([
  Object.freeze(["storyPlan", "Story plan"]),
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

function safeProposal(detail) {
  if (!detail || typeof detail !== "object" || !Number.isInteger(detail.expectedRevision)) return null;
  const changes = detail.changes;
  if (!changes || typeof changes !== "object") return null;
  return {
    expectedRevision: detail.expectedRevision,
    changes: Object.fromEntries(
      FIELDS.map(([key]) => [key, typeof changes[key] === "string" ? changes[key].slice(0, 1048576) : ""]),
    ),
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

  function updateDraft() {
    const latest = store.getState();
    renderDraftDiff(diff, latest.workingPlan, latest.draft);
    promote.disabled = !latest.draft;
    discard.disabled = !latest.draft;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    store.createDraft(Object.fromEntries(FIELDS.map(([key]) => [key, controls[key].value])));
    updateDraft();
    announce("Draft created. Review the differences before promotion.");
    review.focus?.();
  });

  async function refreshProject() {
    announce("Refreshing project revision…");
    try {
      const refreshed = await getProject(project.project_id);
      const previousRevision = store.getState().project.revision;
      store.replaceProject(refreshed);
      if (refreshed.revision !== previousRevision) {
        store.clearDraft();
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

  form.querySelector("#refresh-project").addEventListener("click", refreshProject);
  promote.addEventListener("click", async () => {
    const latest = store.getState();
    const draft = latest.draft;
    if (!draft) return;
    if (project.revision !== draft.expectedRevision) {
      store.clearDraft();
      updateDraft();
      announce("Revision changed. The stale draft was discarded; refresh before editing.", "error");
      return;
    }
    if (store.promoteDraft()) {
      for (const [key] of FIELDS) controls[key].value = store.getState().workingPlan[key];
      updateDraft();
      announce("Draft promoted to the revision-bound working copy.", "success");
    }
  });
  discard.addEventListener("click", () => {
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
    store.createDraft(proposal.changes, "agent");
    updateDraft();
    announce("Agent-proposed changes are ready for review.");
  };
  if (activeProposalHandler) {
    document.removeEventListener("comic-sol:plan-proposal", activeProposalHandler);
  }
  activeProposalHandler = proposalHandler;
  document.addEventListener("comic-sol:plan-proposal", proposalHandler, { once: true });
  updateDraft();
  return view;
}
