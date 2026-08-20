# Image capability detection

> **Provider setup:** See [`image-provider-setup.md`](image-provider-setup.md) for
> platform-specific, provider-neutral setup guidance.

Capability detection belongs to the agent plane, not deterministic Python.

1. Inspect only the skills/tools exposed in the current session for text-to-image or a
   creation-capable image tool that returns or writes a local raster.
2. Prefer reference-image support, then dimensions/aspect support, then direct PNG.
3. Note separately whether the same session also exposes localized editing: a tool that
   edits a bounded part of an existing raster and returns a local image. Pass
   `--localized-edit` to `repair_strategy.py` only then; without it every repair plans a
   full regeneration, which is the safe default rather than a failure.
4. Record only a neutral capability name, availability, reference support, dimension
   support, and detection time. Never record or read credential environment variables,
   import vendor client libraries, embed credentials, or ask the user to paste a secret.
5. Make no speculative network request for detection. The first canonical-reference
   generation is the operational check.
6. After each invocation, require a local PNG/JPEG/WebP that Pillow can read, with both
   dimensions at least 512 px; normalize accepted data to PNG.

An editing-only tool is insufficient unless it can create the first reference from text.
If references are unsupported, strengthen exact text anchors and record degraded mode.

If no compatible capability is exposed, print this exact leading error:

> Comic Sol cannot generate panels because this agent session has no compatible text-to-image capability. Enable or install an image-generation skill/tool that can return a local raster image, then say “resume this Comic Sol project.” Your story plan and editable project files have been preserved at the project path printed below.

Print the resolved absolute project path on the next line. Transition to `BLOCKED`, retain
plans and prompts, and create no placeholder image. For refusal, quota, transient, or tool
failure, preserve the same artifacts and report only the sanitized category plus resume
instruction.
