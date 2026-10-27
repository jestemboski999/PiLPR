# Wyniki końcowe OCR → Hailo-10H (06.10.2026)

Rekomendowany wariant: **l1_clip7**, dokładny odczyt **207/254 = 81,5%**.
FLOAT i FP_OPTIMIZED: **213/254 = 83,9%**. Spadek: 6 tablic, 2,36 punktu procentowego.
Cel dokładności 80–83% osiągnięty. DFC/HailoRT 5.4.0.

Gotowy pakiet z jednoznacznymi nazwami plików:
`WYNIK_KONCOWY_20261006` (poza repozytorium).
Używać `pl_lpr_rec.hef` i `pl_lpr_rec_quant.har` z tego końcowego pakietu.
Pliki o tych nazwach bezpośrednio w starym `hailo_quant_package_20261005` są baseline, nie rekomendacją.

Test: 254 tablice z 02.10.2026, `test_X_raw.npy` + `test_gts.txt`, exact match, argmax + CTC.
Test najlepszego HAR powtórzony 06.10.2026 na GPU: FLOAT 213/254, FP_OPTIMIZED 213/254, SDK_QUANTIZED 207/254.
To wynik emulacji HAR w SDK, nie pomiar na fizycznym Hailo-10H. Tego urządzenia na PC nie znaleziono.

| Wariant | INT8 | Stan / wniosek |
|---|---:|---|
| baseline | 14/254 = 5,5% | level 1 bez clippingu |
| cpuft | 1/254 = 0,4% | QFT na CPU; na GPU zawieszał się |
| logits_l1 / logits_l1_fc16 | 11/254 = 4,3% | usunięcie Softmax nie pomogło |
| l1_16a | 9/254 = 3,5% | kompiluje się |
| l1_16b | 17/254 = 6,7% | nie kompiluje się |
| lvl3 | 187/254 = 73,6% | Bias Correction + Adaround, poprawny HEF |
| l1_clip | 134/254 = 52,8% | clipping wszystkich warstw |
| l1_clip2 | 19/254 = 7,5% | percentile 0.1 / 99.9 |
| l1_clip3 | 27/254 = 10,6% | percentile 0.001 / 99.999 |
| l1_clip4 | 3/254 = 1,2% | percentile 0.05 / 99.95 |
| l1_clip5 | 12/254 = 4,7% | clipping + recollect_stats |
| l1_clip6 | 3,9% | clipping tylko transformera; wynik z dziennika |
| **l1_clip7** | **207/254 = 81,5%** | **wybrany; clipping backbone, level 1, SNR 14,94 dB** |
| l1_clip7b | 202/254 = 79,5% | calibset_size 512 |
| l1_clip7c | 205/254 = 80,7% | calibset_size 1024, dane 2048 |
| lvl3_clip7 | 19/254 = 7,5% | zakończony; Adaround pogorszył wynik |
| lvl4_big_clip7 | 53/254 = 20,9% | zakończony; większy zbiór nie pomógł |
| lvl3_16a / lvl3_clip | — | świadomie przerwane przez poprzedniego agenta |
| lvl4_big / lvl4_big_clip | — | poprzednie kolejki odwołane; nie czekają na wykonanie |
| l1_16mod / l1_16all | — | konfiguracje odrzucone / niewspierana precyzja |

## Dlaczego l1_clip7

Ten wariant daje najwyższą dokładność spośród zapisanych, zakończonych prób i ma skompilowany HEF.
Clipping 0.01 / 99.99 obejmuje backbone; clipping transformera wyłączono.
Normalizacja on-chip `(x - 127.5) / 127.5`, wyjście a16_w16.
Oryginalny README wymagał wykonania Adaround i Bias Correction. Wybrany wariant wykonuje Bias Correction,
ale **celowo pomija Adaround przy optimization_level=1**. Tego warunku formalnie nie spełnia.
Nie ma podstaw, by na podstawie samego `skipped` uznawać jego 81,5% za błędny wynik.
Warianty wykonujące oba algorytmy miały niższą dokładność.

## Zweryfikowany format HEF

`hailortcli parse-hef`: kompatybilny HAILO10H/HAILO15H, compiler 5.4.0, 5 kontekstów.
Wejście `pl_lpr_rec/input_layer1`: UINT8, NHWC(48x320x3).
Wyjście `pl_lpr_rec/precision_change4`: UINT16, NHWC(1x40x37).
Nie zakładać nazwy `output_layer1` dla VStream ani interpretować surowego UINT16 jako prawdopodobieństw float.
Surowy BGR [0,255], wysokość 48, proporcje zachowane, padding tylko z prawej.
W danych emulacji padding ma 127,5 (float32). Ścieżkę UINT8 i odkwantowanie wyjścia trzeba sprawdzić przy integracji na urządzeniu.
Nie normalizować wejścia drugi raz. CTC: blank 0, klasy 1–36 według `pl_dict.txt`.

## Przekazanie

Pakiet zawiera HEF, quant HAR, rzeczywisty optimize.log (bez usuwania skipped), konfigurację .alls,
historyczny log pełnego przebiegu, nowe logi weryfikacji, wyniki JSON, SHA256,
test i dane potrzebne do odtworzenia. Szczegóły i polecenia w README pakietu końcowego.
Historia zmian i decyzji: `HAILO_POSTEP_PRAC.md` w katalogu rejestracje i na E:.
