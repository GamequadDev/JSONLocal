import json
import os
from pathlib import Path
import subprocess
import sys

from classify_documents import classify_pdf, result_json


TYPY_UZP = {
    "odwolanie": "odwolanie",
    "pelnomocnictwo": "pelnomocnictwo",
    "potwierdzenie_przelewu": "przelew",
    "przelew": "przelew",
}
TYPY_SKANOW = {
    "odwolania": "odwolanie",
    "pelnomocnictwa": "pelnomocnictwo",
    "potwierdzenia": "przelew",
}
PRZYKLADY_SZABLONU = {
    Path("case1/negatywny przyklad.pdf"): "inny",
    Path("case1/przyklad odwolanie.pdf"): "odwolanie",
}
BRAK = object()


def pokaz(wartosc):
    if wartosc is BRAK:
        return "<brak>"
    return json.dumps(wartosc, ensure_ascii=False)


def roznice(wzorzec, wynik, pole="$"):
    if isinstance(wzorzec, dict) and isinstance(wynik, dict):
        for klucz in sorted(wzorzec.keys() | wynik.keys()):
            yield from roznice(
                wzorzec.get(klucz, BRAK),
                wynik.get(klucz, BRAK),
                f"{pole}.{klucz}",
            )
    elif isinstance(wzorzec, list) and isinstance(wynik, list):
        for index in range(max(len(wzorzec), len(wynik))):
            yield from roznice(
                wzorzec[index] if index < len(wzorzec) else BRAK,
                wynik[index] if index < len(wynik) else BRAK,
                f"{pole}[{index}]",
            )
    elif wzorzec != wynik or isinstance(wzorzec, bool) != isinstance(wynik, bool):
        yield f"{pole}: oczekiwano {pokaz(wzorzec)}, otrzymano {pokaz(wynik)}"


def obiekt_json(tekst):
    dane = json.loads(tekst)
    if not isinstance(dane, dict):
        raise ValueError("oczekiwano obiektu JSON")
    return dane


def sprawdz(pdf, oczekiwany_typ, plik_wzorca):
    if oczekiwany_typ is None:
        return "BŁĄD TESTU", ["Nieznany katalog typu skanu."]

    if plik_wzorca is not None:
        try:
            wzorzec = obiekt_json(plik_wzorca.read_text(encoding="utf-8"))
        except (OSError, ValueError) as blad:
            return "BŁĄD TESTU", [f"Wzorzec {plik_wzorca.name}: {blad}"]

    try:
        typ, tekst = classify_pdf(pdf)
        if typ != oczekiwany_typ:
            return "BŁĘDNY TYP", [f"Oczekiwano {oczekiwany_typ}, otrzymano {typ}."]
        wynik = result_json(typ, tekst)
    except (OSError, subprocess.SubprocessError, ValueError) as blad:
        return "BŁĄD ANALIZY", [str(blad)]

    try:
        dane = obiekt_json(wynik)
    except ValueError as blad:
        return "RÓŻNICE W JSON", [f"Niepoprawny JSON: {blad}"]
    if typ == "inny":
        if dane:
            return "RÓŻNICE W JSON", ["Oczekiwano pustego obiektu JSON {}."]
        return "OK", []
    if plik_wzorca is not None:
        szczegoly = list(roznice(wzorzec, dane))
        if szczegoly:
            return "RÓŻNICE W JSON", szczegoly
    return "OK", []


def uruchom_testy(katalog, testy):
    statystyki = dict.fromkeys(
        ("OK", "BŁĘDNY TYP", "RÓŻNICE W JSON", "BŁĄD ANALIZY", "BŁĄD TESTU"), 0
    )
    for index, (pdf, typ, wzorzec) in enumerate(testy, 1):
        status, szczegoly = sprawdz(pdf, typ, wzorzec)
        statystyki[status] += 1
        print(f"[{index}/{len(testy)}] {status}  {pdf.relative_to(katalog)}")
        for szczegol in szczegoly:
            print(f"    {szczegol}")
    print(" | ".join(f"{status}: {liczba}" for status, liczba in statystyki.items()))
    return statystyki["OK"] == len(testy)


def main(dane=Path("/input"), szablon=Path("/przyklady")):
    model = os.environ.get("BASAL_MODEL")
    if not model or not os.environ.get("BASAL_URL"):
        print("Brak konfiguracji Docker Model Runnera. Uruchom ./testuj.sh.", file=sys.stderr)
        return 1

    pdfs = sorted(pdf for pdf in dane.rglob("*") if pdf.is_file() and pdf.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"Brak plików PDF w {dane}.", file=sys.stderr)
        return 1
    testy_uzp = []
    testy_skanow = []
    for pdf in pdfs:
        katalogi = pdf.relative_to(dane).parts[:-1]
        if katalogi[:1] == ("skany",):
            typ = TYPY_SKANOW.get(katalogi[1]) if len(katalogi) > 1 else None
            testy_skanow.append((pdf, typ, pdf.with_suffix(".json")))
        else:
            typ = TYPY_UZP.get(pdf.stem, "inny")
            wzorzec = None if typ == "inny" else pdf.with_name(f"{typ}.json")
            testy_uzp.append((pdf, typ, wzorzec))

    print(f"Model: {model}")
    print(f"Zbiór UZP ({len(testy_uzp)} PDF-ów)")
    wynik_uzp = uruchom_testy(dane, testy_uzp)
    wynik_skanow = True
    if testy_skanow:
        print(f"\nSkany UZP ({len(testy_skanow)} PDF-ów)")
        wynik_skanow = uruchom_testy(dane, testy_skanow)
    print("\nPrzykłady z szablonu (typ dokumentu i format wyniku)")
    testy_szablonu = [(szablon / plik, typ, None) for plik, typ in PRZYKLADY_SZABLONU.items()]
    wynik_szablonu = uruchom_testy(szablon, testy_szablonu)
    return int(not (wynik_uzp and wynik_skanow and wynik_szablonu))


if __name__ == "__main__":
    sys.exit(main())
