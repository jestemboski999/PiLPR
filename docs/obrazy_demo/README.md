# Skąd są obrazy w dokumentacji

Wszystkie ilustracje w `docs/images` powstały bez prawdziwych nagrań i bez prawdziwych tablic.

- `scene.py` rysuje syntetyczną scenę drogową z fikcyjną tablicą.
- `render_device.py` ładuje prawdziwy `pi/lpr.py` (moduły Hailo i GStreamer są podmienione atrapami w `harness.py`) i woła jego własne funkcje rysujące: nakładkę podglądu, pasek statystyk, baner trafienia z bazy, nakładkę radaru i zdjęcie z fotoradaru. To, co widać na obrazkach `ekran_*.png` i `radar_zdjecie.jpg`, to dokładnie to, co rysuje urządzenie.
- `webdemo.py` uruchamia `pi/narzedzia_testowe/lpr_stub.py` z syntetycznymi zdjęciami i danymi, a `shot2.js` (Playwright) robi zrzuty panelu www (`www_*.png`).
- `vhs.py` to wspólny styl wszystkich grafik objaśniających: kolory, linie VHS, zakłócenia i gwiazdki z tapety `pi/local_resources/lpr_splash_pilpr_vhs.png`, czcionka VT323.
- `charts.py` rysuje wykresy działania radaru na danych symulowanych.
- `diagram.py` + `diagram.js` renderują `docs/diagrams/architektura.svg` do `architektura.png` z czcionką VT323.
- `render_stl.html` + `stlshot.js` renderują pliki z `hardware/` przez three.js, a `uchwyty.py` składa je w `uchwyty_3d.png`.

Funkcje rysujące szukają czcionek VT323 i Press Start 2P (licencja OFL) w `/home/admin/hailo-apps/local_resources/web/`, tak jak na malince.
- `hero.py` składa grafikę otwierającą README z renderu ekranu i zrzutu z telefonu (`python hero.py pl` lub `en`).
- `scene_szafa.py` i `hero_szafa.py` to alternatywny styl „szafy grającej” (malowane czarne szkło, palmy, latarnie-gwiazdy). Włącza się go zmiennymi `LPR_DEMO_STYL=szafa` i `LPR_DEMO_OUT=<katalog>` dla `render_device.py`, `webdemo.py` i `shot2.js`; bez nich skrypty działają jak dotąd.
