const PROJECTS_PATH = "/api/projects";
export const MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024;

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
    if (candidate.startsWith(prefix)) {
      return decodeURIComponent(candidate.slice(prefix.length));
    }
  }
  return "";
}

function validateEnvelope(value) {
  if (
    !value ||
    typeof value !== "object" ||
    typeof value.project_id !== "string" ||
    !Number.isInteger(value.revision) ||
    value.revision < 1 ||
    typeof value.status !== "string" ||
    !value.summary ||
    typeof value.summary !== "object" ||
    !value.summary.plan ||
    typeof value.summary.plan !== "object" ||
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

async function readEnvelope(response, { archive = false } = {}) {
  if (response.status === 409) {
    throw new StaleRevisionError();
  }
  if (!response.ok) {
    if (archive && (response.status === 400 || response.status === 413 || response.status === 422)) {
      throw new MigrationValidationError(response.status);
    }
    if (response.status === 401 || response.status === 403) {
      throw new StudioApiError("Your Studio session is unavailable. Sign in again and retry.", response.status);
    }
    throw new StudioApiError("The project request could not be completed safely.", response.status);
  }
  try {
    return validateEnvelope(await response.json());
  } catch (error) {
    if (error instanceof StudioApiError) {
      throw error;
    }
    throw new StudioApiError("The server returned an invalid project response.");
  }
}

async function writeRequest(path, { body, expectedRevision, archive = false, idempotencyKey }) {
  const csrf = cookieValue("comic_sol_csrf");
  if (!csrf) {
    throw new StudioApiError("Your Studio session is unavailable. Sign in again and retry.", 403);
  }
  const headers = {
    "X-CSRF-Token": csrf,
    "Idempotency-Key": idempotencyKey || crypto.randomUUID(),
    "X-Expected-Revision": String(expectedRevision),
  };
  if (!(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: body instanceof FormData ? body : JSON.stringify(body),
  });
  return readEnvelope(response, { archive });
}

export function createProject(request, idempotencyKey) {
  return writeRequest(PROJECTS_PATH, {
    body: request,
    expectedRevision: 0,
    idempotencyKey,
  });
}

export function importProject(archive, idempotencyKey) {
  const body = new FormData();
  body.append("archive", archive, archive.name);
  return writeRequest(`${PROJECTS_PATH}/import`, {
    body,
    expectedRevision: 0,
    archive: true,
    idempotencyKey,
  });
}

export function updatePlan(projectId, plan, expectedRevision) {
  return writeRequest(PROJECTS_PATH, {
    body: { project_id: projectId, plan },
    expectedRevision,
  });
}

export async function getCurrentProject() {
  const response = await fetch(`${PROJECTS_PATH}/current`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (response.status === 204) return null;
  return readEnvelope(response);
}

export async function getProject(projectId) {
  const response = await fetch(`${PROJECTS_PATH}/${encodeURIComponent(projectId)}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  return readEnvelope(response);
}
