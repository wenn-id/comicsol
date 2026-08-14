# Comic Sol Support

Support channel:
[GitHub Issues](https://github.com/wenn-id/comicsol/issues)

Include:

- Comic Sol version
- Codex version and host surface
- operating system
- exact command or prompt
- sanitized error output
- a minimal reproduction when possible

Do not post API keys, passwords, private story text, private images, personal
contact data, or generated logs containing sensitive material.

Before opening an issue:

1. Start a fresh Codex session after installing or updating the plugin.
2. Confirm the plugin is enabled with `codex plugin list --json`.
3. Resolve one Python 3.11+ launcher and store it as `PYTHON`, then run check from repository root:

   ```bash
   # POSIX
   PYTHON=python  # replace with resolved launcher
   "$PYTHON" scripts/comic_sol.py doctor --output-root ./comic-sol-output

   # Windows PowerShell:
   # $PYTHON = "py"; & $PYTHON -3 scripts\\comic_sol.py doctor --output-root .\\comic-sol-output
   ```

4. Attach only sanitized output.
