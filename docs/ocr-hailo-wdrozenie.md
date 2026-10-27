# OCR Hailo — stan i wyniki, 06.10.2026

**Aktywny OCR pozostaje na CPU, zgodnie z wyborem użytkownika.** Integracja Hailo jest wdrożona na Raspberry Pi w commicie `32df1c9`. Hailo wykonuje detekcję tablic; OCR Hailo pozostaje do dalszych prób. Zachowano dotychczasowe tempo OCR, ważony konsensus i obsługę tablic dwurzędowych.

W `local_resources/lpr_runtime_config.json` na Pi zapisano `ocr_backend: cpu`. Kopia: `lpr_runtime_config.json.przed_ocr_hailo_20261006` w tym samym katalogu. Kod sprzed integracji: commit `87216ec` i lokalny `lpr.py.przed_hailo_20261006.bak`.

## Wyniki na fizycznym urządzeniu

Raspberry Pi 5, Hailo-10H, HailoRT i firmware 5.1.1. Każda próba obejmuje 254 etykietowane wycinki. Dokładność oznacza zgodność całego napisu z etykietą, bez konsensusu wielu klatek. Próby wykonano obok działającego detektora; nie przeprowadzono próby drogowej.

| Wariant | Poprawne | Trafność | Mediana / p95 |
|---|---:|---:|---:|
| CPU CCT, pełny OcrWorker na JPEG-ach | 213/254 | 83,9% | 117,0 / 129,9 ms |
| Hailo `lvl3`, pełny OcrWorker na JPEG-ach | 200/254 | 78,7% | 9,2 / 14,6 ms |
| Hailo wybrany `l1_clip7`, gotowe wejścia | 27/254 | 10,6% | 7,8 / 13,2 ms |
| `l1_clip7` po ponownej kompilacji DFC 5.3 | 27/254 | 10,6% | 7,8 / 13,3 ms |

Czasy pełnego OcrWorker obejmują kolejkę, przetwarzanie i odbiór wyniku; czasy gotowych wejść obejmują inferencję i CTC. Test pełnej klasy OcrWorker sprawdzał także korelację track_id/generation/seq i zwrot oryginalnego wycinka. Test `lvl3` zakończył się na Hailo bez przejścia na CPU.

Pakiet wybrany pochodzi z `D:\hailo_wynik_20261005\WYNIK_KONCOWY_20261006\pl_lpr_rec.hef` (DFC 5.4, 5 kontekstów). SHA256: `4a1a31653fd31fd8c926ad6e0a561a42ce1aa184f7aef858a6ef5f5d3e67cdc9`. Emulacja INT8 SDK osiągała 207/254 (81,5%); wynik nie potwierdził się na sprzęcie. Drugi HEF pochodzi z `D:\hailo_wynik_20261005\lvl3\`.

Sprawdzono samodzielny test po zatrzymaniu LPR, padding 127/128, padding naprzemienny, wejście FLOAT32 z dokładnym 127,5, zamianę BGR/RGB oraz ponowną kompilację quant HAR w DFC 5.3. Żadna próba nie poprawiła jakości (RGB 26/254, pozostałe 27/254). Wyjście jest odkwantowane do FLOAT32 [0,1]. Przyczyna rozbieżności SDK/sprzęt pozostaje nieustalona. Nie aktualizowano stosu Hailo.

## Integracja i testy

- `lpr_hailo_ocr.py`: InferModel, grupa SHARED, BGR UINT8 48×320, proporcjonalne skalowanie, padding 128; normalizacja w HEF, FLOAT32 na wyjściu, CTC blank=0 i słownik 36 znaków.
- HailoRT działa w osobnym procesie. Timeout inferencji 3 s kończy proces. Błąd inicjalizacji lub inferencji uruchamia CPU; po błędzie inferencji CPU odczytuje ten sam wycinek.
- Poprawiono bufor z potoku: `np.frombuffer(bytes)` daje dane tylko do odczytu, odrzucane przez natywne HailoRT. Kopia zapisywalna przeszła test pełnego workera na Pi.
- `ocr_backend`: cpu (domyślnie), hailo, auto; zmiana po restarcie. `LPR_OCR_BACKEND` ma pierwszeństwo. Auto próbuje Hailo i wraca na CPU po błędzie; nie porównuje trafności.
- `/api/stats`: rzeczywisty `ocr_backend`, ostatni `ocr_infer_ms` dla Hailo.
- Skrypt wdrożenia kopiuje moduł/model/słownik, sprawdza SHA256, kompiluje Python, commituje i restartuje usługę. LAN: zmienna `LPR_PI_SSH=uzytkownik@adres_malinki`.

Domyślny HEF integracji to dostarczony `l1_clip7`, zachowany do diagnozy. **Nie włączać go do normalnej pracy przy obecnej jakości.** Narzędzia testowe pozwalają jawnie wskazać inny HEF.

Lokalnie: 7 testów wejścia, CTC, timeoutu, rzeczywistego protokołu procesu, powrotu na CPU i zapisywalności bufora — OK. Py_compile aplikacji/modułu i bash -n skryptu wdrożenia — OK.

## Pliki i dalsze próby

Na Pi modele w `/home/admin/hailo-apps/local_resources/ocr/hailo_20261006/`: `pl_lpr_rec.hef`, `pl_lpr_rec_lvl3.hef`, `pl_lpr_rec_recompiled.hef`, `pl_dict.txt`, `test_X_raw.npz`, `test_gts.txt`. JSON-y i ZIP oryginalnych JPEG-ów w `ocr_test_20261006/`. Kopie JSON-ów także w lokalnym `kod_z_malinki/ocr_test_20261006/`. Niepełne dawne transfery mają końcówkę `.incomplete_20261006`.

Próba bez przełączania produkcyjnego OCR:

```bash
cd /home/admin/hailo-apps
source venv_hailo_apps/bin/activate
python narzedzia_testowe/test_ocr_hailo.py local_resources/ocr/hailo_20261006 --hef local_resources/ocr/hailo_20261006/pl_lpr_rec_lvl3.hef --json ocr_test_20261006/wynik_lvl3.json
```

Pełny worker: `narzedzia_testowe/test_ocr_worker_na_pi.py --help`. Powrót do CPU: `ocr_backend: cpu` w runtime config i `systemctl --user restart lpr.service`. Limit: 3 uruchomienia / 300 s.

## Sieć

Pełny transfer zakończono przez sieć lokalną, bez przekładania pendrive'a. Problemem tras były zaakceptowane trasy podsieci Tailscale pokrywające LAN na obu komputerach. Tymczasowe wyłączenie przyjmowania tras na PC i trasa hosta do PC w tabeli 52 na Pi umożliwiły poprawne transfery. Przypuszczenie o NordVPN nie zostało potwierdzone. Tymczasowe zmiany sieci cofnięto: usunięto trasę hosta na Pi i przywrócono accept-routes=true na PC. Po cofnięciu usługa nadal działa, OCR CPU i kamera 30 fps.

API: [oficjalny przykład Hailo InferModel](https://github.com/hailo-ai/Hailo-Application-Code-Examples/blob/main/runtime/python/common/hailo_inference.py), [współdzielenie urządzenia](https://github.com/hailo-ai/hailo-apps/blob/main/doc/user_guide/running_parallel.md).


Kontrola końcowa: strona i focus-view HTTP 200, usługa active/running, NRestarts=0, OCR CPU, kamera 30 fps, CPU około 25%, RAM 1,1 GB, bez throttlingu. Sumy SHA256 lpr.py i modułu identyczne na PC i Pi.

