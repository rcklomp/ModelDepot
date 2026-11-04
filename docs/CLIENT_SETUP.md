# Client Setup

How to point each inference engine at the mirror. Every engine that builds on
`huggingface_hub` needs only **two environment variables**; the rest are
URL-based or local-file paths.

Set these once (in `~/.bashrc`, `~/.zshrc`, or per-shell):

```bash
export HF_ENDPOINT=http://<mirror-host>:<port>     # e.g. http://synology:8000
export HF_HUB_DISABLE_XET=1                         # avoid the Xet chunk protocol
```

> Replace `<mirror-host>:<port>` with your mirror's address (the hostname or IP
> of the NAS/mini PC, port 8000 by default, or 443 if fronted by DSM's
> reverse proxy with TLS — in which case use `https://`).

---

## Engines that honor HF_ENDPOINT (the easy ones)

These all call `huggingface_hub` under the hood, so the two env vars above are
the entire configuration. No per-engine patching.

### transformers / diffusers

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
# These resolve through HF_ENDPOINT automatically.
tok = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-Instruct-v0.2")
model = AutoModelForCausalLM.from_pretrained("mistralai/Mistral-7B-Instruct-v0.2")
```

```python
from diffusers import StableDiffusionPipeline
pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5")
```

### vLLM

```bash
export HF_ENDPOINT=http://synology:8000
export HF_HUB_DISABLE_XET=1
vllm serve mistralai/Mistral-7B-Instruct-v0.2 --port 8001
```

`--model` accepts a repo id; vLLM resolves it through `huggingface_hub`, which
honors `HF_ENDPOINT`.

### TGI (Text Generation Inference)

```bash
export HF_ENDPOINT=http://synology:8000
export HF_HUB_DISABLE_XET=1
docker run --gpus all -p 8080:80 \
  -e HF_ENDPOINT=$HF_ENDPOINT -e HF_HUB_DISABLE_XET=1 \
  ghcr.io/huggingface/text-generation-inference:latest \
  --model-id mistralai/Mistral-7B-Instruct-v0.2
```

### MLX (Apple Silicon)

```bash
export HF_ENDPOINT=http://synology:8000
export HF_HUB_DISABLE_XET=1
python -m mlx_lm.generate --model mlx-community/Llama-3.2-1B-Instruct-4bit \
  --prompt "hello"
```

`mlx_lm` uses `huggingface_hub`, so it honors `HF_ENDPOINT`.

### huggingface_hub CLI

```bash
export HF_ENDPOINT=http://synology:8000
export HF_HUB_DISABLE_XET=1
hf download mistralai/Mistral-7B-Instruct-v0.2 \
  --local-dir ./mistral
```

---

## Ollama

Ollama pulls GGUFs from Hugging Face via the `hf.co/<user>/<repo>:<file>` form,
and **honors `HF_ENDPOINT`** for that resolution:

```bash
export HF_ENDPOINT=http://synology:8000
export HF_HUB_DISABLE_XET=1
ollama pull hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M
ollama run hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M
```

The `:<quantization>` selects the GGUF file within the repo. For airgapped use
(no HF endpoint at all), you can also load a local file:

```bash
# Download the GGUF from the mirror directly, then create from the local path:
curl -O http://synology:8000/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf
ollama create myllama -f - <<EOF
FROM ./Llama-3.2-1B-Instruct-Q4_K_M.gguf
EOF
```

---

## llama.cpp

`llama-server` loads a model from a local path or a direct URL. Point it at the
mirror's `/resolve/` endpoint:

```bash
# Download the GGUF from the mirror at startup:
./llama-server --model-url http://synology:8000/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf

# Or load a file you already pulled:
curl -O http://synology:8000/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf
./llama-server -m ./Llama-3.2-1B-Instruct-Q4_K_M.gguf
```

llama.cpp does **not** use `huggingface_hub`; it just streams the URL, so
`HF_ENDPOINT` is irrelevant here — use the full `/resolve/` URL.

---

## LM Studio (the documented exception)

LM Studio has its own internal model catalog and does not honor `HF_ENDPOINT`.
The supported path is **local-file import**: pull the GGUF from the mirror, then
import it into LM Studio.

```bash
# Pull the GGUF from the mirror:
curl -O http://synology:8000/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf
```

Then in LM Studio: **folder icon → My Models → Import** (point at the downloaded
`.gguf`). See LM Studio's
[import-model docs](https://lmstudio.ai/docs/app/advanced/import-model). We do
not emulate LM Studio's proprietary catalog — that's intentional.

---

## Verifying it works

Quick smoke test from any client machine:

```bash
# 1. Metadata endpoint:
curl -s http://synology:8000/api/models/mistralai/Mistral-7B-Instruct-v0.2 | jq .sha

# 2. Resolve a small file and check the three HF headers are present:
curl -sI http://synology:8000/mistralai/Mistral-7B-Instruct-v0.2/resolve/main/config.json \
  | grep -iE 'X-Repo-Commit|ETag|Content-Length'

# 3. Full client flow (transformers):
HF_ENDPOINT=http://synology:8000 HF_HUB_DISABLE_XET=1 \
  python -c "from transformers import AutoConfig; print(AutoConfig.from_pretrained('mistralai/Mistral-7B-Instruct-v0.2').model_type)"
```

If step 3 prints `mistral`, the mirror is serving correctly and the engine is
consuming it.

---

## Datasets

The same two variables. A dataset is addressed like on the Hub:

```bash
hf download HuggingFaceH4/ultrachat_200k --repo-type dataset
```
```python
from datasets import load_dataset
ds = load_dataset("HuggingFaceH4/ultrachat_200k")       # resolves through HF_ENDPOINT
```

## When the server has a token (`MIRROR_AUTH_TOKEN`)

```bash
export HF_TOKEN=<the token>          # huggingface_hub sends it as "Authorization: Bearer ..."
```

`curl` and llama.cpp's `--model-url` need the header too: `curl -H "Authorization: Bearer <token>" ...`.

## A model is not there

The server answers 404 and remembers the request: open the GUI, the Library shows it under "Wanted" with a Download button.
