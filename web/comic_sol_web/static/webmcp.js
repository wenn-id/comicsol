import {
  StudioApiError,
  StudioConflictError,
  StaleRevisionError,
  MigrationValidationError,
  MAX_ARCHIVE_BYTES,
  MAX_SOURCE_BYTES,
  approveProposal,
  createProject,
  exportProject,
  getCurrentProject,
  getGenerationOptions,
  getGenerationRecommendations,
  importProject,
  listGenerationJobs,
  queueGeneration,
  rejectProposal,
  runQa,
  submitGeneratedAsset,
} from "./api.js";

const PROJECT_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$";
const JOB_ID_PATTERN = "^[0-9a-f]{64}$";
const ASSET_ID_PATTERN = "^[A-Za-z0-9_-]{32,64}$";
const PROPOSAL_ID_PATTERN = "^[A-Za-z0-9_-]{32,128}$";
const UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$";
const PROJECT_ID = Object.freeze({ type: "string", pattern: PROJECT_ID_PATTERN });
const REVISION = Object.freeze({ type: "integer", minimum: 1 });
const ZERO_REVISION = Object.freeze({ type: "integer", const: 0 });
const IDEMPOTENCY_KEY = Object.freeze({ type: "string", pattern: UUID_PATTERN });
const JOB_ID = Object.freeze({ type: "string", pattern: JOB_ID_PATTERN });
const ASSET_ID = Object.freeze({ type: "string", pattern: ASSET_ID_PATTERN });
const PROPOSAL_ID = Object.freeze({ type: "string", pattern: PROPOSAL_ID_PATTERN });

function objectSchema(properties, required) {
  return Object.freeze({
    type: "object",
    properties: Object.freeze(properties),
    required: Object.freeze(required),
    additionalProperties: false,
  });
}

const EMPTY_SCHEMA = objectSchema({}, []);
const PLAN_SCHEMA = objectSchema(
  {
    storyPlan: { type: "string", maxLength: 1048576 },
    characterBible: { type: "string", maxLength: 1048576 },
    storyboard: { type: "string", maxLength: 1048576 },
    visualIdentityPack: { type: "string", maxLength: 1048576 },
  },
  ["storyPlan", "characterBible", "storyboard", "visualIdentityPack"],
);
const PROJECT_REVISION_SCHEMA = objectSchema(
  { project_id: PROJECT_ID, expected_revision: REVISION },
  ["project_id", "expected_revision"],
);

const READ_ANNOTATIONS = Object.freeze({ readOnlyHint: true });
const WRITE_ANNOTATIONS = Object.freeze({ readOnlyHint: false });

class WebMcpInputError extends Error {}

function schemaForDecision() {
  return objectSchema(
    {
      proposal_id: PROPOSAL_ID,
      expected_revision: REVISION,
      confirm_switch: { type: "boolean", const: true },
      idempotency_key: IDEMPOTENCY_KEY,
    },
    ["proposal_id", "expected_revision", "confirm_switch", "idempotency_key"],
  );
}

function schemaForProjectWrite() {
  return objectSchema(
    {
      project_id: PROJECT_ID,
      expected_revision: REVISION,
      idempotency_key: IDEMPOTENCY_KEY,
    },
    ["project_id", "expected_revision", "idempotency_key"],
  );
}

export const TOOL_DEFINITIONS = Object.freeze([
  {
    name: "get_project_state",
    description: "Read the active Studio project state without returning private project content.",
    inputSchema: EMPTY_SCHEMA,
    annotations: READ_ANNOTATIONS,
    execute: safeExecute(getProjectState),
  },
  {
    name: "list_generation_options",
    description: "List the curated generation options currently available to this Studio session.",
    inputSchema: EMPTY_SCHEMA,
    annotations: READ_ANNOTATIONS,
    execute: safeExecute(listOptions),
  },
  {
    name: "recommend_provider",
    description: "Read the server recommendation for one current project generation job.",
    inputSchema: objectSchema(
      { project_id: PROJECT_ID, expected_revision: REVISION, job_id: JOB_ID },
      ["project_id", "expected_revision", "job_id"],
    ),
    annotations: READ_ANNOTATIONS,
    execute: safeExecute(recommendProvider),
  },
  {
    name: "list_generation_jobs",
    description: "List bounded, owner-authorized generation job state for the active revision.",
    inputSchema: PROJECT_REVISION_SCHEMA,
    annotations: READ_ANNOTATIONS,
    execute: safeExecute(listJobs),
  },
  {
    name: "get_qa_summary",
    description: "Read the bounded QA summary for the active Studio project.",
    inputSchema: EMPTY_SCHEMA,
    annotations: READ_ANNOTATIONS,
    execute: safeExecute(getQaSummary),
  },
  {
    name: "create_project",
    description: "Create a project through the authenticated Studio project API.",
    inputSchema: objectSchema(
      {
        title: { type: "string", minLength: 1, maxLength: 160 },
        prompt: { type: "string", minLength: 1, maxLength: 204800 },
        language: { type: "string", minLength: 1, maxLength: 16 },
        mode: { type: "string", enum: ["short_prompt", "pasted_story"] },
        page_count: { type: "integer", minimum: 1, maximum: 4 },
        expected_revision: ZERO_REVISION,
        idempotency_key: IDEMPOTENCY_KEY,
      },
      [
        "title",
        "prompt",
        "language",
        "mode",
        "page_count",
        "expected_revision",
        "idempotency_key",
      ],
    ),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(createStudioProject),
  },
  {
    name: "import_project",
    description: "Import only the archive currently selected by the Studio page.",
    inputSchema: objectSchema(
      {
        archive_handle: { type: "string", enum: ["selected"] },
        expected_revision: ZERO_REVISION,
        idempotency_key: IDEMPOTENCY_KEY,
      },
      ["archive_handle", "expected_revision", "idempotency_key"],
    ),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(importStudioProject),
  },
  {
    name: "update_project_plan",
    description: "Submit a revision-bound Studio plan for human review before promotion.",
    inputSchema: objectSchema(
      {
        project_id: PROJECT_ID,
        expected_revision: REVISION,
        plan: PLAN_SCHEMA,
        confirm_plan: { type: "boolean", const: true },
        idempotency_key: IDEMPOTENCY_KEY,
      },
      ["project_id", "expected_revision", "plan", "confirm_plan", "idempotency_key"],
    ),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(updateStudioPlan),
  },
  {
    name: "queue_generation",
    description: "Queue one curated generation selection after explicit cost confirmation.",
    inputSchema: objectSchema(
      {
        project_id: PROJECT_ID,
        expected_revision: REVISION,
        provider: { type: "string", minLength: 1, maxLength: 64 },
        model: { type: "string", minLength: 1, maxLength: 128 },
        auth_mode: { type: "string", enum: ["agent", "hosted", "byok"] },
        confirm_cost: { type: "boolean", const: true },
        idempotency_key: IDEMPOTENCY_KEY,
      },
      [
        "project_id",
        "expected_revision",
        "provider",
        "model",
        "auth_mode",
        "confirm_cost",
        "idempotency_key",
      ],
    ),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(queueStudioGeneration),
  },
  {
    name: "submit_generated_asset",
    description: "Submit one owner-bound opaque page asset for the matching prepared agent job.",
    inputSchema: objectSchema(
      {
        project_id: PROJECT_ID,
        job_id: JOB_ID,
        asset_id: ASSET_ID,
        expected_revision: REVISION,
        confirm_promotion: { type: "boolean", const: true },
        idempotency_key: IDEMPOTENCY_KEY,
      },
      [
        "project_id",
        "job_id",
        "asset_id",
        "expected_revision",
        "confirm_promotion",
        "idempotency_key",
      ],
    ),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(submitStudioAsset),
  },
  {
    name: "approve_provider_switch",
    description: "Approve only an existing, revision-bound provider-switch proposal after confirmation.",
    inputSchema: schemaForDecision(),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(approveStudioSwitch),
  },
  {
    name: "reject_provider_switch",
    description: "Reject only an existing, revision-bound provider-switch proposal after confirmation.",
    inputSchema: schemaForDecision(),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(rejectStudioSwitch),
  },
  {
    name: "run_qa",
    description: "Run the existing bounded project QA operation for one current revision.",
    inputSchema: schemaForProjectWrite(),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(runStudioQa),
  },
  {
    name: "export_project",
    description: "Create a private archive or PDF export after explicit overwrite confirmation.",
    inputSchema: objectSchema(
      {
        project_id: PROJECT_ID,
        expected_revision: REVISION,
        format: { type: "string", enum: ["archive", "pdf"] },
        overwrite_confirmed: { type: "boolean", const: true },
        idempotency_key: IDEMPOTENCY_KEY,
      },
      ["project_id", "expected_revision", "format", "overwrite_confirmed", "idempotency_key"],
    ),
    annotations: WRITE_ANNOTATIONS,
    execute: safeExecute(exportStudioProject),
  },
]);

function assertObject(input, required) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new WebMcpInputError();
  }
  const keys = Object.keys(input).sort();
  const expected = [...required].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new WebMcpInputError();
  }
}

function assertString(value, minimum, maximum) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertSource(value) {
  const source = assertString(value, 1, 204800);
  if (new TextEncoder().encode(source).byteLength > MAX_SOURCE_BYTES) {
    throw new WebMcpInputError();
  }
  return source;
}

function assertProjectId(value) {
  if (typeof value !== "string" || !new RegExp(PROJECT_ID_PATTERN).test(value)) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertRevision(value, zero = false) {
  if (!Number.isInteger(value) || (zero ? value !== 0 : value < 1)) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertIdempotencyKey(value) {
  if (typeof value !== "string" || !new RegExp(UUID_PATTERN).test(value)) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertConfirmation(value) {
  if (value !== true) throw new WebMcpInputError();
}

function assertJobId(value) {
  if (typeof value !== "string" || !new RegExp(JOB_ID_PATTERN).test(value)) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertAssetId(value) {
  if (typeof value !== "string" || !new RegExp(ASSET_ID_PATTERN).test(value)) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertProposalId(value) {
  if (typeof value !== "string" || !new RegExp(PROPOSAL_ID_PATTERN).test(value)) {
    throw new WebMcpInputError();
  }
  return value;
}

function assertPlan(plan) {
  assertObject(plan, ["storyPlan", "characterBible", "storyboard", "visualIdentityPack"]);
  for (const key of ["storyPlan", "characterBible", "storyboard", "visualIdentityPack"]) {
    assertString(plan[key], 0, 1048576);
  }
  return plan;
}

function assertCurrentProject(projectId, revision, project) {
  if (!project || project.project_id !== projectId || project.revision !== revision) {
    throw new WebMcpInputError();
  }
  return project;
}

function safeProject(project) {
  if (!project) return { available: false };
  return {
    available: true,
    project_id: project.project_id,
    revision: project.revision,
    status: typeof project.status === "string" ? project.status.slice(0, 64) : "unknown",
    plan_available: Boolean(project.summary && project.summary.plan),
    qa_available: Boolean(project.summary && project.summary.qa),
  };
}

function safeQa(project) {
  if (!project) return { available: false };
  const qa = project.summary && project.summary.qa;
  const issues = qa && Array.isArray(qa.issues) ? qa.issues : [];
  return {
    available: Boolean(qa),
    project_id: project.project_id,
    revision: project.revision,
    valid: qa && typeof qa.valid === "boolean" ? qa.valid : null,
    issue_count: Math.min(issues.length, 100),
  };
}

function boundedString(value, maximum) {
  return typeof value === "string" ? value.slice(0, maximum) : "";
}

function safeOption(option) {
  if (!option || typeof option !== "object") return null;
  const authModes = Array.isArray(option.auth_modes)
    ? option.auth_modes.filter((value) => ["agent", "hosted", "byok"].includes(value)).slice(0, 3)
    : [];
  return {
    provider: boundedString(option.provider, 64),
    model: boundedString(option.model, 128),
    capabilities: Array.isArray(option.capabilities)
      ? option.capabilities.filter((value) => typeof value === "string").map((value) => value.slice(0, 64)).slice(0, 32)
      : [],
    auth_modes: authModes,
  };
}

function safeJob(job) {
  if (!job || typeof job !== "object") return null;
  const value = {
    job_id: boundedString(job.job_id, 64),
    project_id: boundedString(job.project_id, 128),
    project_revision: Number.isInteger(job.project_revision) ? job.project_revision : 0,
    state: boundedString(job.state, 64),
    provider: boundedString(job.provider, 64),
    model: boundedString(job.model, 128),
    auth_mode: boundedString(job.auth_mode, 16),
    attempt: Number.isInteger(job.attempt) ? job.attempt : 0,
    retry_count: Number.isInteger(job.retry_count) ? job.retry_count : 0,
    max_retries: Number.isInteger(job.max_retries) ? job.max_retries : 0,
    accepted_project_revision: Number.isInteger(job.accepted_project_revision)
      ? job.accepted_project_revision
      : 0,
    artifact_state: typeof job.artifact_state === "string" ? job.artifact_state.slice(0, 32) : null,
    can_cancel: job.can_cancel === true,
  };
  return value;
}

function safeRecommendation(recommendation) {
  if (!recommendation || typeof recommendation !== "object") return null;
  const cost = recommendation.estimated_cost;
  const safeCost = cost && typeof cost === "object"
    ? {
      amount: typeof cost.amount === "number"
        ? cost.amount
        : typeof cost.amount === "string" ? cost.amount.slice(0, 32) : null,
      currency: boundedString(cost.currency, 16),
      unit: boundedString(cost.unit, 32),
    }
    : null;
  return {
    provider: boundedString(recommendation.provider, 64),
    model: boundedString(recommendation.model, 128),
    auth_mode: boundedString(recommendation.auth_mode, 16),
    reasons: Array.isArray(recommendation.reasons)
      ? recommendation.reasons.filter((value) => typeof value === "string").map((value) => value.slice(0, 256)).slice(0, 8)
      : [],
    estimated_cost: safeCost,
  };
}

function safeProposal(proposal, decision) {
  if (!proposal || typeof proposal !== "object") return null;
  return {
    proposal_id: boundedString(proposal.proposal_id, 128),
    project_id: boundedString(proposal.project_id, 128),
    project_revision: Number.isInteger(proposal.project_revision) ? proposal.project_revision : 0,
    job_ids: Array.isArray(proposal.job_ids)
      ? proposal.job_ids.filter((value) => typeof value === "string").map((value) => value.slice(0, 64)).slice(0, 512)
      : [],
    decision,
  };
}

async function getProjectState(input) {
  assertObject(input, []);
  return safeProject(await getCurrentProject());
}

async function listOptions(input) {
  assertObject(input, []);
  const result = await getGenerationOptions();
  return {
    options: Array.isArray(result.options)
      ? result.options.map(safeOption).filter(Boolean).slice(0, 50)
      : [],
  };
}

async function recommendProvider(input) {
  assertObject(input, ["project_id", "expected_revision", "job_id"]);
  assertProjectId(input.project_id);
  assertRevision(input.expected_revision);
  assertJobId(input.job_id);
  const result = await getGenerationRecommendations(
    input.project_id,
    input.expected_revision,
    input.job_id,
  );
  return {
    recommendations: Array.isArray(result.recommendations)
      ? result.recommendations.map(safeRecommendation).filter(Boolean).slice(0, 8)
      : [],
  };
}

async function listJobs(input) {
  assertObject(input, ["project_id", "expected_revision"]);
  assertProjectId(input.project_id);
  assertRevision(input.expected_revision);
  const result = await listGenerationJobs(input.project_id, input.expected_revision);
  return {
    project_id: input.project_id,
    revision: input.expected_revision,
    jobs: Array.isArray(result.jobs) ? result.jobs.map(safeJob).filter(Boolean).slice(0, 50) : [],
    accepted_job: safeJob(result.accepted_job),
  };
}

async function getQaSummary(input) {
  assertObject(input, []);
  return safeQa(await getCurrentProject());
}

async function createStudioProject(input) {
  assertObject(input, [
    "title",
    "prompt",
    "language",
    "mode",
    "page_count",
    "expected_revision",
    "idempotency_key",
  ]);
  assertString(input.title, 1, 160);
  assertSource(input.prompt);
  assertString(input.language, 1, 16);
  if (!["short_prompt", "pasted_story"].includes(input.mode)) throw new WebMcpInputError();
  if (!Number.isInteger(input.page_count) || input.page_count < 1 || input.page_count > 4) {
    throw new WebMcpInputError();
  }
  assertRevision(input.expected_revision, true);
  assertIdempotencyKey(input.idempotency_key);
  const project = await createProject(
    {
      title: input.title,
      prompt: input.prompt,
      language: input.language,
      mode: input.mode,
      page_count: input.page_count,
    },
    input.idempotency_key,
  );
  publishProject(project);
  return safeProject(project);
}

function publishProject(project) {
  if (typeof document === "undefined" || typeof document.dispatchEvent !== "function") {
    throw new WebMcpInputError();
  }
  const detail = { project, accepted: false };
  document.dispatchEvent(new CustomEvent("comic-sol:project-selected", { detail }));
  if (!detail.accepted) throw new WebMcpInputError();
}

function pageOwnedArchive(handle) {
  if (handle !== "selected") throw new WebMcpInputError();
  const input = typeof document === "undefined" ? null : document.getElementById("project-archive");
  const file = input && input.files && input.files[0];
  if (!file || typeof file.name !== "string" || file.name.length > 255) {
    throw new WebMcpInputError();
  }
  if (file.name.includes("/") || file.name.includes("\\") || file.name.includes(":")) {
    throw new WebMcpInputError();
  }
  if (
    !file.name.endsWith(".comic-sol-handoff")
    || !Number.isInteger(file.size)
    || file.size < 1
    || file.size > MAX_ARCHIVE_BYTES
  ) {
    throw new WebMcpInputError();
  }
  return file;
}

async function importStudioProject(input) {
  assertObject(input, ["archive_handle", "expected_revision", "idempotency_key"]);
  assertRevision(input.expected_revision, true);
  assertIdempotencyKey(input.idempotency_key);
  const archive = pageOwnedArchive(input.archive_handle);
  const project = await importProject(archive, input.idempotency_key);
  publishProject(project);
  return safeProject(project);
}

async function updateStudioPlan(input) {
  assertObject(input, ["project_id", "expected_revision", "plan", "confirm_plan", "idempotency_key"]);
  assertProjectId(input.project_id);
  assertRevision(input.expected_revision);
  assertPlan(input.plan);
  assertConfirmation(input.confirm_plan);
  assertIdempotencyKey(input.idempotency_key);
  if (
    typeof document === "undefined"
    || typeof document.dispatchEvent !== "function"
    || !document.getElementById("plan-editor")
    || !document.getElementById("draft-diff")
  ) {
    throw new WebMcpInputError();
  }
  const current = await getCurrentProject();
  assertCurrentProject(input.project_id, input.expected_revision, current);
  const accepted = document.dispatchEvent(new CustomEvent("comic-sol:plan-proposal", {
    cancelable: true,
    detail: { expectedRevision: input.expected_revision, changes: input.plan },
  }));
  if (!accepted) throw new WebMcpInputError();
  return {
    project_id: input.project_id,
    revision: input.expected_revision,
    review_required: true,
  };
}

async function curatedSelection(provider, model, authMode) {
  const result = await getGenerationOptions();
  const options = Array.isArray(result.options) ? result.options : [];
  const selected = options.find((option) => option.provider === provider && option.model === model);
  if (!selected || !Array.isArray(selected.auth_modes) || !selected.auth_modes.includes(authMode)) {
    throw new WebMcpInputError();
  }
  return { provider, model, auth_mode: authMode };
}

async function queueStudioGeneration(input) {
  assertObject(input, [
    "project_id",
    "expected_revision",
    "provider",
    "model",
    "auth_mode",
    "confirm_cost",
    "idempotency_key",
  ]);
  assertProjectId(input.project_id);
  assertRevision(input.expected_revision);
  assertString(input.provider, 1, 64);
  assertString(input.model, 1, 128);
  if (!["agent", "hosted", "byok"].includes(input.auth_mode)) throw new WebMcpInputError();
  assertConfirmation(input.confirm_cost);
  assertIdempotencyKey(input.idempotency_key);
  const selection = await curatedSelection(input.provider, input.model, input.auth_mode);
  assertCurrentProject(
    input.project_id,
    input.expected_revision,
    await getCurrentProject(),
  );
  const result = await queueGeneration(
    input.project_id,
    input.expected_revision,
    selection,
    input.idempotency_key,
  );
  return {
    project_id: input.project_id,
    revision: input.expected_revision,
    jobs: Array.isArray(result.jobs) ? result.jobs.map(safeJob).filter(Boolean).slice(0, 50) : [],
  };
}

async function submitStudioAsset(input) {
  assertObject(input, [
    "project_id",
    "job_id",
    "asset_id",
    "expected_revision",
    "confirm_promotion",
    "idempotency_key",
  ]);
  assertProjectId(input.project_id);
  assertJobId(input.job_id);
  assertAssetId(input.asset_id);
  assertRevision(input.expected_revision);
  assertConfirmation(input.confirm_promotion);
  assertIdempotencyKey(input.idempotency_key);
  assertCurrentProject(
    input.project_id,
    input.expected_revision,
    await getCurrentProject(),
  );
  return safeJob(await submitGeneratedAsset(
    input.asset_id,
    input.job_id,
    input.expected_revision,
    input.idempotency_key,
  ));
}

async function decideProviderSwitch(input, decision) {
  assertObject(input, ["proposal_id", "expected_revision", "confirm_switch", "idempotency_key"]);
  assertProposalId(input.proposal_id);
  assertRevision(input.expected_revision);
  assertConfirmation(input.confirm_switch);
  assertIdempotencyKey(input.idempotency_key);
  const proposal = decision === "approved"
    ? await approveProposal(input.proposal_id, input.expected_revision, input.idempotency_key)
    : await rejectProposal(input.proposal_id, input.expected_revision, input.idempotency_key);
  return safeProposal(proposal, decision);
}

async function approveStudioSwitch(input) {
  return decideProviderSwitch(input, "approved");
}

async function rejectStudioSwitch(input) {
  return decideProviderSwitch(input, "rejected");
}

async function runStudioQa(input) {
  assertObject(input, ["project_id", "expected_revision", "idempotency_key"]);
  assertProjectId(input.project_id);
  assertRevision(input.expected_revision);
  assertIdempotencyKey(input.idempotency_key);
  const project = await runQa(input.project_id, input.expected_revision, input.idempotency_key);
  return safeProject(project);
}

async function exportStudioProject(input) {
  assertObject(input, [
    "project_id",
    "expected_revision",
    "format",
    "overwrite_confirmed",
    "idempotency_key",
  ]);
  assertProjectId(input.project_id);
  assertRevision(input.expected_revision);
  if (!["archive", "pdf"].includes(input.format)) throw new WebMcpInputError();
  assertConfirmation(input.overwrite_confirmed);
  assertIdempotencyKey(input.idempotency_key);
  const result = await exportProject(
    input.project_id,
    input.expected_revision,
    input.format,
    input.overwrite_confirmed,
    input.idempotency_key,
  );
  return {
    project_id: input.project_id,
    revision: result.revision,
    format: input.format,
    media_type: ["application/pdf", "application/zip"].includes(result.mediaType)
      ? result.mediaType
      : "application/octet-stream",
    byte_size: Number.isInteger(result.blob.size) ? result.blob.size : 0,
  };
}

function safeError(error) {
  if (error instanceof WebMcpInputError) {
    return { code: "invalid_request", message: "The WebMCP request is invalid." };
  }
  if (error instanceof StaleRevisionError) {
    return { code: "stale_revision", message: "The project revision is stale; refresh and retry." };
  }
  if (error instanceof StudioConflictError || error?.status === 409) {
    return { code: "conflict", message: "The Studio operation conflicts with the current state." };
  }
  if (error instanceof MigrationValidationError) {
    return { code: "archive_rejected", message: "The selected archive was rejected safely." };
  }
  if (error instanceof StudioApiError && (error.status === 401 || error.status === 403)) {
    return { code: "authorization_required", message: "The authenticated Studio session is unavailable." };
  }
  return { code: "request_failed", message: "The Studio operation could not be completed safely." };
}

function safeExecute(handler) {
  return async (input) => {
    try {
      return { ok: true, data: await handler(input) };
    } catch (error) {
      return { ok: false, error: safeError(error) };
    }
  };
}

function modelContext() {
  const navigatorContext = typeof navigator === "undefined" ? null : navigator.modelContext;
  if (navigatorContext && typeof navigatorContext.registerTool === "function") {
    return navigatorContext;
  }
  const documentContext = typeof document === "undefined" ? null : document.modelContext;
  return documentContext && typeof documentContext.registerTool === "function"
    ? documentContext
    : null;
}

export async function registerWebMcp() {
  try {
    const context = modelContext();
    if (!context) return false;
    for (const definition of TOOL_DEFINITIONS) {
      await context.registerTool(definition);
    }
    return true;
  } catch (_error) {
    return false;
  }
}
