"""Utility to wrap base Hugging Face checkpoints for Text Embeddings Inference."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from sentence_transformers import SentenceTransformer, models


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def wrap_model(source: str, target: Path, normalize: bool = True) -> None:
    """Persist a SentenceTransformer wrapper for ``source`` into ``target``."""

    word = models.Transformer(source, max_seq_length=512)
    pooling = models.Pooling(
        word.get_word_embedding_dimension(),
        pooling_mode_mean_tokens=True,
        pooling_mode_cls_token=False,
        pooling_mode_max_tokens=False,
    )
    modules: Iterable[models.Transformer | models.Pooling | models.Normalize] = [
        word,
        pooling,
    ]
    if normalize:
        modules = [*modules, models.Normalize()]

    model = SentenceTransformer(modules=list(modules))
    _ensure_dir(target)
    model.save(str(target))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gte-output",
        type=Path,
        default=Path("models/gte-large-st"),
        help="Output directory for thenlper/gte-large SentenceTransformer wrapper.",
    )
    parser.add_argument(
        "--legal-output",
        type=Path,
        default=Path("models/legal-bert-st"),
        help="Output directory for nlpaueb/legal-bert-base-uncased wrapper.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip wrapping if the target directory already exists.",
    )
    args = parser.parse_args()

    targets = [
        ("thenlper/gte-large", args.gte_output, True),
        ("nlpaueb/legal-bert-base-uncased", args.legal_output, False),
    ]

    for source, target, normalize in targets:
        if args.skip_existing and target.exists():
            continue
        wrap_model(source, target, normalize=normalize)


if __name__ == "__main__":
    main()

