# Image capability detection

> **Provider setup:** See [`image-provider-setup.md`](image-provider-setup.md) for
> platform-specific, provider-neutral setup guidance.

Capability detection belongs to the agent plane, not deterministic Python. The agent
performs a metadata-only inspection before `doctor`; detection never invokes an image
tool, probes a provider, reads credentials, or makes a network request.

## Inspect the active session

1. Inspect only the tools exposed in the current session. A usable capability must both
   create an image from text alone and return or write a local raster that Comic Sol can
   retain. An editing-only tool is insufficient.
2. Choose one best usable capability. Prefer declared reference-image support, then exact
   dimensions or aspect-ratio support. Use the fixed neutral capability name
   `agent-image-generation`; do not infer features from a provider, model, or tool name.
3. Include an optional feature only when the exposed tool description or schema declares
   it. Unknown and unsupported features both use the safe degraded behavior: omit the
   matching flag.
4. Note localized editing separately. Pass `--localized-edit` to `repair_strategy.py`
   only when the session exposes a tool that can edit a bounded part of an existing
   raster and return a local image. Localized editing is not part of the `doctor`
   descriptor.

Never inspect provider packages, client configuration, credential environment variables,
endpoints, or accounts. Never import a provider SDK, embed credentials, ask the user to
paste a secret, silently install a tool, or enable a third-party provider.

## Report the observation to `doctor`

For a usable capability, pass one active descriptor. Add each optional feature flag only
when it was declared by the selected tool:

```text
PYTHON scripts/comic_sol.py doctor --output-root OUTPUT_ROOT --image-capability-status available --image-capability-name agent-image-generation --supports-reference-images --supports-dimensions
```

If the exposed inventory is inspectable but contains no usable text-to-image capability,
report that fact without installing or invoking anything:

```text
PYTHON scripts/comic_sol.py doctor --output-root OUTPUT_ROOT --image-capability-status unavailable
```

If the inventory cannot be inspected or metadata inspection fails, omit every image
capability flag. `doctor` then reports the capability as unknown instead of guessing.

The stable `image-capability` check distinguishes these states:

- `PASS`: the selected capability is usable and declares reference-image and dimension
  support.
- `WARN`: it is usable but one or both optional features are unavailable or unknown, so
  Comic Sol runs in a partial/degraded mode.
- `WARN`: the inspected session has no usable capability. This does not make the
  deterministic editor unhealthy, but panel generation will later stop at `BLOCKED`.
- `WARN`/`INFO`: no observation was supplied because detection was unavailable or failed.

The doctor observation is provider-neutral diagnostic input; it is not a credential and
does not itself invoke or persist provider state. When a project is initialized, record
the same neutral name, availability, reference support, dimension support, and detection
time in `project.json` according to `schemas.md`.

The first canonical-reference generation is the operational check. After every actual
invocation, require a local PNG/JPEG/WebP that Pillow can read, with both dimensions at
least 512 px, and normalize accepted data to PNG. If references are unsupported,
strengthen exact text anchors and record degraded mode.

If no compatible capability is exposed, print this exact leading error:

> Comic Sol cannot generate panels because this agent session has no compatible text-to-image capability. Enable or install an image-generation skill/tool that can return a local raster image, then say “resume this Comic Sol project.” Your story plan and editable project files have been preserved at the project path printed below.

Print the resolved absolute project path on the next line. Transition to `BLOCKED`, retain
plans and prompts, and create no placeholder image. For refusal, quota, transient, or tool
failure, preserve the same artifacts and report only the sanitized category plus resume
instruction.
