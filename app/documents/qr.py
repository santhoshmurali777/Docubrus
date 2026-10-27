"""
qr.py -- read a QR code from page images.

PURPOSE   Real certificates carry a QR image, not printed text. Find it and return its link.
INPUTS    PIL images (rendered pages)
OUTPUTS   the first http(s) link found, or None
IMPORTS   opencv, numpy
NOTE      Two OpenCV detectors are tried at several scales because either one alone misses
          dense codes. Returns None rather than guessing when nothing decodes.
"""
from __future__ import annotations

from typing import Iterable, Optional

import cv2
import numpy as np

SCALES = (1.0, 0.7, 0.5, 0.35)


def decode_images(images: Iterable) -> Optional[str]:
    detectors = [cv2.QRCodeDetector(), cv2.QRCodeDetectorAruco()]
    for img in images:
        gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
        for sc in SCALES:
            g = gray if sc == 1.0 else cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            for det in detectors:
                try:
                    text, _, _ = det.detectAndDecode(g)
                except cv2.error:
                    continue
                if text and text.lower().startswith(("http://", "https://")):
                    return text
    return None
