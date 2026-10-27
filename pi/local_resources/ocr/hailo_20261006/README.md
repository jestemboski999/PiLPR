# Gotowy wynik OCR dla Hailo-10H — 06.10.2026

Wybrany wariant **l1_clip7**: INT8 w emulacji **207/254 = 81,5%**, FLOAT **213/254 = 83,9%**.
Powtórny test 06.10 potwierdził te liczby i FP_OPTIMIZED 213/254. Cel dokładności 80–83% osiągnięty.
Korzystaj z `pl_lpr_rec.hef` i `pl_lpr_rec_quant.har` z tego folderu.
Stary pakiet w `hailo_quant_package_20261005` ma inne pliki o tych samych głównych nazwach (baseline 5,5%).

## Zawartość

- `pl_lpr_rec.hef` — HEF wariantu l1_clip7, DFC 5.4.0, Hailo-10H.
- `pl_lpr_rec_quant.har` — odpowiadający mu quant HAR (źródło: pl_lpr_rec_quant_l1_clip7.har).
- `optimize.log`, `run_full.log`, `norm.alls` — rzeczywiste logi i konfiguracja wybranego wariantu.
- `test_l1_clip7.log`, `compile_and_parse.log`, `WERYFIKACJA.json` — kontrola z 06.10.2026.
- `SHA256SUMS.txt` — sumy kontrolne wszystkich plików pakietu poza samym manifestem.
- `pl_lpr_rec.har`, `calib_raw.npy` — źródła do odtworzenia optymalizacji.
- `test_X_raw.npy`, `test_gts.txt`, `pl_dict.txt`, `test_hef2.py` — test i dekoder.
- `odtworz.sh` — odtworzenie wybranego przebiegu w istniejącym środowisku Hailo DFC 5.4.0.
- `WYNIKI.md` — porównanie zakończonych eksperymentów; `HAILO_POSTEP_PRAC.md` — historia decyzji.

## Odtworzenie i test

W istniejącym środowisku Linux/WSL/kontenerze z Hailo DFC 5.4.0, po wejściu do tego folderu:

```bash
python test_hef2.py pl_lpr_rec_quant.har --fp
# Odtworzenie optimize + compiler + test; wyniki tylko w podfolderze odtworzone:
bash odtworz.sh
```

Nie trzeba ponownie optymalizować, aby użyć dostarczonego HEF.
Pierwotna optymalizacja l1_clip7 była na CPU; weryfikacja emulacji z 06.10 użyła RTX 4070 SUPER.
Zmiana środowiska lub wersji może zmienić wynik ponownej optymalizacji; dostarczony HAR ma wynik potwierdzony.

## Format i zakres weryfikacji

`hailortcli parse-hef` potwierdził: HAILO10H/HAILO15H, compiler 5.4.0, 5 kontekstów.
Wejście `pl_lpr_rec/input_layer1`: UINT8 NHWC(48x320x3).
Wyjście `pl_lpr_rec/precision_change4`: UINT16 NHWC(1x40x37).
Nie zakładaj nazwy `output_layer1` w HailoRT. Surowy UINT16 wymaga odkwantowania, jeśli ma być używany jako wynik float.
Preprocessing: BGR [0,255], wysokość 48, proporcje zachowane, padding z prawej; w danych emulacji padding 127,5 (float32).
Normalizacja `(x - 127.5) / 127.5` jest w modelu — nie stosować jej drugi raz.
CTC: argmax po 37 klasach w 40 krokach, blank=0; klasy 1–36 według `pl_dict.txt`.

81,5% dotyczy emulacji **HAR** w SDK na 254 tablicach z 02.10, nie testu fizycznego HEF na urządzeniu.
Hailo-10H nie jest podłączony do tego PC; test na urządzeniu, preprocessing UINT8 i wydajność pozostają etapem integracji.

## Kryterium Adaround z historycznego zlecenia

Wybrany level=1 wykonuje Bias Correction i świadomie pomija Adaround. Rzeczywisty `optimize.log` zawiera `Adaround skipped`.
Warunek „brak skipped dla obu algorytmów” nie jest spełniony, choć cel dokładności jest spełniony.
Wariant lvl3 wykonuje oba algorytmy, lecz ma 73,6%; Adaround + clipping daje 7,5% / 20,9%.
Dlatego przekazano najlepszy zmierzony model, a logów nie zmieniano.
