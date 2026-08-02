"""Local embeddings-based semantic search over the configured workspace.

No API key required — uses a small local sentence-transformers model. The
explorer node can call `semantic_search` instead of blindly `list_dir`/
`read_file`-ing its way through every file, which is what actually lets
this harness scale to a repo bigger than a demo folder.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from langchain_core.tools import tool

import tools

_MODEL_NAME = "all-MiniLM-L6-v2"
_CHUNK_LINES = 40
_CHUNK_OVERLAP = 8
_TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".sql", ".sh", ".html", ".css",
}
_MAX_FILE_BYTES = 300_000  # skip anything larger; not worth chunking blindly

_model = None
_index_cache: dict[Path, tuple[np.ndarray, list[dict]]] = {}


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _iter_chunkable_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in tools.IGNORED_PARTS for part in path.parts):
            continue
        if path.suffix not in _TEXT_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _chunk_file(root: Path, path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    if not lines:
        return []
    rel = path.relative_to(root).as_posix()
    chunks = []
    start = 0
    step = _CHUNK_LINES - _CHUNK_OVERLAP
    while start < len(lines):
        end = min(start + _CHUNK_LINES, len(lines))
        text = "\n".join(lines[start:end])
        if text.strip():
            chunks.append(
                {
                    "file_path": rel,
                    "start_line": start + 1,
                    "end_line": end,
                    "text": text,
                }
            )
        if end == len(lines):
            break
        start += step
    return chunks


def _build_index(root: Path) -> tuple[np.ndarray, list[dict]]:
    chunks: list[dict] = []
    for path in _iter_chunkable_files(root):
        chunks.extend(_chunk_file(root, path))

    if not chunks:
        return np.zeros((0, 384), dtype=np.float32), []

    model = _get_model()
    texts = [c["text"] for c in chunks]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(embeddings, dtype=np.float32), chunks


def _get_index(root: Path) -> tuple[np.ndarray, list[dict]]:
    if root not in _index_cache:
        _index_cache[root] = _build_index(root)
    return _index_cache[root]


def invalidate_index(root: Path | None = None) -> None:
    """Call after files change (e.g. a diff was applied) so the next search re-indexes."""
    if root is None:
        _index_cache.clear()
    else:
        _index_cache.pop(root, None)


@tool
def semantic_search(query: str, top_k: int = 5) -> str:
    """Search the workspace by meaning, not exact text — use this to find
    relevant code before reading files one by one, especially in a repo too
    large to read in full. Returns the top matching chunks with file paths
    and line ranges; use read_file on a hit to see the full surrounding code.
    """
    root = tools.WORKSPACE_ROOT
    embeddings, chunks = _get_index(root)
    if not chunks:
        return "ERROR: no searchable text files found in the workspace."

    model = _get_model()
    query_vec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
    scores = embeddings @ query_vec
    top_k = max(1, min(top_k, len(chunks)))
    top_indices = np.argsort(-scores)[:top_k]

    results = []
    for idx in top_indices:
        chunk = chunks[idx]
        results.append(
            f"{chunk['file_path']}:{chunk['start_line']}-{chunk['end_line']} "
            f"(score {scores[idx]:.2f})\n{chunk['text']}"
        )
    return "\n\n---\n\n".join(results)
