# ComfyUI local reference executor

> **Status: reference/experimental.** Fake-server tests verify request shaping and safety
> behavior, not a live ComfyUI/model run. Do not describe this adapter as verified until a
> manual local smoke result has been recorded in issue #244.

This standard-library command is an agent-managed reference implementation of Comic Sol's
existing `external-tool` handoff contract. It is not imported by `scripts/` or
`comic_sol_product/`, is not an MCP tool, and is not part of the deterministic engine. The
active agent launches it for one prepared generation job, then submits the resulting local
raster through normal handoff result intake. Result intake remains responsible for raster
validation, retry accounting, provenance, receipts, atomic retention, visual QA, and
promotion.

The adapter does not install, start, update, or configure ComfyUI. It never downloads a
model, checkpoint, LoRA, VAE, custom node, or workflow. A workflow and its custom nodes are
user-controlled executable configuration inside the user's ComfyUI trust boundary.

## Prerequisites

- Python 3.11 or newer; the adapter uses only the Python standard library.
- An already-running ComfyUI server.
- A workflow exported from ComfyUI in **API format**.
- A profile that maps Comic Sol semantics to exact workflow node IDs and input names.
- A current `generation/jobs/<job-id>.json` produced by `comic-sol handoff prepare`.

## Run one job

```text
python integrations/comfyui-local/comfyui_executor.py run \
  --job PROJECT/generation/jobs/JOB_ID.json \
  --workflow /path/to/workflow-api.json \
  --profile /path/to/profile.json \
  --output /path/to/new-output.png
```

The complete command is:

```text
python integrations/comfyui-local/comfyui_executor.py run --job JOB --workflow WORKFLOW --profile PROFILE --output FILE [--endpoint URL] [--allow-non-loopback]
```

The default endpoint is `http://127.0.0.1:8188`. The output path must have an existing
parent and must not already exist. On success, stdout contains one compact JSON object with
sanitized result metadata. Diagnostics and the non-loopback warning go to stderr. The
adapter never emits the prompt, workflow, raw ComfyUI response, endpoint, credentials, or
private absolute paths. It publishes only the bounded output raster; a failed run removes
its temporary output.

A successful result identifies `executor_kind` as `external-tool` and `executor_id` as
`comfyui-local`. The invoking agent must pass the raster to normal handoff result intake;
the adapter does not write a Comic Sol receipt or promote an image.

## Profile contract

[`profile.schema.json`](profile.schema.json) describes schema version `1.0`.
[`example-profile.json`](example-profile.json) is intentionally non-runnable until its
node IDs and hashes are replaced for a real API workflow.

The profile has these fields:

- `profile_id`: bounded, sanitized profile identity.
- `profile_sha256`: SHA-256 of compact canonical JSON for the complete profile **without**
  `profile_sha256`. Canonical JSON is UTF-8, key-sorted, uses `,` and `:` separators, and
  does not append a newline. This closes on stale profile edits.
- `workflow_sha256`: SHA-256 of the workflow file's exact bytes. This closes on stale or
  substituted workflows.
- `model`: an optional sanitized label; never put a path, URL, credential, or secret here.
- `output_node_id`: the only node whose terminal `images` result may be downloaded.
- `inputs`: explicit mappings for the positive prompt and optional negative prompt, seed,
  width, height, and ordered references.

Each mapping names an existing workflow `node_id` and existing `input_name`. The adapter
patches only those exact entries and rejects unknown nodes, unknown inputs, duplicate
targets, partial width/height mappings, missing reference mappings, and unexpected output
nodes. It never guesses from node classes, model names, provider names, or graph shape.

Comic Sol jobs contain the positive prompt, requested dimensions, and ordered reference
hashes. They do not contain a negative prompt or seed, so mapped negative-prompt and seed
entries carry explicit versioned `value` fields in the profile. Set either mapping to
`null` to leave that workflow input unchanged.

Capability metadata reports actual use, not the adapter's name:

- `reference_images` is true only when the job contains references and every reference is
  explicitly mapped and uploaded.
- `dimensions` is true only when the job requests dimensions and both width and height are
  explicitly mapped.
- `localized_edit` is always false in profile schema `1.0`; no localized-edit mapping is
  defined by this reference adapter.

To calculate `profile_sha256`, remove the field while hashing and then put the digest back.
For example:

```python
import hashlib
import json

profile.pop("profile_sha256", None)
encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
    "utf-8"
)
profile["profile_sha256"] = hashlib.sha256(encoded).hexdigest()
```

Hash the workflow's exact bytes separately for `workflow_sha256`. Recalculate both hashes
whenever their corresponding content intentionally changes.

## Request sequence

For a job with references, the adapter performs only this bounded sequence:

1. Verify the job ID, job location, prompt hash, ordered reference hashes, workflow hash,
   profile hash, profile shape, and every explicit mapping.
2. Upload references in job order through `POST /upload/image`.
3. Submit the patched API workflow through `POST /prompt`.
4. Poll `GET /history/{prompt_id}` to a fixed execution deadline.
5. Accept exactly one image from `output_node_id` and retrieve it through `GET /view`.
6. fsync and atomically publish the new local raster, then emit sanitized metadata.

WebSocket `/ws` support is intentionally out of scope.

## Network and trust boundary

Loopback IPv4 and IPv6 resolutions are accepted by default. Before every connection the
adapter resolves the configured host, validates every returned address, and connects with
a numeric pinned address while preserving the approved Host origin. It rejects:

- embedded usernames or passwords;
- URL fragments, queries, or endpoint path prefixes;
- non-HTTP schemes;
- non-loopback or mixed loopback/non-loopback DNS results by default;
- a rebinding result that becomes non-loopback on any later request;
- all redirects, including same-origin redirects;
- server-supplied URLs and unsafe output path components.

`--allow-non-loopback` is an explicit unsafe override. It prints a warning because local
ComfyUI has no Comic Sol authentication boundary. The override relaxes only the address
class check; origin pinning, redirect refusal, response validation, size limits, and
timeouts remain active. Do not use the flag unless the user has deliberately placed that
server inside an independently secured trust boundary.

## Bounds and failure behavior

The adapter bounds job/workflow/profile JSON, each uploaded reference, queue/history JSON,
and the downloaded raster. It also limits JSON depth, collection sizes, and string sizes.
Connection, queue, execution, and download phases have separate timeouts. Downloads are
streamed to a byte ceiling even if `Content-Length` is absent or false.

Failures produce a nonzero exit and one sanitized category, such as `invalid-job`,
`stale-job-input`, `stale-workflow`, `stale-profile`, `endpoint-rejected`,
`redirect-rejected`, `queue-timeout`, `execution-timeout`, `download-timeout`,
`malformed-response`, `unexpected-response`, or `size-limit`. Raw server diagnostics are
not persisted or returned.

The adapter does not decode or promote the downloaded raster. Always use normal handoff
result intake next. That route validates the local raster, applies bounded retry accounting,
retains the attempt and receipt atomically, and keeps panel promotion blocked until normal
visual QA passes.

## Verification boundary

Run the isolated fake-server suite without a GPU, model, network, or real ComfyUI:

```text
python -m unittest tests.test_comfyui_executor
```

Those tests exercise `/upload/image`, `/prompt`, `/history/{prompt_id}`, `/view`, explicit
mapping, stale hashes, SSRF/origin controls, redirects, malformed responses, timeouts, and
size ceilings. They are mechanics-only evidence. A real ComfyUI workflow/model is not
verified until a maintainer records the required manual local smoke result in issue #244;
do not commit that generated image or a private workflow without separate approval.
