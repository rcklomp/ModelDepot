#!/usr/bin/env bash
set -euo pipefail
export VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/radeon_icd.json
export PATH="/opt/rocm/bin:$PATH"
LLAMA=/home/ronald/src/llama.cpp/build/bin/llama-server
MODEL="${MODEL_PATH:-}"
ALIAS="${MODEL_ALIAS:-qwen3.6-35b}"
PORT="${PORT:-8080}"
CTX="${CTX_SIZE:-262144}"
GPU_LAYERS="${GPU_LAYERS:-99}"
DEVICES="${DEVICES:-Vulkan0,Vulkan1,Vulkan2}"
if [ -z "$MODEL" ]; then echo "ERROR: MODEL_PATH not set" >&2; exit 1; fi
exec "$LLAMA" \
  --model "$MODEL" --alias "$ALIAS" \
  --host 0.0.0.0 --port "$PORT" \
  --n-gpu-layers "$GPU_LAYERS" \
  --device "$DEVICES" --split-mode layer --tensor-split 1,1,1 \
  --ctx-size "$CTX" --flash-attn on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --parallel 1 --cont-batching
