# Limitations

The WP17 work package ships an honest, evidence-bounded submission.
The following classes of evidence were **not** produced in this work
package; each is listed here and referenced from
[`README.md`](README.md) so the PR checkpoint can mirror them.

## No external deployment

- No production instance of Comic Sol Studio exists.
- No external deployment URL is claimed.
- The [deployment contract](../../docs/web/deployment.md) is documented
  but has not been exercised against a live target.

## No video recording

- No video was recorded for the demo.
- The [demo script](demo.md) is a narration in lieu of a video.
- A future work package that records a video must update this section
  and the PR checkpoint with the retained artifact link.

## No live paid provider call

- No paid provider call was authorized or made.
- Every paid row in the [provider evidence table](provider-evidence.md)
  is `Not run` for live smoke and `None` for evidence.
- A future work package that runs a live smoke must record the retained
  evidence link in the provider matrix.

## No local ComfyUI

- No local ComfyUI instance was available in this work package.
- The local ComfyUI route is documented as experimental and was not
  exercised.
- A future work package that exercises the local route must record the
  evidence and update both the provider matrix and the user guide.

## No active-agent WebMCP

- No browser environment in this work package exposed
  `document.modelContext`.
- The active-agent image generation route is documented as
  experimental and was not exercised.
- A future work package that demonstrates the active-agent route must
  record the environment, the exact model context, and the retained
  evidence.

## What this document does not limit

- The submission is still a Draft PR, not a merged release.
- The `67` documentation contract tests under
  `web/tests/test_web_docs.py` are RED before documentation and GREEN
  after, and are part of the release-qualification suite. The exact
  count for the final head is recorded in
  [verification.md](verification.md).
- The local MCP surface remains exactly 17 `comic_*` tools and is not
  changed by this work package.
