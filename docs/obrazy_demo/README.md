# Skąd są obrazy w dokumentacji

Wszystkie ilustracje w `docs/images` powstały bez prawdziwych nagrań i bez prawdziwych tablic.

- `scene.py` rysuje syntetyczną scenę drogową z fikcyjną tablicą.
- `render_device.py` ładuje prawdziwy `pi/lpr.py` (moduły Hailo i GStreamer są podmienione atrapami w `harness.py`) i woła jego własne funkcje rysujące: nakładkę podglądu, pasek statystyk, baner trafienia z bazy, nakładkę radaru i zdjęcie z fotoradaru. To, co widać na obrazkach `ekran_*.png` i `radar_zdjecie.jpg`, to dokładnie to, co rysuje urządzenie.
- `webdemo.py` uruchamia `pi/narzedzia_testowe/lpr_stub.py` z syntetycznymi zdjęciami i danymi, a `shot2.js` (Playwright) robi zrzuty panelu www (`www_*.png`).
- `charts.py` rysuje wykresy działania radaru na danych symulowanych.
- `render_stl.html` + `stlshot.js` renderują pliki z `hardware/` przez three.js.

Funkcje rysujące szukają czcionek VT323 i Press Start 2P (licencja OFL) w `/home/admin/hailo-apps/local_resources/web/`, tak jak na malince.
