#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scalanie bazy z karty SD (praca BEZ pendrive'a) do bazy na pendrivie.

Kiedy malinka pracuje bez pendrive'a (np. noc przy drodze 01/02.10.2026), pisze
do /home/admin/hailo-apps/logs/lpr/plates.db i logs/lpr/<dzień>/*.jpg. Po
włożeniu pendrive'a `_przeprowadzka_z_sd` (lpr.py) przenosi pliki dashcamu i
radaru, ale BAZY nie — kolizje id między dwiema bazami. To narzędzie robi
resztę:

  sightings      → wstawiane BEZ id (nowe id na pendrivie); zdjęcia
                   (full_image, crop_image, extra_crops) kopiowane do
                   /media/lpr/lpr/<dzień>/, przy kolizji nazwy przyrostek _sd
                   (i nazwa w bazie poprawiona). Duplikaty (ta sama tablica
                   + seen_at + full_image) pomijane — można uruchamiać wielokrotnie.
  annotations    → przepisane na NOWE sighting_id (oceny człowieka = najcenniejsze).
  plates         → first_seen/last_seen/total_count scalone (min/max/suma).
  radar_zdarzenia→ wstawiane bez id; pliki zdjęć/klipów leżą już w
                   /media/lpr/radar/<dzień>/ (przeniosła je przeprowadzka) —
                   sprawdzamy, czy są, i wpisujemy nazwę (z _sd, jeśli trzeba).

Po udanym scaleniu baza SD dostaje nazwę plates.db.scalona_<data>_<godzina>
(nic nie kasujemy). Zawsze najpierw kopia bazy docelowej:
  /media/lpr/lpr/plates.db.przed_scaleniem_<data>_<godzina>

Użycie (na malince, usługa może działać — SQLite z WAL, timeout 30 s):
  python3 scal_baze_sd.py                       # SD → /media/lpr
  python3 scal_baze_sd.py --test                 # próba na KOPII bazy w /tmp, nic nie zmienia
  python3 scal_baze_sd.py --sd <plates.db> --usb <katalog pendrive>   # inne ścieżki
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SD_DIR = Path("/home/admin/hailo-apps/logs/lpr")
USB_ROOT = Path("/media/lpr")


def polacz(p: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(p), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def kolumny(con, tabela) -> list:
    return [r[1] for r in con.execute(f"PRAGMA table_info({tabela})")]


def upewnij_schemat(dst: sqlite3.Connection) -> None:
    """Brakujące kolumny/tabele w bazie docelowej (starsza baza na pendrivie)."""
    dst.execute("""CREATE TABLE IF NOT EXISTS sightings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, plate_text TEXT NOT NULL,
        seen_at TEXT NOT NULL, full_image TEXT, crop_image TEXT,
        det_confidence REAL, ocr_confidence REAL, ocr_reads INTEGER,
        track_duration REAL, bbox TEXT, extra_crops TEXT)""")
    dst.execute("""CREATE TABLE IF NOT EXISTS annotations (
        sighting_id INTEGER PRIMARY KEY, is_correct INTEGER, correct_text TEXT,
        reviewed_at TEXT, notes TEXT, correct_bbox TEXT, lepsza_klatka TEXT)""")
    dst.execute("""CREATE TABLE IF NOT EXISTS plates (
        plate_text TEXT PRIMARY KEY, first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, total_count INTEGER DEFAULT 0)""")
    dst.execute("""CREATE TABLE IF NOT EXISTS radar_zdarzenia (
        id INTEGER PRIMARY KEY AUTOINCREMENT, czas TEXT NOT NULL,
        predkosc REAL NOT NULL, kierunek TEXT, probek INTEGER, tablica TEXT,
        ocr_conf REAL, zdjecie TEXT, crop TEXT, klip TEXT, uwagi TEXT,
        v_kamera REAL, pewne INTEGER, debug TEXT)""")
    for tab, kol in (("sightings", ("bbox TEXT", "extra_crops TEXT")),
                     ("annotations", ("correct_bbox TEXT", "lepsza_klatka TEXT")),
                     ("radar_zdarzenia", ("v_kamera REAL", "pewne INTEGER", "debug TEXT"))):
        for k in kol:
            try:
                dst.execute(f"ALTER TABLE {tab} ADD COLUMN {k}")
            except sqlite3.OperationalError:
                pass
    dst.commit()


def kopiuj_zdjecie(nazwa: str, dzien: str, sd_dir: Path, usb_lpr: Path,
                   test: bool, stat: dict) -> str:
    """Kopiuje logs/lpr/<dzień>/<nazwa> → /media/lpr/lpr/<dzień>/; zwraca
    nazwę docelową (z _sd przy kolizji). Brak pliku źródłowego → nazwa bez zmian."""
    if not nazwa:
        return nazwa
    zrodlo = sd_dir / dzien / nazwa
    if not zrodlo.is_file():
        stat["brak_plikow"] += 1
        return nazwa
    cel_dir = usb_lpr / dzien
    cel = cel_dir / nazwa
    if cel.exists():
        if cel.stat().st_size == zrodlo.stat().st_size:
            return nazwa                      # ten sam plik już jest
        p = Path(nazwa)
        cel = cel_dir / (p.stem + "_sd" + p.suffix)
    if not test:
        cel_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(zrodlo), str(cel))
    stat["pliki"] += 1
    return cel.name


def scal(sd_db: Path, usb_root: Path, test: bool) -> int:
    sd_dir = sd_db.parent
    usb_lpr = usb_root / "lpr"
    dst_db = usb_lpr / "plates.db"
    if not sd_db.is_file():
        print(f"brak bazy SD: {sd_db}")
        return 1
    if not dst_db.is_file():
        print(f"brak bazy docelowej: {dst_db} (pendrive włożony i zamontowany?)")
        return 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if test:
        tmp = Path(tempfile.mkdtemp(prefix="scal_test_"))
        kopia = tmp / "plates.db"
        shutil.copy2(str(dst_db), str(kopia))
        print(f"[TEST] pracuję na kopii bazy docelowej: {kopia} (nic na pendrivie nie zmienię)")
        dst_db = kopia
    else:
        kopia = usb_lpr / f"plates.db.przed_scaleniem_{stamp}"
        shutil.copy2(str(dst_db), str(kopia))
        print(f"kopia bazy docelowej: {kopia}")

    src = polacz(sd_db)
    dst = polacz(dst_db)
    upewnij_schemat(dst)
    stat = {"sightings": 0, "dup": 0, "annot": 0, "plates": 0, "radar": 0,
            "radar_dup": 0, "pliki": 0, "brak_plikow": 0}
    kol_s = [k for k in kolumny(src, "sightings") if k != "id"]
    kol_s_dst = set(kolumny(dst, "sightings"))
    kol_s = [k for k in kol_s if k in kol_s_dst]
    mapa_id = {}
    try:
        dst.execute("BEGIN")
        for r in src.execute("SELECT * FROM sightings ORDER BY id"):
            dzien = (r["seen_at"] or "")[:10]
            dup = dst.execute("SELECT id FROM sightings WHERE plate_text=? AND seen_at=? "
                              "AND IFNULL(full_image,'')=IFNULL(?, '')",
                              (r["plate_text"], r["seen_at"], r["full_image"])).fetchone()
            if dup:
                mapa_id[r["id"]] = dup["id"]
                stat["dup"] += 1
                continue
            wart = {k: r[k] for k in kol_s}
            wart["full_image"] = kopiuj_zdjecie(r["full_image"], dzien, sd_dir, usb_lpr, test, stat)
            wart["crop_image"] = kopiuj_zdjecie(r["crop_image"], dzien, sd_dir, usb_lpr, test, stat)
            if wart.get("extra_crops"):
                wart["extra_crops"] = ";".join(
                    kopiuj_zdjecie(n, dzien, sd_dir, usb_lpr, test, stat)
                    for n in wart["extra_crops"].split(";") if n)
            cur = dst.execute(
                f"INSERT INTO sightings ({','.join(wart)}) VALUES ({','.join('?' * len(wart))})",
                list(wart.values()))
            mapa_id[r["id"]] = cur.lastrowid
            stat["sightings"] += 1
        # oceny człowieka
        kol_a = [k for k in kolumny(src, "annotations") if k in set(kolumny(dst, "annotations"))]
        for r in src.execute("SELECT * FROM annotations"):
            nowy = mapa_id.get(r["sighting_id"])
            if nowy is None:
                continue
            wart = {k: r[k] for k in kol_a}
            wart["sighting_id"] = nowy
            dst.execute(f"INSERT OR REPLACE INTO annotations ({','.join(wart)}) "
                        f"VALUES ({','.join('?' * len(wart))})", list(wart.values()))
            stat["annot"] += 1
        # statystyka tablic
        for r in src.execute("SELECT * FROM plates"):
            dst.execute("""INSERT INTO plates (plate_text, first_seen, last_seen, total_count)
                VALUES (?,?,?,?) ON CONFLICT(plate_text) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen),
                total_count = total_count + excluded.total_count""",
                        (r["plate_text"], r["first_seen"], r["last_seen"], r["total_count"] or 0))
            stat["plates"] += 1
        # zdarzenia radaru (pliki przeniosła już przeprowadzka → /media/lpr/radar)
        try:
            kol_r = [k for k in kolumny(src, "radar_zdarzenia") if k != "id"
                     and k in set(kolumny(dst, "radar_zdarzenia"))]
            radar_usb = usb_root / "radar"
            for r in src.execute("SELECT * FROM radar_zdarzenia ORDER BY id"):
                if dst.execute("SELECT 1 FROM radar_zdarzenia WHERE czas=? AND predkosc=? "
                               "AND IFNULL(zdjecie,'')=IFNULL(?,'')",
                               (r["czas"], r["predkosc"], r["zdjecie"])).fetchone():
                    stat["radar_dup"] += 1
                    continue
                wart = {k: r[k] for k in kol_r}
                dzien = (r["czas"] or "")[:10]
                for k in ("zdjecie", "crop", "klip"):
                    n = wart.get(k)
                    if n and not (radar_usb / dzien / n).is_file():
                        p = Path(n)
                        alt = p.stem + "_sd" + p.suffix
                        if (radar_usb / dzien / alt).is_file():
                            wart[k] = alt
                        elif (sd_dir.parent / "radar" / dzien / n).is_file():
                            # plik jeszcze na SD (przeprowadzka nie zdążyła) — kopiujemy
                            cel = radar_usb / dzien / n
                            if not test:
                                cel.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(str(sd_dir.parent / "radar" / dzien / n), str(cel))
                            stat["pliki"] += 1
                        else:
                            stat["brak_plikow"] += 1
                dst.execute(f"INSERT INTO radar_zdarzenia ({','.join(wart)}) "
                            f"VALUES ({','.join('?' * len(wart))})", list(wart.values()))
                stat["radar"] += 1
        except sqlite3.OperationalError as e:
            print(f"radar_zdarzenia: pominięte ({e})")
        dst.execute("COMMIT")
    except Exception as e:
        dst.execute("ROLLBACK")
        print(f"BŁĄD — cofnięto wszystko: {e}")
        return 2
    finally:
        src.close()
        dst.close()
    print(f"sightings: {stat['sightings']} nowych, {stat['dup']} duplikatów pominiętych; "
          f"annotations: {stat['annot']}; plates: {stat['plates']}; "
          f"radar_zdarzenia: {stat['radar']} nowych, {stat['radar_dup']} dupl.; "
          f"plików skopiowanych: {stat['pliki']}, brakujących: {stat['brak_plikow']}")
    if test:
        print("[TEST] zakończony — baza SD i pendrive nietknięte")
        return 0
    nowa = sd_db.with_name(f"plates.db.scalona_{stamp}")
    os.rename(str(sd_db), str(nowa))
    for suf in ("-wal", "-shm"):
        p = Path(str(sd_db) + suf)
        if p.exists():
            p.unlink()
    print(f"baza SD przeniesiona na: {nowa} (zdjęcia na SD zostają w {sd_dir})")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sd", default=str(SD_DIR / "plates.db"), help="baza na SD")
    ap.add_argument("--usb", default=str(USB_ROOT), help="katalog pendrive'a (z lpr/ i radar/)")
    ap.add_argument("--test", action="store_true", help="próba na kopii, bez zmian")
    a = ap.parse_args()
    sys.exit(scal(Path(a.sd), Path(a.usb), a.test))
