# One model store for rome -- plan (2026-10-13, proposal, not built)

The owner: "we need an integral solution for the LLM repo and how to have a model repository standardised for storing
LLMs on a machine with different inference servers/engines."

## 1. What is there today (surveyed 2026-10-13)

| consumer | where it finds a model | how |
|---|---|---|
| Franken engine (GLM / DS4 / Qwen arms) | `~/models/<dir>/<quant>/*.gguf` | paths hard-coded in `start_franken*.sh` |
| llama-swap (6 entries) | `~/llm-mirror/blobs/sha256/90/9028...` (qwen-chat) and `~/models/...gguf` | paths in three wrapper scripts |
| Ollama 0.35.1 (5 models) | `/var/lib/ollama/.ollama/models` (root-owned) | `ollama create` COPIES the file in |
| ComfyUI | `~/comfyui-models/<type>/` (75 GB) | its own folder convention |
| LLM_repo mirror | `~/llm-mirror` (127 GB): sha256 blobs + manifests + sqlite index | downloads (HF / Civitai), serves an HF-compatible API |
| stray | `~/.cache/huggingface` (343 MB) | tools that downloaded on their own |

`~/models` is ~1 TB, of which ~600 GB is named by no serving script (research leftovers: DSpark-draft, DFlash2,
MTP-drafter, qwen36_* variants, hipengine_qwen36_gguf, gpt-oss-120b, Qwen3.8-27B, the FP8 checkpoint). Nothing records
which engine uses which file, models built on the rig (the GLM IQ3_XXS hybrid) have no provenance except the record,
and the only sha256 list is colibri's `served.sha256` (GLM only).

## 2. Prior art (research 2026-10-13; links in the session record)

- **No tool shares one store across Ollama / llama.cpp / LM Studio / custom engines.** Harbor's author: "different
  engines would still only work with their own models". The community fix is a readable tree plus links.
- **Readable `publisher/repo/file` tree** (LM Studio's convention; `lms import -L` hardlinks): stable paths, real
  files, any engine can be given a path.
- **HF hub cache** (`models--org--repo/{blobs,snapshots/<commit>}`): best tooling, but commit hashes in every path --
  bad for hand-written configs; HF-origin files only.
- **Ollama**: private manifest + `blobs/sha256-<hex>` layout. `ollama create` copies, BUT skips the copy when
  `blobs/sha256-<hex>` already exists -- so a hardlink placed there first makes the import free (from Ollama's source;
  to be verified on rome, phase 0).
- **ComfyUI**: `extra_model_paths.yaml` with a `base_path`; follows symlinks.
- **OCI (CNCF ModelPack, Docker Model Runner, KitOps)**: distribution formats, not a disk layout several engines read;
  not consumed by Ollama. Skip.

## 3. The design

**One root on the root filesystem** (it already holds `/home` and `/var`, so hardlinks work everywhere):

```
/srv/models/                         owner ronald, group `models` (ollama in it); 2 TB free today
  store/                             THE registry: LLM_repo's content-addressed store (moved from ~/llm-mirror)
                                     blobs/sha256/..  repos/<publisher>/<repo>/manifests  index.sqlite
  llm/<publisher>/<repo>/<file>      readable view: HARDLINKS to store blobs, generated, never edited by hand
  llm/rome/<name>/<file>             models BUILT on the rig (the GLM hybrid, requants), ingested as source "local"
  comfy/<type>/<file>                ComfyUI's folders, same hardlink view (extra_model_paths.yaml -> here)
  ollama/                            OLLAMA_MODELS; every import hardlinked into blobs/sha256-<hex> before `ollama create`
  deployments.yaml                   which engine serves which model (by publisher/repo/file + sha256) with which settings
```

Rules:
1. **Every model file enters through the registry** -- a download (HF / Civitai) or an import of a local file
   (`rome/...`). It gets a sha256, an origin, a date. Nothing else writes model files.
2. **Engines get readable paths from `llm/` (or `comfy/`)**, never a blob path, never `~/models`.
3. **`deployments.yaml` is the one place that says who uses what.** From it are generated: llama-swap's model entries,
   the Franken arms' GGUF paths, the Ollama names, and the sha256 list the Franken launcher checks (SEC6's
   `served.sha256` becomes generated from the registry instead of hand-kept).
4. **Delete = one action in the console**, refused while `deployments.yaml` names the model; the blob goes when no link
   is left.
5. **Zero copies:** a file is stored once; the view, Ollama and ComfyUI hold hardlinks to the same inode.

**The console** (LLM_repo, `:8002`) becomes the face of it: Download (search HF, pick a quant, progress), Import (a
file built on the rig), one table of every model with size, origin and **used by** (Franken arm / llama-swap entry /
Ollama name / ComfyUI), and actions: serve with Franken (supported families only -- see below), add to llama-swap,
add to Ollama, delete when unused. Disk use and free space on top.

**What "a new model for the Franken engine" means, honestly:** the engine runs three families (GLM-5.3, DeepSeek-V4,
Qwen3.8); another architecture is engine work, not a download. A new FILE of a supported family (another quant) needs
its gate before it serves -- acceptance, decode / prefill numbers, the quality check -- because a quant can be broken
or slow on this engine. So: download in the console -> the console shows "Franken: GLM family, not gated" -> the gate
runs (today: ask the session; later a console action that queues the gate chain) -> on PASS it becomes a Serve option
and its sha256 is recorded. Ollama and llama-swap need no gate.

## 4. Phases

| phase | what | root? | risk |
|---|---|---|---|
| 0 | verify the Ollama hardlink import on rome with a small model; measure that a hardlinked view survives the mirror's GC | no | none |
| 1 | `/srv/models` + group `models`; move `~/llm-mirror` to `store/` (same FS: a rename, seconds); OLLAMA_MODELS -> `ollama/` (move the 5 models) | **one sudo script** | low; rollback = move back |
| 2 | LLM_repo: the `llm/` view generator, `local` import, `deployments.yaml` + generators (llama-swap, Franken paths, sha256 list) | no | low |
| 3 | import what serves today: the GLM hybrid, GLM UD-IQ4_XS, DS4 UD-IQ2_M, Qwen3.8 UD-IQ4_XS (+ the FP8 config dirs the gateway reads) as `rome/...` / HF repos; point the Franken arms and llama-swap at `llm/`; re-gate GLM (acceptance only, same bytes, new path) | no | medium: the served paths change -- gated |
| 4 | console: Download, Import, used-by table, delete; ComfyUI under `comfy/` via extra_model_paths.yaml | no | low |
| 5 | `~/models` leftovers (~600 GB): import what is worth keeping as `rome/...`, delete the rest -- **owner decision** | no | data loss if wrong -> owner decides per directory |

Phases 0-2 and 4 do not touch what serves; phase 3 is the only one that does, and it is gated like any install.

## 5. Owner decisions

- **D1** the root: `/srv/models` (proposed) or keep it under `~/models`.
- **D2** ComfyUI in the same store (proposed yes) or left alone.
- **D3** the ~600 GB of research leftovers in `~/models`: per directory keep (import) or delete.
- **D4** the mirror's HF server for other LAN machines: it is loopback-only since SEC1; reopen it (with a key) only if
  another machine should download from rome.
