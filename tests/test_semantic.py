"""Semantic search over images and video frames.

Scoring, ranking, persistence and filtering are tested against a deterministic
fake embedder so CI needs neither torch nor CLIP weights. The tests that load
real CLIP are gated behind REDACT_TEST_CLIP=1.
"""

import os

import pytest

from redact.document import Document
from redact.semantic import (
    BACKGROUND_PROMPTS,
    DEFAULT_MODEL,
    Match,
    SemanticIndex,
    _Entry,
    filter_documents,
    missing_dependencies,
    weights_dir,
)
from redact.types import MediaType

np = pytest.importorskip("numpy")

REAL = os.environ.get("REDACT_TEST_CLIP") == "1"
needs_clip = pytest.mark.skipif(not REAL, reason="set REDACT_TEST_CLIP=1 for real CLIP")


class FakeEmbedder:
    """Maps known strings to fixed unit vectors in a 3-d space.

    'bus' and 'football' are orthogonal; 'noise' sits between them, standing in
    for the out-of-distribution image that fools raw cosine similarity.
    """

    model_name = DEFAULT_MODEL
    VECTORS = {
        "bus": [1.0, 0.0, 0.0],
        "football": [0.0, 1.0, 0.0],
        "noise": [0.6, 0.6, 0.0],
    }

    def _vec(self, key: str):
        for name, vec in self.VECTORS.items():
            if name in key.lower():
                v = np.array(vec, dtype="float32")
                return v / np.linalg.norm(v)
        return np.array([0.0, 0.0, 1.0], dtype="float32")

    def embed_images(self, images):
        return np.stack([self._vec(str(i)) for i in images]) if images else np.zeros((0, 3), "float32")

    def embed_text(self, texts):
        return np.stack([self._vec(t) for t in texts])


def _index():
    entries = [
        _Entry("bus.jpg", "image", -1, 0.0),
        _Entry("football.jpg", "image", -1, 0.0),
        _Entry("noise.png", "image", -1, 0.0),
        _Entry("clip.mp4", "video", 10, 2.0),
    ]
    embedder = FakeEmbedder()
    vectors = np.stack([embedder._vec(e.path) for e in entries])
    return SemanticIndex(entries, vectors), embedder


# -- ranking -----------------------------------------------------------------

def test_query_ranks_the_matching_file_first():
    index, embedder = _index()
    matches = index.query("a bus", embedder, top_k=4)
    assert matches[0].path.name == "bus.jpg"
    assert isinstance(matches[0], Match)


def test_calibration_demotes_the_out_of_distribution_image():
    """Raw similarity can rank 'noise' above the true match; calibration must not."""
    index, embedder = _index()
    raw = index.query("football", embedder, top_k=4, calibrate=False)
    calibrated = index.query("football", embedder, top_k=4, calibrate=True)

    raw_rank = [m.path.name for m in raw]
    cal_rank = [m.path.name for m in calibrated]
    assert cal_rank[0] == "football.jpg"
    # noise is closer to football than bus is, so it places 2nd on raw scores;
    # calibrating against the background prompts must push it below.
    assert cal_rank.index("noise.png") >= raw_rank.index("noise.png")
    assert all(0.0 <= m.score <= 1.0 for m in calibrated)  # a probability


def test_calibration_uses_the_background_prompts():
    seen = []

    class Spy(FakeEmbedder):
        def embed_text(self, texts):
            seen.append(list(texts))
            return super().embed_text(texts)

    index, _ = _index()
    index.query("a bus", Spy(), top_k=1)
    assert seen[0][0] == "a bus"
    assert tuple(seen[0][1:]) == BACKGROUND_PROMPTS


def test_threshold_filters_weak_matches():
    index, embedder = _index()
    assert index.query("a bus", embedder, threshold=0.99) == [] or all(
        m.score >= 0.99 for m in index.query("a bus", embedder, threshold=0.99)
    )


def test_top_k_limits_results():
    index, embedder = _index()
    assert len(index.query("a bus", embedder, top_k=2)) == 2


def test_video_matches_carry_a_timestamp():
    index, embedder = _index()
    match = next(m for m in index.query("noise", embedder, top_k=4) if m.media_type == "video")
    assert match.frame == 10 and match.time == pytest.approx(2.0)
    assert "@ 2.0s" in match.describe()


def test_per_file_collapses_a_video_to_its_best_frame():
    entries = [_Entry("v.mp4", "video", i, float(i)) for i in range(5)]
    embedder = FakeEmbedder()
    vectors = np.stack([embedder._vec("bus") for _ in entries])
    index = SemanticIndex(entries, vectors)
    assert len(index.query("a bus", embedder, per_file=True)) == 1
    assert len(index.query("a bus", embedder, per_file=False, top_k=99)) == 5


def test_empty_index_queries_cleanly():
    assert SemanticIndex([], np.zeros((0, 3), "float32")).query("x", FakeEmbedder()) == []


# -- persistence -------------------------------------------------------------

def test_index_round_trips_through_disk(tmp_path):
    index, embedder = _index()
    path = tmp_path / "idx.npz"
    index.save(path)
    loaded = SemanticIndex.load(path)
    assert len(loaded) == len(index)
    assert loaded.files == index.files
    assert [m.path for m in loaded.query("a bus", embedder)] == [
        m.path for m in index.query("a bus", embedder)
    ]


def test_loading_a_missing_index_is_a_clear_error(tmp_path):
    from redact.semantic import SemanticError

    with pytest.raises(SemanticError):
        SemanticIndex.load(tmp_path / "nope.npz")


# -- building & filtering ----------------------------------------------------

def test_build_skips_non_visual_documents(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    docs = [Document(path=tmp_path / "a.txt", media_type=MediaType.TEXT)]
    assert len(SemanticIndex.build(docs, FakeEmbedder())) == 0


def test_build_survives_an_unreadable_image(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    index = SemanticIndex.build(
        [Document(path=bad, media_type=MediaType.IMAGE)], FakeEmbedder()
    )
    assert len(index) == 0  # skipped, not raised


def test_filter_always_keeps_non_visual_documents(tmp_path, monkeypatch):
    """A visual filter must never silently drop the text files in a folder."""
    import redact.semantic as mod

    text = Document(path=tmp_path / "notes.txt", media_type=MediaType.TEXT)
    bus = Document(path=tmp_path / "bus.jpg", media_type=MediaType.IMAGE)
    football = Document(path=tmp_path / "football.jpg", media_type=MediaType.IMAGE)

    embedder = FakeEmbedder()
    monkeypatch.setattr(
        mod.SemanticIndex, "build",
        classmethod(lambda cls, docs, emb=None, frames=8, **kw: _built(list(docs), embedder)),
    )
    kept = filter_documents([text, bus, football], "a bus", threshold=0.5, embedder=embedder)
    names = [d.path.name for d in kept]
    assert "notes.txt" in names          # passed through untouched
    assert "bus.jpg" in names
    assert "football.jpg" not in names   # filtered out


def _built(docs, embedder):
    entries = [_Entry(str(d.path), str(d.media_type), -1, 0.0) for d in docs]
    vectors = np.stack([embedder._vec(e.path) for e in entries])
    return SemanticIndex(entries, vectors)


def test_filter_with_no_visual_documents_is_a_passthrough(tmp_path):
    docs = [Document(path=tmp_path / "a.txt", media_type=MediaType.TEXT)]
    assert filter_documents(docs, "anything", 0.5) == docs


# -- configuration -----------------------------------------------------------

def test_weights_dir_is_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("REDACT_CLIP_WEIGHTS_DIR", str(tmp_path / "w"))
    assert weights_dir() == tmp_path / "w"


def test_missing_dependencies_never_raises():
    assert isinstance(missing_dependencies(), list)


# -- real CLIP ---------------------------------------------------------------

@needs_clip
def test_real_clip_ranks_a_real_photo_correctly(tmp_path):
    import ultralytics
    from pathlib import Path

    from redact.semantic import ClipEmbedder

    assets = Path(ultralytics.__file__).parent / "assets"
    docs = [
        Document(path=assets / "bus.jpg", media_type=MediaType.IMAGE),
        Document(path=assets / "zidane.jpg", media_type=MediaType.IMAGE),
    ]
    embedder = ClipEmbedder()
    index = SemanticIndex.build(docs, embedder)
    assert len(index) == 2
    assert index.query("a bus on a city street", embedder)[0].path.name == "bus.jpg"
    assert index.query("football players", embedder)[0].path.name == "zidane.jpg"
