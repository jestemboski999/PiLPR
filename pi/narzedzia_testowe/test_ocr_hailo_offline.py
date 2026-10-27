"""Kontrola kolorów, geometrii, CTC i izolacji timeoutu bez sprzętu."""
from pathlib import Path
import ast
from types import SimpleNamespace
import queue
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lpr_hailo_ocr import prepare_bgr, decode_ctc, HailoOcrProcess, HailoEngine


class OcrTests(unittest.TestCase):
    def test_readonly_pipe_buffer_is_copied_for_native_binding(self):
        engine = HailoEngine.__new__(HailoEngine)
        engine.input_dtype = np.uint8
        captured = []
        engine.bindings = SimpleNamespace(input=lambda: SimpleNamespace(set_buffer=lambda image: captured.append(image)))
        engine.config = SimpleNamespace(run=lambda *a: None)
        engine.output = np.zeros((1, 40, 37), np.float32)
        engine.output[..., 0] = 1
        engine.chars = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        raw = np.frombuffer(bytes(48 * 320 * 3), np.uint8).reshape(48, 320, 3)
        self.assertFalse(raw.flags.writeable)
        text, conf, _ms = engine.infer_raw(raw)
        self.assertEqual((text, conf), ("", 0.0))
        self.assertTrue(captured[0].flags.writeable)
        np.testing.assert_array_equal(captured[0], raw)

    def test_bgr_geometry_and_padding(self):
        crop = np.zeros((20, 101, 3), np.uint8)
        crop[:] = (10, 40, 200)
        image = prepare_bgr(crop)
        self.assertEqual(image.shape, (48, 320, 3))
        self.assertEqual(image.dtype, np.uint8)
        np.testing.assert_array_equal(image[0, 242], (10, 40, 200))
        np.testing.assert_array_equal(image[0, 243], (128, 128, 128))

    def test_wide_and_empty(self):
        image = prepare_bgr(np.full((10, 100, 3), 17, np.uint8))
        self.assertTrue((image == 17).all())
        with self.assertRaises(ValueError):
            prepare_bgr(np.empty((0, 10, 3), np.uint8))

    def test_ctc_blank_separates_repeated_characters(self):
        chars = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        probs = np.zeros((1, 40, 37), np.float32)
        probs[0, :, 0] = 1
        # AA0: repeated A without blank collapses, with blank stays.
        for t, index in enumerate((11, 11, 0, 11, 1)):
            probs[0, t] = 0
            probs[0, t, index] = .99
        text, conf = decode_ctc(probs, chars)
        self.assertEqual(text, "AA0")
        self.assertAlmostEqual(conf, .99, places=6)
        text, conf = decode_ctc(np.eye(37, dtype=np.float32)[np.zeros(40, int)], chars)
        self.assertEqual((text, conf), ("", 0.0))

    def test_timeout_terminates_child(self):
        worker = HailoOcrProcess.__new__(HailoOcrProcess)
        worker.responses = queue.Queue()
        closed = []
        worker.close = lambda: closed.append(True)
        with self.assertRaises(TimeoutError):
            worker._receive(.01)
        self.assertEqual(closed, [True])

    def test_real_child_protocol_and_repeated_requests(self):
        # Rzeczywisty proces/potoki; zastępujemy jedynie niedostępny układ Hailo.
        import subprocess
        real_popen = subprocess.Popen
        code = (
            "import sys,json; "
            "print('@LPR_OCR@'+json.dumps({'kind':'ready'}),flush=True)\n"
            "while True:\n"
            " data=sys.stdin.buffer.read(46080)\n"
            " if not data: break\n"
            " assert len(data)==46080 and data[0:3]==bytes([10,40,200])\n"
            " print('@LPR_OCR@'+json.dumps({'kind':'result','text':'WX3312K',"
            "'confidence':0.99,'ms':7.0}),flush=True)\n"
        )
        def launch(_command, **kwargs):
            return real_popen([sys.executable, "-u", "-c", code], **kwargs)
        with patch("lpr_hailo_ocr.subprocess.Popen", side_effect=launch):
            worker = HailoOcrProcess(timeout=2)
        try:
            crop = np.full((60, 240, 3), (10, 40, 200), np.uint8)
            for _ in range(3):
                self.assertEqual(worker.run_bgr(crop), ("WX3312K", .99))
            self.assertEqual(worker.last_ms, 7)
        finally:
            worker.close()
        self.assertIsNotNone(worker.process.poll())

    def test_hailo_failure_retries_same_crop_on_cpu(self):
        # Wyciągamy klasę, ponieważ import całej aplikacji otwiera kamerę/splash.
        import cv2
        import re
        source = (Path(__file__).resolve().parents[1] / "lpr.py").read_text(encoding="utf-8")
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "OcrWorker")
        namespace = {"np": np, "cv2": cv2, "re": re,
                     "logger": SimpleNamespace(exception=lambda *a: None)}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "OcrWorker", "exec"), namespace)
        worker = namespace["OcrWorker"].__new__(namespace["OcrWorker"])
        closed = []
        def fail(_crop):
            raise TimeoutError("symulacja timeoutu firmware")
        worker._hailo = SimpleNamespace(run_bgr=fail, close=lambda: closed.append(True))
        pred = SimpleNamespace(plate="WX3312K", char_probs=np.full(8, .99))
        def init_cpu():
            worker.backend = "cpu"
            worker._model = SimpleNamespace(run=lambda *a, **kw: [pred])
        worker._init_cpu = init_cpu
        text, conf = worker._odczyt(np.zeros((48, 200, 3), np.uint8))
        self.assertEqual(text, "WX3312K")
        self.assertAlmostEqual(conf, .99)
        self.assertEqual((worker.backend, worker._hailo, closed), ("cpu", None, [True]))


if __name__ == "__main__":
    unittest.main()
