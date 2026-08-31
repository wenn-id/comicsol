# Demo fixture

This fixture is hand-authored and offline-only. It is **not** an
artifact of a reproducible WebMCP run: the merged E2E test does not
invoke WebMCP, imports a different archive, and rejects its 1x1
raster, so it cannot produce this three-panel plan or queue. The
fixture exists so a reviewer can inspect the shape of a Plan and a
queue without contacting a paid provider.

The fixture contains:

- `story.txt` — a hand-authored short story, illustrative of a prompt;
- `plan.json` — a hand-authored illustrative Plan shape; no
  `create_project` call produced it;
- `queue.json` — a hand-authored illustrative queue shape.

The fixture does not contain a credential, an API key, a token, a
password, a session secret, an authorization header, or any other value
that would identify a real provider account.
