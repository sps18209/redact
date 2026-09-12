"""The deface backend: face blurring for images and video.

Real face detection is deface's own concern (and needs real photographs), so
the detector is stubbed where the assertion is about *our* wiring — bbox
translation, mode mapping, output naming — and exercised for real where the
assertion is that the pipeline runs end to end.
"""

import zipfile

import pytest

from redact import RedactionOptions, RedactionSuite
from redact.backends.anonymizer import AnonymizerBackend
from redact.backends.deface import FACE_ENTITY, DefaceBackend
from redact.document import Document
from redact.types import MediaType, RedactionMode

deface_lib = pytest.importorskip("deface", reason="deface extra not installed")
np = pytest.importorskip("numpy")
iio = pytest.importorskip("imageio.v2")


@pytest.fixture
def image(tmp_path):
    rng = np.random.default_rng(0)
    path = tmp_path / "photo.png"
    iio.imwrite(path, rng.integers(0, 255, (192, 240, 3)).astype("uint8"))
    return path


@pytest.fixture
def video(tmp_path):
    rng = np.random.default_rng(0)
    # must be large enough to contain the stubbed detection boxes
    frame = rng.integers(0, 255, (192, 240, 3)).astype("uint8")
    path = tmp_path / "clip.mp4"
    writer = iio.get_writer(path, fps=10, macro_block_size=1)
    for _ in range(6):
        writer.append_data(frame)
    writer.close()
    return path


@pytest.fixture
def stub_detector(monkeypatch):
    """Force two detections so the entity/bbox path is deterministic."""
    from redact.backends import deface as mod

    class _Stub:
        calls = 0

        def __call__(self, frame, threshold):
            type(self).calls += 1
            dets = np.array([[10, 20, 60, 90, 0.91], [100, 30, 140, 80, 0.55]], dtype="float32")
            return dets, None

    _Stub.calls = 0
    monkeypatch.setitem(mod._MODEL_CACHE, "instance", _Stub())
    return _Stub


def test_backend_is_available_and_outranks_the_legacy_anonymizer():
    assert DefaceBackend().is_available()
    assert DefaceBackend.priority > AnonymizerBackend.priority


def test_supported_media_types():
    b = DefaceBackend()
    assert b.supports(MediaType.IMAGE) and b.supports(MediaType.VIDEO)
    assert not b.supports(MediaType.PDF)


def test_detections_become_entities_with_bboxes(tmp_path, image, stub_detector):
    res = DefaceBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path == tmp_path / "out" / "photo.redacted.png"
    assert [e.entity_type for e in res.entities] == [FACE_ENTITY, FACE_ENTITY]
    # bbox is (x, y, w, h) derived from CenterFace's (x1, y1, x2, y2)
    assert res.entities[0].bbox == (10, 20, 50, 70)
    assert res.entities[1].bbox == (100, 30, 40, 50)
    assert res.entities[0].score == pytest.approx(0.91, abs=1e-4)
    assert "2 face(s) blurred" in res.message


def test_blurring_actually_changes_the_pixels(tmp_path, image, stub_detector):
    before = iio.imread(image)
    res = DefaceBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    after = iio.imread(res.output_path)
    assert after.shape == before.shape
    # the detected region changed, and the rest of the frame did not
    assert not np.array_equal(before[20:90, 10:60], after[20:90, 10:60])
    assert np.array_equal(before[150:, 200:], after[150:, 200:])
    assert iio.imread(image).tolist() == before.tolist()  # source untouched


@pytest.mark.parametrize(
    "mode,verb",
    [
        (RedactionMode.BLUR, "blurred"),
        (RedactionMode.MASK, "pixelated"),
        (RedactionMode.REDACT, "blacked out"),
        (RedactionMode.REPLACE, "blurred"),
    ],
)
def test_modes_map_to_deface_strategies(tmp_path, image, stub_detector, mode, verb):
    res = DefaceBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / mode.value, mode=mode),
    )
    assert res.success, res.message
    assert verb in res.message


def test_dry_run_writes_nothing(tmp_path, image):
    res = DefaceBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE), RedactionOptions(dry_run=True)
    )
    assert res.success and res.output_path is None
    assert list(tmp_path.iterdir()) == [image]


def test_entity_filter_without_face_skips_the_file(tmp_path, image):
    res = DefaceBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out", entities=["EMAIL_ADDRESS"]),
    )
    assert res.success and res.output_path is None
    assert "not in the requested entity types" in res.message


def test_unreadable_input_is_a_failed_result_not_an_exception(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image")
    res = DefaceBackend().redact(
        Document(path=bad, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success is False and "deface failed" in res.message


def test_video_runs_and_reports_frame_counts(tmp_path, video, stub_detector):
    res = DefaceBackend().redact(
        Document(path=video, media_type=MediaType.VIDEO),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path.name == "clip.redacted.mp4" and res.output_path.stat().st_size > 0
    # two stubbed detections per frame, across every frame of the clip
    assert "12 face detection(s) across 6 frame(s) blurred" in res.message
    assert res.entities == []  # per-frame rows would be noise, totals go in the message


def test_real_detector_runs_end_to_end(tmp_path, image):
    """No stub: the bundled ONNX model must load and run offline."""
    res = DefaceBackend().redact(
        Document(path=image, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path.exists()
    assert all(e.entity_type == FACE_ENTITY and e.bbox for e in res.entities)


def test_model_is_loaded_once_per_process(tmp_path, image):
    from redact.backends import deface as mod

    mod._MODEL_CACHE.clear()
    backend = DefaceBackend()
    for i in range(3):
        backend.redact(
            Document(path=image, media_type=MediaType.IMAGE),
            RedactionOptions(output_dir=tmp_path / f"o{i}"),
        )
    assert list(mod._MODEL_CACHE) == ["instance"]


def test_suite_auto_routes_images_and_video_to_deface(tmp_path, image, video):
    suite = RedactionSuite()
    for src in (image, video):
        res = suite.redact_path(src, RedactionOptions(output_dir=tmp_path / "out"))
        assert res.success, res.message
        assert res.backend == "deface"


def test_docx_blur_uses_deface_for_embedded_images(tmp_path, monkeypatch):
    """--docx-images blur now works out of the box once deface is installed."""
    import io

    import test_docx
    from test_docx import make_docx

    # test_docx's images are stdlib-only placeholders; blurring needs real ones.
    rng = np.random.default_rng(2)
    pixels = rng.integers(0, 255, (64, 64, 3)).astype("uint8")
    png, jpeg = io.BytesIO(), io.BytesIO()
    iio.imwrite(png, pixels, format="PNG")
    iio.imwrite(jpeg, pixels, format="JPEG")
    monkeypatch.setattr(test_docx, "IMAGE", png.getvalue())
    monkeypatch.setattr(test_docx, "JPEG", jpeg.getvalue())

    docx = make_docx(tmp_path / "memo.docx")
    res = RedactionSuite().redact_path(
        docx, RedactionOptions(output_dir=tmp_path / "out", docx_images="blur")
    )
    assert res.success, res.message
    assert "2 embedded image(s) blurred" in res.message  # blurred, not stripped
    with zipfile.ZipFile(res.output_path) as zf:
        names = zf.namelist()
        # both kept their own format, so nothing was renamed to a blank PNG
        assert "word/media/photo.jpeg" in names and "word/media/photo.png" not in names
        out = iio.imread(io.BytesIO(zf.read("word/media/image1.png")))
        assert out.shape == pixels.shape  # a real image came back through deface
