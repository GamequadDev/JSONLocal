# Odwołania2json

Automatyczna klasyfikacja i ekstrakcja ustrukturyzowanych danych JSON z wielodokumentowych spraw odwoławczych przed Krajową Izbą Odwoławczą (KIO) — rozwiązanie opracowane w ramach hackathonu **HackNation 2026** (wyzwanie Urzędu Zamówień Publicznych).

Rozwiązanie działa w całości na komponentach **open source**, w trybie **on-premise bez GPU** (na standardowym komputerze biurowym CPU / 16 GB RAM), obsługuje zarówno elektroniczne pliki PDF, jak i skany, zapewnia pełną **transparentność ekstrakcji** oraz umożliwia **dodawanie nowych klas dokumentów bez modyfikacji kodu źródłowego**.

---

## 1. Architektura rozwiązania

Pipeline przetwarzania dokumentu składa się z pięciu niezależnych etapów:

```text
PDF / skany (input/)
   │
   ▼
1. Odczyt warstwy tekstowej (pdftotext, pierwsze 3 strony)
   │  └─ gdy strona ma < 80 znaków alfanumerycznych → fallback OCR:
   │     pdftoppm (220 DPI) → RapidOCR (PP-OCRv5 Latin, ONNX Runtime CPU)
   │     → korekta polskich znaków diakrytycznych (spylls + Hunspell pl_PL)
   ▼
2. Klasyfikacja treści dokumentu (classify_documents.py)
   │  └─ klasy ładowane dynamicznie z config/documents.json
   │  └─ otwarto-źródłowy model LLM (basal-2.0-mini) z gramatyką BNF
   │     wymuszającą odpowiedź 1 tokenem (kod klasy A–Z)
   │  └─ brak polegania na nazwie pliku
   ▼
3. Ekstrakcja ustrukturyzowana (config_loader.py + extraction.py)
   │  ├─ klasy wbudowane: odwolanie, pelnomocnictwo, przelew (deterministyczne)
   │  ├─ klasy własne: silnik schema-driven z config/documents.json (regex + typy)
   │  └─ klasa spoza zakresu (inny): pusty obiekt {}
   ▼
4. Ślad ekstrakcji / transparentność (build_extraction_trace)
   │  └─ dla każdego pola: wartość, typ, metoda, warstwa (pdftotext/OCR) i cytat
   ▼
5. Wynik JSON (output/ z zachowaniem struktury drzewa + podgląd/edycja w GUI)
```

### Kluczowe moduły

| Plik | Rola |
| --- | --- |
| `classify_documents.py` | Pipeline wsadowy (`input/` → `output/`), odczyt PDF, wywołanie OCR oraz klasyfikacja LLM |
| `ocr.py` | Warstwa OCR na CPU (`RapidOCR` + `ONNX Runtime`) z korektą ortograficzno-diakrytyczną (`spylls` + `hunspell-pl`) |
| `extraction.py` | Deterministyczne ekstraktory dla klas `odwolanie`, `pelnomocnictwo` i `przelew` zgodne z `oczekiwane-json/` |
| `config_loader.py` | Ładowanie i walidacja `config/documents.json`, generyczny ekstraktor ze schematu, atomowy zapis nowych klas, budowa śladu ekstrakcji (`trace`) |
| `config/documents.json` | Plik konfiguracyjny klas dokumentów i ich schematów ekstrakcji (domyślna kopia w `documents.default.json`) |
| `web.py` + `index.html` | Interfejs WWW (GUI) bez zewnętrznych zależności (działa offline): analiza pojedynczych PDF/skanów, tryb wsadowy, podgląd cytatów, edycja pól i kreator nowych klas |
| `compose.yaml` | Konfiguracja Docker Compose dla trybu wsadowego (`klasyfikacja`) i GUI (`gui`) oraz lokalnego modelu LLM |

---

## 2. Wykorzystany model LLM i komponenty open source

- **Model LLM**: `hf.co/Remek/basal-2.0-mini-GGUF:Q5_K_M` (kontekst `8192`) — lekki, otwarto-źródłowy model językowy w kwantyzacji Q5_K_M, uruchamiany lokalnie na CPU (bez GPU) w kontenerze `ghcr.io/ggml-org/llama.cpp:server` (standardowy Docker Compose, bez wymagania wtyczki Docker Model Runner).
- **Dlaczego taki dobór**: model jest używany wyłącznie do klasyfikacji dokumentu (z deterministyczną `temperature: 0`, `max_tokens: 1` i gramatyką `root ::= [A-Z]`), co zapewnia minimalny koszt obliczeniowy, brak halucynacji w strukturze JSON i szybkie działanie na laptopie z 16 GB RAM bez GPU.
- **Obsługa zewnętrznego API (EU/EOG)**: aplikacja komunikuje się z modelem przez standardowe żądania HTTP (`BASAL_URL`, `BASAL_MODEL`, opcjonalnie `BASAL_API_KEY`), dzięki czemu może bez zmian w kodzie korzystać zarówno z lokalnego kontenera `llama.cpp`, jak i z zewnętrznego API kompatybilnego z OpenAI/llama.cpp (`/v1/completions`).
- **Pozostałe komponenty open source**: `Python 3.13`, `poppler-utils` (`pdftotext`, `pdftoppm`), `RapidOCR` (PP-OCRv5), `ONNX Runtime`, `OpenCV`, `NumPy`, `Pillow`, `spylls`, `hunspell-pl`.

---

## 3. Instrukcja uruchomienia

### Opcja A: Docker Compose (zalecane)

Wymagania: standardowy **Docker** + **Docker Compose** (działa na Linux, macOS i Windows bez dodatkowych wtyczek). Przy pierwszym uruchomieniu kontener `basal` automatycznie pobiera i buforuje w wolumenie `llama-cache` model `Remek/basal-2.0-mini-GGUF:Q5_K_M`.

#### 1. Tryb wsadowy (`./input` → `./output`)

Umieść pliki PDF w katalogu `input/` (z dowolną strukturą podkatalogów, np. `input/case1/...`), a następnie uruchom:

```bash
docker compose run --build --rm klasyfikacja
```

Wynikowe pliki JSON zostaną zapisane w `output/` z identyczną strukturą katalogów i nazwami odpowiadającymi plikom PDF (np. `input/case1/pismo.pdf` → `output/case1/pismo.json`). Dokumenty nierozpoznane (klasa `inny`) otrzymują pusty obiekt `{}`.

#### 2. Tryb interaktywny (GUI)

```bash
docker compose --profile gui up --build
```

Aplikacja WWW dostępna jest pod adresem: `http://127.0.0.1:8080`.

GUI umożliwia:
- przesłanie pojedynczego pliku PDF lub zestawu skanów (PNG, JPG, WebP, TIFF, BMP),
- uruchomienie przetwarzania całego katalogu `input/` → `output/` jednym kliknięciem,
- podgląd transparentnego śladu ekstrakcji (źródło tekstu, metoda, wycinek cytatu) i korektę wyekstrahowanych pól,
- dodawanie nowych klas dokumentów oraz reset konfiguracji do ustawień domyślnych.

### Opcja B: Uruchomienie lokalne (Python 3.13+, bez Dockera)

Wymagania systemowe: `Python 3.13+`, `poppler-utils` (`pdftotext`, `pdftoppm`) oraz opcjonalnie słownik `hunspell-pl` (`/usr/share/hunspell/pl_PL.{dic,aff}`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Ustaw zmienne środowiskowe wskazujące na endpoint modelu (lokalny serwer llama.cpp / Docker Model Runner lub zewnętrzne API w EU/EOG):

```bash
export BASAL_URL="http://127.0.0.1:12434/engines/v1"
export BASAL_MODEL="hf.co/Remek/basal-2.0-mini-GGUF:Q5_K_M"
export BASAL_API_KEY=""   # opcjonalnie dla zdalnego API
```

Uruchomienie trybu wsadowego (`./input` → `./output`, można nadpisać przez `INPUT_DIR` i `OUTPUT_DIR`):

```bash
python classify_documents.py
```

Uruchomienie GUI:

```bash
python web.py
```

---

## 4. Konfiguracja ekstrakcji i dodawanie nowej klasy dokumentów

Nowe klasy dokumentów oraz ich pola ekstrakcji konfiguruje się **bez modyfikacji kodu źródłowego** — na dwa sposoby:

### Sposób 1: Edycja pliku `config/documents.json`

Dodaj nowy wpis do tablicy `document_types` w `config/documents.json`:

```json
{
  "id": "wezwanie_kio",
  "label": "Wezwanie do uzupełnienia braków",
  "description": "Pismo z KIO wzywające odwołującego do uzupełnienia braków formalnych odwołania.",
  "schema": {
    "wezwanie": {
      "sygnatura": {
        "type": "string",
        "regex": "Sygn\\.?\\s*akt\\s*:?\\s*(KIO\\s*\\d+/\\d{2,4})"
      },
      "data_pisma": {
        "type": "date",
        "regex": "dnia\\s+(\\d{1,2}[./-]\\d{1,2}[./-]\\d{4})"
      },
      "termin_dni": {
        "type": "integer",
        "regex": "w\\s+terminie\\s+(\\d+)\\s+dni"
      }
    }
  }
}
```

- Obsługiwane typy pól w `schema`: `string`, `date` (konwersja do ISO `YYYY-MM-DD`), `number`, `integer`, `digits`, `boolean` oraz opcjonalna lista `enum`.
- Nowa klasa jest automatycznie uwzględniana w promptach klasyfikatora LLM (maks. 25 klas + klasa `fallback_class`) oraz w generatorze śladu ekstrakcji (`trace`).
- Można również dopisywać dodatkowe pola w `schema` dla istniejących typów — zostaną one automatycznie dołączone do wynikowego JSON-a.

### Sposób 2: Kreator w GUI

1. Uruchom GUI (`docker compose --profile gui up --build`) i otwórz `http://127.0.0.1:8080`.
2. W panelu kreatora podaj identyfikator, nazwę, opis dla klasyfikatora oraz listę pól (nazwa, typ, wzorzec regex) — konfiguracja zapisze się atomowo do `config/documents.json` (`POST /api/types`).
3. W razie potrzeby przywróć konfigurację fabryczną przyciskiem resetu (`POST /api/config/reset`, przywraca zawartość `documents.default.json`).

---

## 5. Testy i walidacja

### Testy jednostkowe (konfiguracja, ekstrakcja ze schematu, trace, edytor GUI)

```bash
python3 -m unittest test_config_loader.py
node --test test_editor.cjs
```

### Pełny test na zbiorze treningowym UZP (`dane/` + `szablon-aplikacja/`)

```bash
./testuj.sh
```

Skrypt porównuje klasyfikację i wygenerowane pliki JSON z wzorcami referencyjnymi dla wszystkich dokumentów tekstowych, skanów oraz przykładów negatywnych.
