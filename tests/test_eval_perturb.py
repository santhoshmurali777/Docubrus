"""The eval harness itself: perturbations must yield valid image-only PDFs and be deterministic."""
import os

import pytest

from app.documents.pdf_text import read_pdf
from eval.perturb import NAMES, degrade
from tests.helpers import ROOT

SRC = open(os.path.join(ROOT, "data", "synthetic", "clean", "land_deed.pdf"), "rb").read()


@pytest.mark.parametrize("name", NAMES)
def test_perturbation_gives_an_image_only_pdf(name):
    out = degrade(SRC, name, 3)
    assert out.startswith(b"%PDF") and not read_pdf(out).has_text_layer


def test_deterministic_and_unknown_rejected():
    from app.documents.ocr import render_pages     # PDF bytes carry a timestamp, so compare pixels
    a, b = (render_pages(degrade(SRC, "noise", 2, seed=7))[0] for _ in range(2))
    assert a.tobytes() == b.tobytes()
    with pytest.raises(ValueError):
        degrade(SRC, "nope", 1)
    with pytest.raises(ValueError):
        degrade(SRC, "blur", 9)
