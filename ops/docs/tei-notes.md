# Text-Embeddings-Inference (TEI) Notes

The compose stack references `ghcr.io/huggingface/text-embeddings-inference:cpu-0.7`,
which is not published upstream. Build the local image tag before starting services:

```bash
docker pull ghcr.io/huggingface/text-embeddings-inference:cpu-1.6
docker tag ghcr.io/huggingface/text-embeddings-inference:cpu-1.6 \
  ghcr.io/huggingface/text-embeddings-inference:cpu-0.7
```

Wrapped checkpoints live under `models/` (ignored by git). Regenerate them with:

```bash
python ../ops/tei/wrap_models.py  # wraps thenlper/gte-large and nlpaueb/legal-bert-base-uncased
```

Update `TEI_GTE_LARGE_URL`, `TEI_LEGAL_BERT_URL`, and any additional encoder URLs
in the environment before restarting the stack. LiteLLM and the runtime loaders
resolve endpoints exclusively through these variables.
