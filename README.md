# LLM Repository (working name)

Your own **Hugging Face for the LAN**: a repository for AI **models and datasets** that runs in one Docker container on a NAS, with a browser GUI.
Machines on your network point `HF_ENDPOINT` at it, and `hf download`, `transformers`, `datasets`, vLLM, mlx_lm, llama.cpp and Ollama fetch from it as if it were
huggingface.co. (No Spaces: they are apps, not files.)

```bash
export HF_ENDPOINT=http://<nas>:8090
export HF_HUB_DISABLE_XET=1
hf download Qwen/Qwen3-0.6B
```

## Why

- Download a model **once** for the whole household instead of once per machine.
- If Hugging Face is blocked, rate-limited or a model is taken down, what you already stored keeps working.
- Plain files on storage you own (a NAS volume): back it up or move it with `rsync`.

## What you get

- **A browser GUI** on the same port: browse the library, read a model's README and files, search Hugging Face and Civitai, pick exactly the quant or files you want (with sizes),
  watch downloads with progress, pause and resume, see and free disk space, and copy ready-made connection lines for any machine.
- **A download queue that is safe for big files:** one at a time, optional night-time window, resumes after a drop or a restart, refuses what will not fit, checks every file's sha256.
- **"Wanted":** when a machine asks for a model the library lacks, it shows up with a one-click Download.
- **Models and datasets**, including the legacy short names (`gpt2`, `squad`). Tested with the real `huggingface_hub`, `hf download --repo-type dataset` and `datasets.load_dataset`.
- **Publish your own:** push your fine-tunes and datasets with the normal tools (`hf upload`, `HfApi.upload_folder`, `HF_TOKEN=<admin token>`) or drag a folder into the GUI's Upload page. Each upload is one new revision; old revisions keep serving their bytes.
- **Safe by construction:** blobs are stored by sha256 and never change; garbage collection and delete can never remove the files of a running download; writes are fsync'd.

## Run it (Synology DS713+ or any Docker host)

```bash
cp .env.example .env        # set DATA_DIR to a folder on the NAS volume
docker compose up -d --build
```

Open `http://<nas>:8090`. The first admin action asks for the admin token the server made (`DATA_DIR/config/admin-token`). Full walkthrough, backup and
troubleshooting: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Not yet tested on the DS713+ itself (an old kernel and Docker): the image builds for `linux/amd64` and passes its checks elsewhere.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest                                   # unit, contract, real-client compatibility and GUI-API tests
MIRROR_ROOT=./data uvicorn server.main:app --reload
```

Command line (inside the container: `docker exec llm-repo repo ...`): `repo ingest-hf <id> [--type dataset] [--allow GLOB]`, `ingest-civitai`, `ingest-local`, `list`, `verify`, `gc`.

## Not supported

Branches, pull requests and private repositories (an upload is one new revision of `main`); resumable uploads of one huge file (one PUT per file); transparent pull-through caching; Spaces never. Details: `docs/API_COMPATIBILITY.md`.

## Map

`server/` the app (`adapters/hf_adapter.py` Hub routes, `hub/` the GUI's API, jobs and runner, `ui/` the GUI) - `ingester/` downloads and imports - `docs/` (start with
`ARCHITECTURE.md`, `API_COMPATIBILITY.md`, `GUI-API.md`) - `tests/`. The rig's old model console lives in the colibri repository, not here.
