#!/bin/sh
set -eu

ensure_curl() {
  if command -v curl >/dev/null 2>&1; then
    return
  fi
  echo "curl command missing; installing"
  apt-get update >/dev/null
  apt-get install -y curl >/dev/null
}

pull_model() {
  name="$1"
  if ollama show "$name" >/dev/null 2>&1; then
    echo "Model $name already present"
  else
    echo "Pulling $name"
    ollama pull "$name"
  fi
}

create_model() {
  name="$1"
  file="$2"
  if ollama show "$name" >/dev/null 2>&1; then
    echo "Model $name already present"
  else
    echo "Creating $name from $file"
    if ollama create "$name" -f "$file"; then
      echo "Model $name created"
    else
      echo "Warning: failed to create $name" >&2
    fi
  fi
}

download_qwen3_model() {
  dest_dir="/root/.ollama/models/qwen3-235b-int8"
  final_gguf="$dest_dir/Qwen3-235B-A22B-Instruct-2507-128x10B-Q4_K_M.gguf"
  if [ -f "$final_gguf" ]; then
    echo "Qwen3-235B base GGUF already materialized"
    return
  fi

  echo "Preparing Qwen3-235B INT8 shards"
  mkdir -p "$dest_dir"
  base_url="https://huggingface.co/Intel/Qwen3-235B-A22B-Instruct-2507-gguf-q4km-AutoRound/resolve/main"
  for idx in 01 02 03; do
    shard_name="Qwen3-235B-A22B-Instruct-2507-128x10B-Q4_K_M-000${idx}-of-00003.gguf"
    shard_path="$dest_dir/$shard_name"
    size=$(stat -c%s "$shard_path" 2>/dev/null || echo 0)
    if [ "$size" -lt 1000000 ]; then
      echo "Downloading $shard_name"
      curl -fSL "$base_url/$shard_name?download=1" -o "$shard_path"
      size=$(stat -c%s "$shard_path" 2>/dev/null || echo 0)
      if [ "$size" -lt 1000000 ]; then
        echo "Shard $shard_name appears truncated after download (size=$size)." >&2
        exit 1
      fi
    else
      echo "Shard $shard_name already present"
    fi
  done

  echo "Merging shards into GGUF (this may take a while)"
  cat "$dest_dir"/Qwen3-235B-A22B-Instruct-2507-128x10B-Q4_K_M-000*.gguf > "$final_gguf"
  echo "Combined GGUF saved to $final_gguf"
}

pull_model "smollm2:1.7b"
create_model "emotion-english-distilroberta-base" "/modelfiles/emotion-english-distilroberta-base/Modelfile"
create_model "twitter-roberta-base-emotion" "/modelfiles/twitter-roberta-base-emotion/Modelfile"
create_model "twitter-roberta-base-sentiment-latest" "/modelfiles/twitter-roberta-base-sentiment-latest/Modelfile"

if [ "${ENABLE_QWEN3_235B:-1}" = "1" ]; then
  ensure_curl
  download_qwen3_model
  create_model "qwen3-235b-int8" "/modelfiles/qwen3-235b-int8/Modelfile"
else
  echo "Skipping qwen3-235b-int8 preload (set ENABLE_QWEN3_235B=1 to enable)"
fi

echo "Ollama model preload complete"
