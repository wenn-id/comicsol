# Demo fixture

This fixture is hand-authored and offline-only. It is **not** an
artifact of a reproducible WebMCP run: the merged E2E test does not
invoke WebMCP, imports a different archive, and rejects its 1x1
raster, so it cannot produce this three-panel plan or queue. The
fixture exists so a reviewer can inspect the shape of a Plan and a
queue without contacting a paid provider.

The fixture contains:

- `story.txt` — the short story used as the prompt;
- `plan.json` — the Plan produced by the WebMCP `create_project` tool;
- `queue.json` — the queue state illustrated for the offline run.

The fixture does not contain a credential, an API key, a token, a
password, a session secret, an authorization header, or any other value
that would identify a real provider account.
