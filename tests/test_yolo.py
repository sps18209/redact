"""The YOLO backend: open-vocabulary (prompt-driven) and COCO-class redaction.

Model inference is stubbed for the wiring assertions so the suite stays fast and
offline. The tests that genuinely exercise Ultralytics and download weights are
gated behind REDACT_TEST_YOLO_WEIGHTS=1.
"""

import os
from pathlib import Path

import pytest

from redact import RedactionOptions
from redact.backends.yolo import (
    DEFAULT_CLASSES,
    DEFAULT_MODEL,
    YoloBackend,
    entity_label,
)
from redact.document import Document
from redact.types import MediaType, RedactionMode

np = pytest.importorskip("numpy")
iio = pytest.importorskip("imageio.v2")
pytest.importorskip("cv2")

WEIGHTS = os.environ.get("REDACT_TEST_YOLO_WEIGHTS") == "1"
needs_weights = pytest.mark.skipif(
    not WEIGHTS, reason="set REDACT_TEST_YOLO_WEIGHTS=1 to run real-model tests"
)


@pytest.fixture
def image(tmp_path):
    rng = np.random.default_rng(0)
    path = tmp_path / "photo.png"
    iio.imwrite(path, rng.integers(0, 255, (200, 320, 3)).astype("uint8"))
    return path


@pytest.fixture
def stub_model(monkeypatch):
    """Two fixed detections, so masking and entity mapping are deterministic."""
    from redact.backends import yolo as mod

    monkeypatch.setattr(YoloBackend, "missing_dependencies", lambda self: [])
    monkeypatch.setattr(mod, "_load_model", lambda name, wanted: ("model", {0: "LICENSE_PLATE"}))
    monkeypatch.setattr(
        mod, "_detect",
        lambda model, frame, labels, threshold: [
            (10.0, 20.0, 90.0, 60.0, 0.88, "LICENSE_PLATE"),
            (150.0, 100.0, 220.0, 150.0, 0.42, "LICENSE_PLATE"),
        ],
    )
    return mod


# -- label mapping -----------------------------------------------------------

def test_entity_label_is_distinct_per_prompt():
    assert entity_label("license plate") == "LICENSE_PLATE"
    assert entity_label("  human face ") == "HUMAN_FACE"
    assert entity_label("ID card") == "ID_CARD"
    # plates must never be folded into FACE
    assert entity_label("license plate") != "FACE"


def test_defaults_cover_plates_out_of_the_box():
    assert "license plate" in DEFAULT_CLASSES
    assert "world" in DEFAULT_MODEL  # open-vocabulary by default


def test_backend_is_below_deface_so_faces_keep_their_specialist():
    from redact.backends.deface import DefaceBackend

    assert YoloBackend.priority < DefaceBackend.priority


# -- detection -> entities ---------------------------------------------------

def test_detections_become_labelled_entities_with_bboxes(tmp_path, image, stub_model):
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path == tmp_path / "out" / "photo.redacted.png"
    assert [e.entity_type for e in res.entities] == ["LICENSE_PLATE", "LICENSE_PLATE"]
    assert res.entities[0].bbox == (10, 20, 80, 40)  # (x, y, w, h)
    assert res.entities[1].score == pytest.approx(0.42)
    assert "2x LICENSE_PLATE masked (blur)" in res.message


def test_masking_changes_only_the_detected_regions(tmp_path, image, stub_model):
    before = iio.imread(image)
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    after = iio.imread(res.output_path)
    assert not np.array_equal(before[20:60, 10:90], after[20:60, 10:90])
    assert np.array_equal(before[170:, 250:], after[170:, 250:])


@pytest.mark.parametrize(
    "mode,strategy",
    [
        (RedactionMode.BLUR, "blur"),
        (RedactionMode.MASK, "mosaic"),
        (RedactionMode.REDACT, "solid"),
        (RedactionMode.REPLACE, "blur"),
    ],
)
def test_modes_map_to_masking_strategies(tmp_path, image, stub_model, mode, strategy):
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / mode.value, mode=mode),
    )
    assert res.success and f"({strategy})" in res.message


def test_solid_mode_blacks_the_region_out(tmp_path, image, stub_model):
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out", mode=RedactionMode.REDACT),
    )
    after = iio.imread(res.output_path)
    assert after[25:55, 15:85].max() == 0  # genuinely blacked out


def test_out_of_bounds_boxes_are_clamped(tmp_path, image, monkeypatch):
    """A detection running past the frame edge must not crash or corrupt output."""
    from redact.backends import yolo as mod

    monkeypatch.setattr(YoloBackend, "missing_dependencies", lambda self: [])
    monkeypatch.setattr(mod, "_load_model", lambda n, w: ("m", {0: "LICENSE_PLATE"}))
    monkeypatch.setattr(
        mod, "_detect",
        lambda *a, **k: [(-50.0, -50.0, 9999.0, 9999.0, 0.9, "LICENSE_PLATE")],
    )
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert iio.imread(res.output_path).shape == iio.imread(image).shape


# -- configuration & failure modes -------------------------------------------

def test_class_prompts_come_from_extra(monkeypatch):
    b = YoloBackend()
    assert b._classes(RedactionOptions()) == list(DEFAULT_CLASSES)
    assert b._classes(RedactionOptions(extra={"yolo_classes": "a, b ,"})) == ["a", "b"]
    assert b._classes(RedactionOptions(extra={"yolo_classes": ["x"]})) == ["x"]


def test_model_name_from_extra_then_env(monkeypatch):
    b = YoloBackend()
    monkeypatch.delenv("REDACT_YOLO_MODEL", raising=False)
    assert b._model_name(RedactionOptions()) == DEFAULT_MODEL
    monkeypatch.setenv("REDACT_YOLO_MODEL", "env.pt")
    assert b._model_name(RedactionOptions()) == "env.pt"
    assert b._model_name(RedactionOptions(extra={"yolo_model": "x.pt"})) == "x.pt"


def test_entity_filter_narrows_prompts(tmp_path, image, stub_model):
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "o", entities=["EMAIL_ADDRESS"]),
    )
    assert res.success and res.output_path is None
    assert "no requested entity type matches" in res.message


def test_dry_run_writes_nothing(tmp_path, image, stub_model):
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None
    assert "would detect and mask" in res.message


def test_model_load_failure_is_a_failed_result(tmp_path, image, monkeypatch):
    from redact.backends import yolo as mod

    monkeypatch.setattr(YoloBackend, "missing_dependencies", lambda self: [])

    def boom(name, wanted):
        raise ValueError("model 'x.pt' cannot detect ['license plate']; it knows [...]")

    monkeypatch.setattr(mod, "_load_model", boom)
    res = YoloBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "o"),
    )
    assert res.success is False
    assert "cannot detect" in res.message  # never a silent clean run


# -- real Ultralytics (weights download) -------------------------------------

@needs_weights
def test_stock_coco_model_refuses_plates_rather_than_finding_nothing(tmp_path):
    """The headline trap: no stock YOLO — YOLO26 included — has a plate class."""
    from redact.backends.yolo import _load_model

    with pytest.raises(ValueError) as err:
        _load_model("yolo26n.pt", ["license plate"])
    assert "cannot detect" in str(err.value) and "worldv2" in str(err.value)


@needs_weights
def test_real_closed_vocabulary_detection(tmp_path):
    import ultralytics

    photo = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
    res = YoloBackend().redact(
        Document(path=photo, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "o", extra={
            "yolo_model": "yolo26n.pt", "yolo_classes": "person",
        }),
    )
    assert res.success, res.message
    assert res.entities and all(e.entity_type == "PERSON" for e in res.entities)


@needs_weights
def test_real_open_vocabulary_detection_from_a_text_prompt(tmp_path):
    import ultralytics

    photo = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
    res = YoloBackend().redact(
        Document(path=photo, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "o", threshold=0.05,
                         extra={"yolo_classes": "human face"}),
    )
    assert res.success, res.message
    assert any(e.entity_type == "HUMAN_FACE" for e in res.entities)


# -- weights cache -----------------------------------------------------------

def test_bare_names_resolve_into_the_cache_not_the_cwd(tmp_path, monkeypatch):
    """Ultralytics would download a bare name into CWD; we redirect it."""
    from redact.backends.yolo import resolve_weights, weights_dir

    monkeypatch.setenv("REDACT_YOLO_WEIGHTS_DIR", str(tmp_path / "cache"))
    resolved = resolve_weights("yolo26x.pt")
    assert resolved == str(tmp_path / "cache" / "yolo26x.pt")
    assert weights_dir().is_dir()  # created on demand


def test_explicit_paths_are_left_alone(tmp_path, monkeypatch):
    from redact.backends.yolo import resolve_weights

    monkeypatch.setenv("REDACT_YOLO_WEIGHTS_DIR", str(tmp_path / "cache"))
    assert resolve_weights("/models/custom.pt") == "/models/custom.pt"


def test_an_existing_local_file_is_used_as_is(tmp_path, monkeypatch):
    from redact.backends.yolo import resolve_weights

    monkeypatch.setenv("REDACT_YOLO_WEIGHTS_DIR", str(tmp_path / "cache"))
    local = tmp_path / "plate.pt"
    local.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert resolve_weights("plate.pt") == "plate.pt"


# -- checkpoints must never land in the user's working directory --------------

def test_redirect_clip_cache_never_raises(monkeypatch, tmp_path):
    """Placement is a convenience; it must not be able to fail a redaction."""
    from redact.backends import yolo

    monkeypatch.setenv("REDACT_YOLO_WEIGHTS_DIR", str(tmp_path / "cache"))
    yolo._redirect_clip_cache()  # ultralytics present or not, this must return


def test_clip_text_encoder_is_redirected_out_of_the_cwd(monkeypatch, tmp_path):
    """YOLO-World's set_classes embeds prompts with CLIP, which Ultralytics
    fetches to WEIGHTS_DIR/clip. That setting defaults to the *relative* string
    "weights", so a 338 MB encoder lands wherever `redact` was run — measured
    here as ./weights/clip/ViT-B-32.pt inside the repo itself.
    """
    text_model = pytest.importorskip("ultralytics.nn.text_model")
    from redact.backends import yolo

    cache = tmp_path / "cache"
    monkeypatch.setenv("REDACT_YOLO_WEIGHTS_DIR", str(cache))
    monkeypatch.setattr(text_model, "WEIGHTS_DIR", Path("weights"), raising=False)

    yolo._redirect_clip_cache()

    assert Path(text_model.WEIGHTS_DIR).is_absolute(), "a relative dir resolves against the CWD"
    assert Path(text_model.WEIGHTS_DIR) == cache
