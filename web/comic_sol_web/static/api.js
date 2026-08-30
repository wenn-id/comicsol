const PROJECTS_PATH = "/api/projects";
const GENERATION_PATH = "/api/generation";
const APPROVALS_PATH = "/api/approvals";
const ASSETS_PATH = "/api/assets";
export const MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024;
export const MAX_SOURCE_BYTES = 200 * 1024;

export class StudioApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "StudioApiError";
    this.status = status;
  }
}

export class StaleRevisionError extends StudioApiError {
  constructor() {
    super("The project changed in another session. Refresh before continuing.", 409);
    this.name = "StaleRevisionError";
  }
}

export class MigrationValidationError extends StudioApiError {
  constructor(status) {
    super("The archive could not be validated or migrated. The original archive was not changed.", status);
    this.name = "MigrationValidationError";
  }
}

function cookieValue(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(";")) {
    const candidate = part.trim();
    if (candidate.startsWith(prefix)) return decodeURIComponent(candidate.slice(prefix.length));
  }
  return "";
}

function validateEnvelope(value) {
  if (
    !value || typeof value !== "object" || typeof value.project_id !== "string" ||
    !Number.isInteger(value.revision) || value.revision < 1 || typeof value.status !== "string" ||
    !value.summary || typeof value.summary !== "object" ||
    !value.summary.plan || typeof value.summary.plan !== "object" ||
    !["storyPlan", "characterBible", "storyboard", "visualIdentityPack"].every(
      (field) => typeof value.summary.plan[field] === "string",
    )
  ) {
    throw new StudioApiError("The server returned an invalid project response.");
  }
  const plan = Object.freeze({
    storyPlan: value.summary.plan.storyPlan,
    characterBible: value.summary.plan.characterBible,
    storyboard: value.summary.plan.storyboard,
    visualIdentityPack: value.summary.plan.visualIdentityPack,
  });
  return Object.freeze({
    project_id: value.project_id,
    revision: value.revision,
    status: value.status,
    summary: Object.freeze({ ...value.summary, plan }),
  });
}

function requestFailure(response, archive = false) {
  if (response.status === 409) throw new StaleRevisionError();
  if (archive && (response.status === 400 || response.status === 413 || response.status === 422)) {
    throw new MigrationValidationError(response.status);
  }
  if (response.status === 401 || response.status === 403) {
    throw new StudioApiError("Your Studio session is unavailable. Sign in again and retry.", response.status);
  }
  throw new StudioApiError("The project request could not be completed safely.", response.status);
}

async function readEnvelope(response, { archive = false } = {}) {
  if (!response.ok) requestFailure(response, archive);
  try {
    return validateEnvelope(await response.json());
  } catch (error) {
    if (error instanceof StudioApiError) throw error;
    throw new StudioApiError("The server returned an invalid project response.");
  }
}

async function readJson(response) {
  if (!response.ok) requestFailure(response);
  try {
    const value = await response.json();
    if (!value || typeof value !== "object") throw new Error("invalid response");
    return value;
  } catch (error) {
    if (error instanceof StudioApiError) throw error;
    throw new StudioApiError("The server returned an invalid Studio response.");
  }
}

async function writeRequest(
  path,
  { body, expectedRevision, archive = false, idempotencyKey, responseType = "project" },
) {
  const csrf = cookieValue("comic_sol_csrf");
  if (!csrf) {
    throw new StudioApiError("Your Studio session is unavailable. Sign in again and retry.", 403);
  }
  const headers = {
    "X-CSRF-Token": csrf,
    "Idempotency-Key": idempotencyKey || crypto.randomUUID(),
    "X-Expected-Revision": String(expectedRevision),
  };
  if (!(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: body instanceof FormData ? body : JSON.stringify(body),
  });
  if (responseType === "raw") {
    if (!response.ok) requestFailure(response, archive);
    return response;
  }
  return responseType === "json" ? readJson(response) : readEnvelope(response, { archive });
}

function getJson(path) {
  return fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  }).then(readJson);
}

export function createProject(request, idempotencyKey) {
  return writeRequest(PROJECTS_PATH, { body: request, expectedRevision: 0, idempotencyKey });
}

export function importProject(archive, idempotencyKey) {
  const body = new FormData();
  body.append("archive", archive, archive.name);
  return writeRequest(`${PROJECTS_PATH}/import`, {
    body, expectedRevision: 0, archive: true, idempotencyKey,
  });
}

export function updatePlan(projectId, plan, expectedRevision, idempotencyKey) {
  return writeRequest(PROJECTS_PATH, {
    body: { project_id: projectId, plan }, expectedRevision, idempotencyKey,
  });
}

export async function getCurrentProject() {
  const response = await fetch(`${PROJECTS_PATH}/current`, {
    method: "GET", credentials: "same-origin", headers: { Accept: "application/json" },
  });
  if (response.status === 204) return null;
  return readEnvelope(response);
}

export async function getProject(projectId) {
  const response = await fetch(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}`, {
    method: "GET", credentials: "same-origin", headers: { Accept: "application/json" },
  });
  return readEnvelope(response);
}

export function getGenerationOptions() {
  return getJson(`${GENERATION_PATH}/options`);
}

export function getGenerationRecommendations(projectId, expectedRevision, jobId) {
  const query = new URLSearchParams({
    project_id: projectId,
    expected_revision: String(expectedRevision),
    job_id: jobId,
  });
  return getJson(`${GENERATION_PATH}/recommendations?${query}`);
}

export function listGenerationJobs(projectId, expectedRevision) {
  const query = new URLSearchParams({
    project_id: projectId,
    expected_revision: String(expectedRevision),
    limit: "50",
  });
  return getJson(`${GENERATION_PATH}/jobs?${query}`);
}

export function queueGeneration(projectId, expectedRevision, selection, idempotencyKey) {
  return writeRequest(`${GENERATION_PATH}/queue`, {
    body: {
      project_id: projectId,
      expected_revision: expectedRevision,
      provider: selection.provider,
      model: selection.model,
      auth_mode: selection.auth_mode,
    },
    expectedRevision,
    idempotencyKey,
    responseType: "json",
  });
}

function generationAction(jobId, action, expectedRevision, idempotencyKey) {
  return writeRequest(`${GENERATION_PATH}/${encodeURIComponent(jobId)}/${action}`, {
    body: { expected_revision: expectedRevision },
    expectedRevision,
    idempotencyKey,
    responseType: "json",
  });
}

export function retryGeneration(jobId, expectedRevision) {
  return generationAction(jobId, "retry", expectedRevision);
}

export function cancelGeneration(jobId, expectedRevision) {
  return generationAction(jobId, "cancel", expectedRevision);
}

export function pauseForSwitch(jobId, expectedRevision) {
  return generationAction(jobId, "pause-for-switch", expectedRevision);
}

export function submitStagedRaster(jobId, expectedRevision) {
  return generationAction(jobId, "submit-staged", expectedRevision);
}

export function submitGeneratedAsset(assetId, jobId, expectedRevision, idempotencyKey) {
  return writeRequest(`${ASSETS_PATH}/${encodeURIComponent(assetId)}/submit-agent`, {
    body: { job_id: jobId, expected_revision: expectedRevision },
    expectedRevision,
    idempotencyKey,
    responseType: "json",
  });
}

function proposalDecision(proposalId, decision, expectedRevision, idempotencyKey) {
  return writeRequest(`${APPROVALS_PATH}/${encodeURIComponent(proposalId)}/${decision}`, {
    body: {}, expectedRevision, idempotencyKey, responseType: "json",
  });
}

export function approveProposal(proposalId, expectedRevision) {
  return proposalDecision(proposalId, "approve", expectedRevision, arguments[2]);
}

export function rejectProposal(proposalId, expectedRevision) {
  return proposalDecision(proposalId, "reject", expectedRevision, arguments[2]);
}

export function runQa(projectId, expectedRevision, idempotencyKey) {
  return writeRequest(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}/qa`, {
    body: {}, expectedRevision, idempotencyKey,
  });
}

export async function exportProject(
  projectId,
  expectedRevision,
  format,
  overwriteConfirmed,
  idempotencyKey,
) {
  const response = await writeRequest(
    `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/export`,
    {
      body: { format, overwrite_confirmed: overwriteConfirmed },
      expectedRevision,
      idempotencyKey,
      responseType: "raw",
    },
  );
  const revision = Number(response.headers.get("x-project-revision"));
  if (!Number.isInteger(revision) || revision < expectedRevision) {
    throw new StudioApiError("The server returned an invalid export response.");
  }
  return Object.freeze({
    blob: await response.blob(),
    mediaType: response.headers.get("content-type"),
    revision,
  });
}

export function acceptedRasterUrl(projectId, expectedRevision, jobId) {
  const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
  return `${PROJECTS_PATH}/${encodeURIComponent(projectId)}/accepted-raster/${encodeURIComponent(jobId)}?${query}`;
}
