"""Semantic search over images and video — find content by describing it.

Redaction starts with a question the filename cannot answer: *which* of these
ten thousand frames shows a whiteboard, a badge, a screen full of records? This
module embeds images and sampled video frames with CLIP into the same vector
space as English text, so a plain-language query ranks them.

It powers two things:

``redact search "a photo of an ID card" ./footage``
    Rank a corpus by how well it matches a description.

``redact run ./footage --match "whiteboard with writing"``
    Redact only the files that match, instead of the whole folder.

The index is content, not code: it is built once, cached on disk, and reused.
Heavy imports (torch, clip) happen inside methods so ``redact list`` stays fast.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .document import Document
from .types import MediaType

#: CLIP checkpoint. ViT-B/32 is the small, fast default (~340 MB).
DEFAULT_MODEL = "ViT-B/32"

#: How many frames to sample from each video when indexing.
DEFAULT_FRAMES_PER_VIDEO = 8

#: Media types that can be embedded.
VISUAL = (MediaType.IMAGE, MediaType.VIDEO)

#: Generic prompts the query competes against when scores are calibrated.
#:
#: Raw CLIP cosine similarity is *not* calibrated: it is only meaningful
#: relative to other text for the *same* image, never as an absolute number
#: across images. Measured here, random noise scored 0.2099 against "a football
#: player" while an actual photo of footballers scored 0.1972 — so thresholding
#: raw similarity ranks noise above the real thing. Scoring the query against
#: these backgrounds and taking its softmax share fixes that: the same noise
#: image drops to 0.0001 and the footballers rise to the top.
BACKGROUND_PROMPTS = (
    "a photo",
    "a screenshot",
    "random noise",
    "a blank image",
    "a document",
    "an abstract pattern",
)

#: CLIP's own logit scale, used as the softmax temperature.
_LOGIT_SCALE = 100.0


class SemanticError(Exception):
    """Raised when semantic search cannot run (missing deps, unreadable media)."""


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_dependencies() -> List[str]:
    """What semantic search needs but does not have. Cheap; never raises."""
    missing = [n for n in ("torch", "clip", "PIL") if not _module_present(n)]
    return ["clip (pip install 'redact-suite[semantic]')"] if missing else []


def weights_dir() -> Path:
    """Where CLIP weights are cached (``REDACT_CLIP_WEIGHTS_DIR`` overrides)."""
    return Path(
        os.environ.get(
            "REDACT_CLIP_WEIGHTS_DIR", Path.home() / ".cache" / "redact-suite" / "clip"
        )
    )


@dataclass(frozen=True)
class Match:
    """One ranked hit: a file, and for video the moment inside it."""

    path: Path
    score: float
    media_type: str
    frame: int = -1          # -1 for stills
    time: float = 0.0        # seconds into the video

    def describe(self) -> str:
        where = "" if self.frame < 0 else f" @ {self.time:.1f}s (frame {self.frame})"
        return f"{self.score:+.4f}  {self.path}{where}"


@dataclass
class _Entry:
    path: str
    media_type: str
    frame: int
    time: float


# -- embedding ---------------------------------------------------------------

_MODEL_CACHE = {}


class ClipEmbedder:
    """Embeds images and text into one shared vector space."""

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "cpu"):
        self.model_name = model_name
        self.device = device

    def _load(self):
        key = (self.model_name, self.device)
        if key not in _MODEL_CACHE:
            if missing_dependencies():
                raise SemanticError(
                    "semantic search needs CLIP: pip install 'redact-suite[semantic]'"
                )
            import clip  # lazy: pulls torch

            cache = weights_dir()
            cache.mkdir(parents=True, exist_ok=True)
            _MODEL_CACHE[key] = clip.load(
                self.model_name, device=self.device, download_root=str(cache)
            )
        return _MODEL_CACHE[key]

    def embed_images(self, images: Sequence) -> "object":
        """PIL images -> L2-normalised vectors, one row each."""
        import numpy as np
        import torch

        model, preprocess = self._load()
        if not images:
            return np.zeros((0, 512), dtype="float32")
        batch = torch.stack([preprocess(im) for im in images]).to(self.device)
        with torch.no_grad():
            vectors = model.encode_image(batch)
        return _normalise(vectors.cpu().numpy())

    def embed_text(self, texts: Sequence[str]) -> "object":
        import clip
        import torch

        model, _ = self._load()
        tokens = clip.tokenize(list(texts)).to(self.device)
        with torch.no_grad():
            vectors = model.encode_text(tokens)
        return _normalise(vectors.cpu().numpy())


def _normalise(matrix):
    import numpy as np

    matrix = np.asarray(matrix, dtype="float32")
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


# -- frame extraction --------------------------------------------------------

def _open_image(path: Path):
    from PIL import Image

    with Image.open(path) as im:
        return im.convert("RGB")


def _video_frames(path: Path, count: int):
    """Sample up to ``count`` frames spread evenly through a video."""
    import imageio.v2 as iio
    from PIL import Image

    frames = []
    reader = iio.get_reader(str(path))
    try:
        meta = reader.get_meta_data()
        fps = float(meta.get("fps") or 30.0)
        try:
            total = reader.count_frames()
        except Exception:
            total = 0
        if total and total > 0:
            step = max(1, total // count)
            wanted = list(range(0, total, step))[:count]
            for idx in wanted:
                try:
                    frames.append((idx, idx / fps, Image.fromarray(reader.get_data(idx))))
                except Exception:
                    break
        else:  # streams without a frame count: walk and take every Nth
            for idx, frame in enumerate(reader):
                if idx % max(1, count) == 0 and len(frames) < count:
                    frames.append((idx, idx / fps, Image.fromarray(frame)))
                if len(frames) >= count:
                    break
    finally:
        reader.close()
    return frames


# -- the index ---------------------------------------------------------------

class SemanticIndex:
    """Vectors for a corpus of images and video frames, queryable by text."""

    def __init__(self, entries: List[_Entry], vectors, model_name: str = DEFAULT_MODEL):
        self.entries = entries
        self.vectors = vectors
        self.model_name = model_name

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def files(self) -> List[str]:
        seen, out = set(), []
        for e in self.entries:
            if e.path not in seen:
                seen.add(e.path)
                out.append(e.path)
        return out

    @classmethod
    def build(
        cls,
        documents: Iterable[Document],
        embedder: Optional[ClipEmbedder] = None,
        frames_per_video: int = DEFAULT_FRAMES_PER_VIDEO,
    ) -> "SemanticIndex":
        """Embed every image and sampled video frame in ``documents``."""
        import numpy as np

        embedder = embedder or ClipEmbedder()
        entries: List[_Entry] = []
        images = []
        for doc in documents:
            if doc.media_type not in VISUAL:
                continue
            try:
                if doc.media_type is MediaType.IMAGE:
                    images.append(_open_image(doc.path))
                    entries.append(_Entry(str(doc.path), str(doc.media_type), -1, 0.0))
                else:
                    for idx, seconds, frame in _video_frames(doc.path, frames_per_video):
                        images.append(frame)
                        entries.append(
                            _Entry(str(doc.path), str(doc.media_type), idx, seconds)
                        )
            except Exception:
                continue  # an unreadable file must not abort the whole index
        vectors = (
            embedder.embed_images(images)
            if images
            else np.zeros((0, 512), dtype="float32")
        )
        return cls(entries, vectors, embedder.model_name)

    def query(
        self,
        text: str,
        embedder: Optional[ClipEmbedder] = None,
        top_k: int = 10,
        threshold: float = 0.0,
        per_file: bool = True,
        calibrate: bool = True,
    ) -> List[Match]:
        """Rank the corpus against ``text``, best first.

        With ``per_file`` (the default) a video contributes only its single
        best-matching frame, so one long clip cannot flood the results.

        With ``calibrate`` (the default) each score is the query's softmax share
        against :data:`BACKGROUND_PROMPTS` rather than a raw cosine similarity —
        comparable across images, and readable as "how much more this looks like
        the query than like nothing in particular". Pass ``calibrate=False`` for
        the raw similarity.
        """
        import numpy as np

        if not len(self):
            return []
        embedder = embedder or ClipEmbedder(self.model_name)
        vectors = np.asarray(self.vectors)
        if calibrate:
            text_vecs = embedder.embed_text([text, *BACKGROUND_PROMPTS])
            logits = (vectors @ np.asarray(text_vecs).T) * _LOGIT_SCALE
            logits -= logits.max(axis=1, keepdims=True)  # stable softmax
            exp = np.exp(logits)
            scores = exp[:, 0] / exp.sum(axis=1)
        else:
            scores = vectors @ embedder.embed_text([text])[0]

        best = {}
        matches = []
        for entry, score in zip(self.entries, scores.tolist()):
            if score < threshold:
                continue
            match = Match(Path(entry.path), float(score), entry.media_type,
                          entry.frame, entry.time)
            if per_file:
                if entry.path not in best or score > best[entry.path].score:
                    best[entry.path] = match
            else:
                matches.append(match)
        ranked = sorted(best.values() if per_file else matches,
                        key=lambda m: -m.score)
        return ranked[:top_k]

    # -- persistence ---------------------------------------------------------
    def save(self, path) -> None:
        import numpy as np

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "model": self.model_name,
            "entries": [
                {"path": e.path, "media_type": e.media_type, "frame": e.frame, "time": e.time}
                for e in self.entries
            ],
        }
        np.savez_compressed(
            path, vectors=np.asarray(self.vectors), meta=json.dumps(meta)
        )

    @classmethod
    def load(cls, path) -> "SemanticIndex":
        import numpy as np

        path = Path(path)
        if not path.is_file():
            raise SemanticError(f"no index at {path}")
        with np.load(path, allow_pickle=False) as data:
            vectors = data["vectors"]
            meta = json.loads(str(data["meta"]))
        entries = [
            _Entry(e["path"], e["media_type"], int(e["frame"]), float(e["time"]))
            for e in meta["entries"]
        ]
        return cls(entries, vectors, meta.get("model", DEFAULT_MODEL))


def filter_documents(
    documents: Iterable[Document],
    query: str,
    threshold: float,
    embedder: Optional[ClipEmbedder] = None,
    frames_per_video: int = DEFAULT_FRAMES_PER_VIDEO,
    calibrate: bool = True,
) -> List[Document]:
    """Keep the documents whose visual content matches ``query``.

    Non-visual documents (text, PDFs) are passed through untouched — a semantic
    image filter should never silently drop the text files in a mixed folder.
    """
    docs = list(documents)
    visual = [d for d in docs if d.media_type in VISUAL]
    if not visual:
        return docs
    embedder = embedder or ClipEmbedder()
    index = SemanticIndex.build(visual, embedder, frames_per_video)
    hits = {
        str(m.path)
        for m in index.query(
            query, embedder, top_k=len(index), threshold=threshold, calibrate=calibrate
        )
    }
    return [d for d in docs if d.media_type not in VISUAL or str(d.path) in hits]
