#!/usr/bin/env python3
"""Train the Hailo-compatible LPRNet using a temporal validation split."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import random
import time

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CHAR_TO_INDEX = {char: index + 1 for index, char in enumerate(CHARS)}
IMAGE_WIDTH = 96
IMAGE_HEIGHT = 32
TIME_STEPS = 24
CLASS_COUNT = len(CHARS) + 1


def load_annotations(path: Path, new_domain_root: Path | None) -> list[tuple[Path, str, bool]]:
    items: list[tuple[Path, str, bool]] = []
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            image_path = Path(row["image_path"])
            if not image_path.is_absolute():
                image_path = (path.parent / image_path).resolve()
            text = row["plate_text"].strip().upper()
            if not image_path.is_file():
                raise FileNotFoundError(image_path)
            if not (2 <= len(text) <= 10) or any(char not in CHAR_TO_INDEX for char in text):
                raise ValueError(f"invalid label {text!r} for {image_path}")
            is_new = (
                new_domain_root is not None
                and image_path.resolve().is_relative_to(new_domain_root)
            )
            items.append((image_path, text, is_new))
    if not items:
        raise ValueError(f"no annotations in {path}")
    return items


def domain_augmentation(image: np.ndarray, heavy: bool) -> np.ndarray:
    height, width = image.shape[:2]
    if heavy and random.random() < 0.8:
        padding = random.uniform(0.0, 0.30)
        pad_x, pad_y = int(width * padding), int(height * padding)
        image = cv2.copyMakeBorder(
            image, pad_y, pad_y, pad_x, pad_x, cv2.BORDER_REPLICATE
        )
        height, width = image.shape[:2]

    if random.random() < 0.85:
        angle = random.uniform(-6, 6)
        shear = random.uniform(-0.12, 0.12)
        scale = random.uniform(0.9, 1.1)
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, scale)
        matrix[0, 1] += shear
        image = cv2.warpAffine(
            image, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE
        )

    if random.random() < 0.5:
        factor = random.uniform(0.35, 0.9)
        image = cv2.resize(
            image,
            (max(8, int(width * factor)), max(4, int(height * factor))),
        )
        image = cv2.resize(image, (width, height))

    blur_draw = random.random()
    if blur_draw < 0.45:
        kernel_size = random.choice([3, 5, 7])
        kernel = np.zeros((kernel_size, kernel_size), np.float32)
        if random.random() < 0.7:
            kernel[kernel_size // 2, :] = 1.0 / kernel_size
        else:
            cv2.line(
                kernel,
                (0, 0),
                (kernel_size - 1, kernel_size - 1),
                1.0 / kernel_size,
            )
        image = cv2.filter2D(image, -1, kernel)
    elif blur_draw < 0.65:
        image = cv2.GaussianBlur(image, (3, 3), 0)

    if heavy and random.random() < 0.8:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV).astype(np.int16)
        hsv[..., 0] = (hsv[..., 0] + random.randint(-12, 12)) % 180
        hsv[..., 1] = np.clip(
            hsv[..., 1] * random.uniform(0.5, 1.4), 0, 255
        )
        image = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        gains = np.array([random.uniform(0.8, 1.25) for _ in range(3)])
        image = np.clip(image.astype(np.float32) * gains, 0, 255).astype(
            np.uint8
        )

    if random.random() < 0.85:
        image = cv2.convertScaleAbs(
            image,
            alpha=random.uniform(0.55, 1.5),
            beta=random.uniform(-45, 45),
        )
    if random.random() < 0.35:
        gamma = random.uniform(0.55, 1.7)
        lookup = np.clip(
            ((np.arange(256) / 255.0) ** gamma) * 255, 0, 255
        ).astype(np.uint8)
        image = lookup[image]

    if heavy and random.random() < 0.4:
        image = cv2.convertScaleAbs(
            image, alpha=random.uniform(0.4, 0.75), beta=0
        )
        noise_sigma = random.uniform(4, 14)
    elif random.random() < 0.3:
        noise_sigma = 8
    else:
        noise_sigma = 0
    if noise_sigma:
        image = np.clip(
            image.astype(np.int16)
            + np.random.normal(0, noise_sigma, image.shape),
            0,
            255,
        ).astype(np.uint8)
    return image


class PlateDataset(Dataset):
    def __init__(self, items: list[tuple[Path, str, bool]], training: bool):
        self.items = items
        self.training = training

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int):
        path, text, is_new = self.items[index]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"cannot decode image: {path}")
        if self.training:
            image = domain_augmentation(image, heavy=not is_new)
        image = cv2.resize(image, (IMAGE_WIDTH, IMAGE_HEIGHT))
        tensor = torch.from_numpy(image.astype(np.float32) / 255.0).permute(2, 0, 1)
        target = torch.tensor(
            [CHAR_TO_INDEX[char] for char in text], dtype=torch.long
        )
        return tensor, target, len(target), text


def collate(batch):
    images, targets, lengths, texts = zip(*batch)
    return (
        torch.stack(images),
        torch.cat(targets),
        torch.tensor(lengths, dtype=torch.long),
        texts,
    )


def block(input_channels: int, output_channels: int, pool=None) -> list[nn.Module]:
    layers: list[nn.Module] = [
        nn.Conv2d(input_channels, output_channels, 3, padding=1),
        nn.BatchNorm2d(output_channels),
        nn.ReLU(inplace=True),
    ]
    if pool:
        layers.append(nn.MaxPool2d(pool))
    return layers


class LPRNet(nn.Module):
    """Convolution-only CTC model that is compatible with Hailo DFC."""

    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            *block(3, 32),
            *block(32, 32, (2, 2)),
            *block(32, 64),
            *block(64, 64, (2, 2)),
            *block(64, 128),
            *block(128, 128, (2, 1)),
            *block(128, 256),
            *block(256, 256, (2, 1)),
            nn.Conv2d(256, 256, (2, 1)),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Dropout2d(0.3),
            nn.Conv2d(256, CLASS_COUNT, 1),
        )

    def forward(self, inputs):
        return self.net(inputs)


def decode_greedy(logits: torch.Tensor) -> list[str]:
    indices = logits.squeeze(2).argmax(1)
    decoded: list[str] = []
    for row in indices.detach().cpu().numpy():
        characters: list[str] = []
        previous = 0
        for value in row:
            if value != 0 and value != previous:
                characters.append(CHARS[value - 1])
            previous = value
        decoded.append("".join(characters))
    return decoded


def levenshtein(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def evaluate(model, loader, device) -> tuple[float, float]:
    model.eval()
    exact = 0
    count = 0
    distance = 0
    characters = 0
    with torch.no_grad():
        for images, _, _, texts in loader:
            predictions = decode_greedy(model(images.to(device, non_blocking=True)))
            for prediction, reference in zip(predictions, texts, strict=True):
                exact += prediction == reference
                count += 1
                distance += levenshtein(reference, prediction)
                characters += len(reference)
    return exact / max(1, count), max(0.0, 1.0 - distance / max(1, characters))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-csv", required=True, type=Path)
    parser.add_argument("--validation-csv", required=True, type=Path)
    parser.add_argument("--new-domain-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--init-weights", type=Path)
    parser.add_argument("--epochs", type=int, default=180)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--new-domain-repeat", type=int, default=5)
    parser.add_argument("--learning-rate", type=float, default=1.0e-3)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--early-stopping-patience", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    new_root = args.new_domain_root.resolve()
    train_items = load_annotations(args.train_csv.resolve(), new_root)
    validation_items = load_annotations(args.validation_csv.resolve(), new_root)
    old_items = [item for item in train_items if not item[2]]
    new_items = [item for item in train_items if item[2]]
    weighted_train = old_items + new_items * args.new_domain_repeat
    random.shuffle(weighted_train)

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; LPRNet v3 must run on the RTX GPU")
    device = torch.device("cuda")
    model = LPRNet().to(device)
    if args.init_weights:
        model.load_state_dict(
            torch.load(args.init_weights.resolve(), map_location=device)
        )

    train_loader = DataLoader(
        PlateDataset(weighted_train, training=True),
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.workers,
        collate_fn=collate,
        drop_last=True,
        persistent_workers=args.workers > 0,
        pin_memory=True,
    )
    validation_loader = DataLoader(
        PlateDataset(validation_items, training=False),
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=max(1, min(2, args.workers)),
        collate_fn=collate,
        persistent_workers=args.workers > 0,
        pin_memory=True,
    )

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.learning_rate, weight_decay=1.0e-4
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.epochs
    )
    loss_function = nn.CTCLoss(blank=0, zero_infinity=True)
    scaler = torch.amp.GradScaler("cuda")

    configuration = {
        **vars(args),
        "train_csv": str(args.train_csv.resolve()),
        "validation_csv": str(args.validation_csv.resolve()),
        "new_domain_root": str(new_root),
        "output_dir": str(output),
        "init_weights": (
            str(args.init_weights.resolve()) if args.init_weights else None
        ),
        "gpu": torch.cuda.get_device_name(0),
        "old_samples": len(old_items),
        "new_samples": len(new_items),
        "weighted_train_samples": len(weighted_train),
        "validation_samples": len(validation_items),
        "architecture": {
            "image_width": IMAGE_WIDTH,
            "image_height": IMAGE_HEIGHT,
            "time_steps": TIME_STEPS,
            "classes": CLASS_COUNT,
            "characters": CHARS,
        },
    }
    (output / "config.json").write_text(
        json.dumps(configuration, default=str, indent=2), encoding="utf-8"
    )

    metrics_path = output / "metrics.csv"
    with metrics_path.open("w", newline="", encoding="utf-8") as metrics_file:
        writer = csv.DictWriter(
            metrics_file,
            fieldnames=[
                "epoch",
                "train_loss",
                "validation_exact_accuracy",
                "validation_character_accuracy",
                "learning_rate",
                "elapsed_seconds",
            ],
        )
        writer.writeheader()

        best_exact = -1.0
        best_character = -1.0
        stale_epochs = 0
        started = time.perf_counter()
        for epoch in range(1, args.epochs + 1):
            model.train()
            total_loss = 0.0
            for images, targets, lengths, _ in train_loader:
                images = images.to(device, non_blocking=True)
                targets = targets.to(device, non_blocking=True)
                lengths = lengths.to(device, non_blocking=True)
                optimizer.zero_grad(set_to_none=True)
                with torch.amp.autocast("cuda", dtype=torch.float16):
                    logits = model(images).squeeze(2).permute(2, 0, 1)
                    log_probabilities = logits.log_softmax(2)
                    input_lengths = torch.full(
                        (images.size(0),),
                        TIME_STEPS,
                        dtype=torch.long,
                        device=device,
                    )
                    loss = loss_function(
                        log_probabilities.float(),
                        targets,
                        input_lengths,
                        lengths,
                    )
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                scaler.step(optimizer)
                scaler.update()
                total_loss += float(loss.detach())
            scheduler.step()

            validation_exact, validation_character = evaluate(
                model, validation_loader, device
            )
            average_loss = total_loss / max(1, len(train_loader))
            elapsed = time.perf_counter() - started
            row = {
                "epoch": epoch,
                "train_loss": average_loss,
                "validation_exact_accuracy": validation_exact,
                "validation_character_accuracy": validation_character,
                "learning_rate": optimizer.param_groups[0]["lr"],
                "elapsed_seconds": elapsed,
            }
            writer.writerow(row)
            metrics_file.flush()

            improved = validation_exact > best_exact or (
                validation_exact == best_exact
                and validation_character > best_character
            )
            if improved:
                best_exact = validation_exact
                best_character = validation_character
                stale_epochs = 0
                torch.save(model.state_dict(), output / "best.pt")
            else:
                stale_epochs += 1
            print(
                f"epoch={epoch:03d} loss={average_loss:.4f} "
                f"val_exact={validation_exact:.4f} "
                f"val_char={validation_character:.4f} "
                f"best={best_exact:.4f} elapsed={elapsed:.0f}s",
                flush=True,
            )
            if stale_epochs >= args.early_stopping_patience:
                print(f"early stopping after {stale_epochs} stale epochs", flush=True)
                break

    torch.save(model.state_dict(), output / "last.pt")
    summary = {
        "best_validation_exact_accuracy": best_exact,
        "best_validation_character_accuracy": best_character,
        "epochs_completed": epoch,
        "elapsed_seconds": time.perf_counter() - started,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
