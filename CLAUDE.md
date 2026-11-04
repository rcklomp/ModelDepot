# ModelDepot -- rules for a session started in this repository

**What this is.** A self-hosted, Hugging Face-compatible repository for AI models and datasets (no Spaces), run in a Docker container on the owner's
Synology DS713+ (Intel Atom D2700, 4 GB RAM, an old kernel, Docker installed), with a browser GUI. Machines on the LAN set `HF_ENDPOINT` to it and
`huggingface_hub`, `transformers`, `hf download`, vLLM, mlx_lm, llama.cpp and Ollama fetch from it as if it were huggingface.co. Why: models and
datasets stay available when the Hub is blocked, rate-limited or gone, and are downloaded once per LAN instead of once per machine.

**What it is NOT (owner, 2026-11-04).** It is not part of the Franken-Engine, Colibri or rig projects, and none of their rules, session card or closeout apply
here. The AI rig is just one client of it, like any other machine. Do not add rig, Franken or Ollama-management code to this repository; the rig's own model
console moved to colibri (`tools/hot-expert/rig_console/`, frozen copy of this repository's old `console/` at the tag `archive/rig-console`). The application is
called **ModelDepot** (the owner chose the name on 2026-11-04; the Gitea repository is `ronald/ModelDepot`, it was `ronald/LLM_repo`, and the old name still redirects).
The Mac folder is still `~/Projects/LLM_repo` because colibri's closeout check and its tests name that path; the name of the app lives in one place in the GUI
(`APP_NAME` in `server/ui/app.js`, with the same text in `index.html`'s `<title>`).

**Layout.** `server/` the one app: `adapters/hf_adapter.py` (Hub read routes for models and datasets) and `adapters/hf_upload.py` (create repo, preupload, LFS, commit),
`hub/` (the GUI's JSON API `api.py`, the download queue `jobs.py` + `runner.py`, upstream search/picker `upstream.py`, `upload.py`, `library.py`, `state.py`), `ui/` (the GUI: plain
JS and CSS, no build step), the core (`repository.py`, `storage.py`, `blobstore.py`, `manifests.py`, `locks.py`: content-addressed blobs, immutable manifests, mutable refs, plain files).
`ingester/` downloads and imports (HF, Civitai, local, the `repo` CLI). `docs/` (start with ARCHITECTURE.md), `tests/` (unit, contract, `compat/` = the REAL huggingface_hub client against
a live server, `hub/` = the GUI API). The principles in the README (storage is the truth, blobs immutable, derived state rebuildable) are not negotiable.

**The NAS (facts, 2026-11-04).** Synology DS713+ (`klompnas3971.lan`), DSM 7.1.1, 4 GB RAM, Docker present. Already used there: Gitea on :3000, the restic rest-server on :8000 (so this
application's host port defaults to **8090**). The image is built for linux/amd64 (`docker build --platform linux/amd64`); on a Mac, Docker Desktop must be started by hand to test it.

**Rules for a session here.**
- Done = the tests are green AND, for anything a client sees, a probe with the REAL `huggingface_hub` client against a running instance (the unit tests
  once passed while `snapshot_download` was broken). Say what was run and what was not; "not run" is never green.
- The target is small: one uvicorn worker, no C extensions, no Node build step, nothing that needs more than a few hundred MB. Keep dependencies few.
- Never delete or rewrite the owner's store (`MIRROR_ROOT`); anything that removes blobs must be safe against a download in progress.
- Credentials: never read, print, copy or write `~/.config/git/gitea-*credentials`, tokens or ssh keys. Push as `claude-bot` only (credential-store helper), to Gitea
  `ronald/ModelDepot`, branch `main`. Another agent (the owner's Hermes Agent on the NAS, commits as `hermes-agent`) also pushes here: `git pull --ff-only` first,
  never force-push, never rebase published commits.
- Deploying to the NAS is not something a session can do (no ssh entry for it): a change lands on Gitea `main`; the NAS pulls it and rebuilds the image
  (`docs/DEPLOYMENT.md`). Say that plainly instead of claiming a deploy.
- Small commits, one change each; code first, its doc in the same commit. Commit messages end with the attribution lines from the session's system reminder.

**How the owner likes answers.** Plain words, short: what changed, what it means, whether it is safe, then the next item. Few numbers in the message (the detail
goes in the commit). "Not measured" / "not verified" instead of a guess. Times in Amsterdam time. Never hand the owner a to-do list; if something is open and not
his to decide, decide, take the safe default and name the assumption in one line.
