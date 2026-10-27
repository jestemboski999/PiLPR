#!/usr/bin/env python3
"""OCR Paddle/CTC na Hailo-10H. HailoRT jest izolowany w procesie potomnym.

Proces aplikacji przesyła tylko wycinek 48x320 BGR. Wywołania firmware,
które potrafią trzymać GIL, nigdy nie trafiają do procesu z podglądem.
"""
import argparse
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

HEIGHT, WIDTH = 48, 320
PREFIX = "@LPR_OCR@"
DEFAULT_DIR = Path(__file__).resolve().parent / "local_resources/ocr/hailo_20261006"


def prepare_bgr(crop, padding=128):
    """Jak RecResizeImg w treningu; normalizacja jest już w HEF."""
    if crop.ndim != 3 or crop.shape[2] != 3 or min(crop.shape[:2]) == 0:
        raise ValueError("OCR wymaga niepustego wycinka BGR")
    h, w = crop.shape[:2]
    rw = min(WIDTH, math.ceil(HEIGHT * w / h))
    result = np.full((HEIGHT, WIDTH, 3), padding, dtype=np.uint8)
    result[:, :rw] = cv2.resize(crop, (rw, HEIGHT), interpolation=cv2.INTER_LINEAR)
    return result


def decode_ctc(output, chars):
    """Odkwantowane prawdopodobieństwa; blank rozdziela powtórzone znaki."""
    probs = np.asarray(output, dtype=np.float32).reshape(40, 37)
    if len(chars) != 36 or not np.isfinite(probs).all():
        raise ValueError("Niepoprawny słownik lub wyjście OCR")
    indices = probs.argmax(axis=-1)
    text, scores, previous = [], [], -1
    for t, index in enumerate(indices):
        index = int(index)
        if index and index != previous:
            text.append(chars[index - 1])
            scores.append(float(np.clip(probs[t, index], 0.0, 1.0)))
        previous = index
    return "".join(text), float(np.mean(scores)) if scores else 0.0


class HailoEngine:
    """Używać wyłącznie w osobnym procesie lub samodzielnym teście."""
    def __init__(self, hef_path, dict_path, input_format="UINT8"):
        from hailo_platform import VDevice, FormatType, HailoSchedulingAlgorithm
        self.chars = Path(dict_path).read_text(encoding="utf-8").splitlines()
        if len(self.chars) != 36:
            raise ValueError("Słownik OCR musi mieć 36 znaków")
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        params.group_id = "SHARED"
        self.device = VDevice(params)
        self.config_cm = None
        try:
            self.model = self.device.create_infer_model(str(hef_path))
            self.model.set_batch_size(1)
            self.input_dtype = np.float32 if input_format == "FLOAT32" else np.uint8
            self.model.input().set_format_type(getattr(FormatType, input_format))
            # HailoRT odkwantowuje UINT16 do float; nie traktujemy liczb
            # całkowitych jako pewności OCR ani nie zakładamy nazwy wyjścia.
            self.model.output().set_format_type(FormatType.FLOAT32)
            if tuple(self.model.input().shape) != (HEIGHT, WIDTH, 3):
                raise ValueError(f"Nieoczekiwany kształt wejścia: {self.model.input().shape}")
            if int(np.prod(self.model.output().shape)) != 40 * 37:
                raise ValueError(f"Nieoczekiwany kształt wyjścia: {self.model.output().shape}")
            self.config_cm = self.model.configure()
            self.config = self.config_cm.__enter__()
            self.output = np.empty(self.model.output().shape, dtype=np.float32)
            self.bindings = self.config.create_bindings()
            self.bindings.output().set_buffer(self.output)
            self.info = {"input": self.model.input().name,
                         "output": self.model.output().name,
                         "input_shape": list(self.model.input().shape),
                         "output_shape": list(self.model.output().shape),
                         "group": "SHARED", "input_format": input_format}
        except BaseException:
            self.close()
            raise

    def infer_raw(self, image):
        image = np.ascontiguousarray(image, dtype=self.input_dtype)
        if not image.flags.writeable:
            image = image.copy()   # frombuffer(bytes) jest readonly, HailoRT wymaga writable
        if image.shape != (HEIGHT, WIDTH, 3):
            raise ValueError(f"Niepoprawny rozmiar wejścia: {image.shape}")
        self.bindings.input().set_buffer(image)
        start = time.monotonic()
        self.config.run([self.bindings], 2000)
        if (not np.isfinite(self.output).all() or self.output.min() < -0.01
                or self.output.max() > 1.1):
            raise ValueError("Wyjście HEF nie jest odkwantowanym Softmax [0,1]")
        text, confidence = decode_ctc(self.output, self.chars)
        return text, confidence, (time.monotonic() - start) * 1000

    def close(self):
        if self.config_cm is not None and hasattr(self, "config"):
            self.config_cm.__exit__(None, None, None)
            self.config_cm = None
        self.device.release()


class HailoOcrProcess:
    """Jedno zlecenie naraz. Timeout zabija proces, nie zatrzymuje podglądu."""
    def __init__(self, hef_path=None, dict_path=None, timeout=3.0):
        self.timeout = timeout
        self.responses = queue.Queue()
        self.last_ms = 0.0
        env = os.environ.copy()
        env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--serve",
             "--hef", str(hef_path or DEFAULT_DIR / "pl_lpr_rec.hef"),
             "--dict", str(dict_path or DEFAULT_DIR / "pl_dict.txt")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
            bufsize=-1)
        self.reader = threading.Thread(target=self._read, daemon=True,
                                       name="ocr-hailo-pipe")
        self.reader.start()
        try:
            ready = self._receive(45)
            if ready.get("kind") != "ready":
                raise RuntimeError(f"Hailo OCR nie wystartował: {ready}")
            self.info = ready
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in iter(self.process.stdout.readline, b""):
                line = line.decode("utf-8", errors="replace").strip()
                if line.startswith(PREFIX):
                    self.responses.put(json.loads(line[len(PREFIX):]))
                elif line:
                    print(f"[OCR Hailo] {line}", flush=True)
        finally:
            self.responses.put({"kind": "error", "error": "Proces Hailo OCR zakończony"})

    def _receive(self, timeout):
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise TimeoutError(f"Hailo OCR: przekroczono {timeout}s") from None
        if response.get("kind") == "error":
            raise RuntimeError(response.get("error", "Hailo OCR error"))
        return response

    def run_bgr(self, crop):
        image = prepare_bgr(crop)
        if self.process.poll() is not None:
            raise RuntimeError("Proces Hailo OCR nie działa")
        self.process.stdin.write(image.tobytes())
        self.process.stdin.flush()
        response = self._receive(self.timeout)
        self.last_ms = response["ms"]
        return response["text"], response["confidence"]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


def _emit(value):
    print(PREFIX + json.dumps(value), flush=True)


def serve(args):
    cv2.setNumThreads(1)
    if hasattr(os, "nice"):
        os.nice(3)   # detektor ma pierwszeństwo przed procesem OCR
    engine = None
    try:
        engine = HailoEngine(args.hef, args.dict)
        _emit({"kind": "ready", **engine.info})
        nbytes = HEIGHT * WIDTH * 3
        while True:
            data = sys.stdin.buffer.read(nbytes)
            if not data:
                break
            if len(data) != nbytes:
                raise ValueError("Niepełny wycinek OCR w potoku")
            image = np.frombuffer(data, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
            text, confidence, ms = engine.infer_raw(image)
            _emit({"kind": "result", "text": text, "confidence": confidence, "ms": ms})
    except Exception as error:
        _emit({"kind": "error", "error": str(error)})
        return 1
    finally:
        if engine is not None:
            engine.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--hef", default=str(DEFAULT_DIR / "pl_lpr_rec.hef"))
    parser.add_argument("--dict", default=str(DEFAULT_DIR / "pl_dict.txt"))
    sys.exit(serve(parser.parse_args()))
