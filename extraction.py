"""Extraction from document content. Reference answers are never runtime inputs."""
from datetime import date
import re

MONTHS = "stycznia lutego marca kwietnia maja czerwca lipca sierpnia września października listopada grudnia".split()
MONTH_MAP = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}
DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{1,2}\s+(?:" + "|".join(MONTH_MAP) + r")\s+\d{4})"
DATE_LOOSE = r"(?:" + DATE + r"|\d{2}\s+\d{2}\s+\d{4})"
WORD = r"[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)?"
NAME = WORD + r"\s+" + WORD
LAWYER = r"(?i:\badw\.|\br\.\s*pr\.|\bradca prawny|\badwokat)\s+(" + NAME + ")"
ACCOUNT = r"(?<!\d)((?:PL\s*)?\d{2}(?:\s*\d{4}){6})(?!\d)"
ACTS = {
    1: 'Decyzja o odrzuceniu oferty',
    2: 'Ogłoszenie wyboru oferty',
    3: 'Decyzja o wykluczeniu wykonawcy',
    4: 'Publikacja specyfikacji warunków zamówienia (SWZ)',
    5: 'Modyfikacja treści SWZ',
    6: 'Odpowiedzi/wyjaśnienia do SWZ',
    7: 'Zaniechanie odrzucenia oferty',
    8: 'Zaniechanie wykluczenia wykonawcy',
    9: 'Decyzja o unieważnieniu postępowania',
    10: 'Zaniechanie unieważnienia postępowania',
    11: 'Opis przedmiotu zamówienia (OPZ)',
    12: 'Projektowane postanowienia umowy',
}


def value(pattern, text, flags=re.I | re.M):
    m = re.search(pattern, text, flags)
    return " ".join(m.group(1).split()) if m else None


def iso(raw):
    if not raw:
        return None
    parts = re.split(r"[\s./-]+", raw.lower())
    if len(parts) != 3:
        return None
    a, b, c = parts
    try:
        month = MONTH_MAP[b] if b in MONTH_MAP else int(b)
        year, day = (a, c) if len(a) == 4 else (c, a)
        return date(int(year), month, int(day)).isoformat()
    except (KeyError, ValueError):
        return None


_OCR_DIGIT_MAP = str.maketrans("oOlI", "0011")


def _clean_digits(raw, valid_lengths):
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw.translate(_OCR_DIGIT_MAP))
    return digits if len(digits) in valid_lengths else None


def identifiers(text):
    d = r"[0-9oOlI]"
    krs = _clean_digits(value(r"\bKRS\s*[:;]?\s*(" + d + r"{10})\b", text), (10,))
    nip = _clean_digits(
        value(
            r"\bN[I1]P\s*[:;]?\s*("
            + d + r"{3}[-\s]?" + d + r"{3}[-\s]?" + d + r"{2}[-\s]?" + d + r"{2}|"
            + d + r"{3}[-\s]?" + d + r"{2}[-\s]?" + d + r"{2}[-\s]?" + d + r"{3}|"
            + d + r"{10})\b",
            text,
        ),
        (10,),
    )
    regon = _clean_digits(value(r"\bREGON\s*[:;]?\s*(" + d + r"{14}|" + d + r"{9})\b", text), (9, 14))
    return {"krs": krs, "nip": nip, "regon": regon}


def transfer(text):
    gap = r"[\s:.·]*"
    amount = value(
        r"\b(?:kwota(?: operacji| przelewu| transakcji)?|warto[śs][ćc]|w kwocie)"
        + gap + r"(?:PLN\s*)?-?([\d \u00a0]+[,.]\d{2})(?!\d|\.\d)",
        text,
    )
    if not amount:
        amount = value(
            r"\b(?:kwota(?: operacji| przelewu| transakcji)?|warto[śs][ćc])[^\n]*\n[^\n]*?(?:PLN\s*)?-?([\d \u00a0]+[,.]\d{2})(?!\d|\.\d)",
            text,
        )
    if not amount:
        amount = value(r"(?:^|[\s:])-?([\d \u00a0]+[,.]\d{2})\s*(?:PLN|z[łlt])\b", text)

    transaction_date = value(
        r"(?:z[il]econa\s+data\s+wykonania|data\s+(?:realizacji|transakcji|operacj[ia]|operaci|wykonania)|potwierdza,\s*[żz]e\s+w\s+dniu)"
        + gap + "(" + DATE_LOOSE + ")",
        text,
    )
    if not transaction_date:
        transaction_date = value(r"\bdata\s+(" + DATE_LOOSE + r")\s*\n\s*wykonania\b", text)
    if not transaction_date:
        transaction_date = value(
            r"(?:data\s+(?:realizacji|operacj[ia]|operaci|transakcji|wykonania))[^\n]*\n[^\n]*?(" + DATE + ")",
            text,
        )

    def account(label):
        number = value(label + gap + r"(?:nr\.?\s*)?" + ACCOUNT, text)
        if not number:
            number = value(label + r"[^\n]*\n[^\n]*?" + ACCOUNT, text)
        return re.sub(r"\s", "", number) if number else None

    sender = account(r"(?:rachunek\s+(?:nadawcy|obci[aą][żz]any|p[łlt]atnika|zleceniodawcy)|z\s+rachunku)")
    recipient = account(r"(?:rachunek\s+(?:odbiorcy|uznawany|beneficjenta|kontrahenta)|na\s+rachunek)")

    # Two-column bank statements identify both columns in their header.
    pair = re.search(
        r"(?:p[łlt]atnik|nadawca|dane nadawcy|zleceniodawca)\s+(?:beneficjent|odbiorca|dane odbiorcy|adresat przelewu)\b(.*?)(?=\bkwota)",
        text, re.I | re.S,
    )
    if pair:
        accounts = re.findall(ACCOUNT, pair[1])
        if len(accounts) == 2:
            sender, recipient = (re.sub(r"\s", "", a) for a in accounts)

    if not sender:
        sender_section = re.search(
            r"dane\s+(?:nadawcy|zleceniodawcy)\b([\s\S]+?)(?=dane\s+(?:odbiorcy|kontrahenta)|\Z)",
            text, re.I,
        )
        if sender_section:
            m_acc = re.search(ACCOUNT, sender_section[1])
            if m_acc:
                sender = re.sub(r"\s", "", m_acc.group(1))
    if not sender:
        sender = account(r"numer\s+rachunku")

    if not recipient:
        recip_section = re.search(
            r"dane\s+(?:odbiorcy|kontrahenta)\b([\s\S]+?)(?=dane\s+zlecenia|niniejsze|dokument|\Z)",
            text, re.I,
        )
        if recip_section:
            m_acc = re.search(ACCOUNT, recip_section[1])
            if m_acc:
                recipient = re.sub(r"\s", "", m_acc.group(1))

    title_stop = (
        r"(?=\n\s*(?:kwota|warto[śs][ćc]|data\s+(?:operacj|transakcj|wystawienia|wykonania|:)|"
        r"nr\s+ref|numer|identyfikator|status|waluta|typ\s+(?:operacji|transakcji|przelewu)|"
        r"kana[łlt]|tryb|zrealizowano|potwierdzenie|signature\s+not|dokument\s+(?:elektroniczny|sporz|podpisany|jest)|"
        r"strona\s+\d|bank\s+centralny|credo\s+bank|[-*]{3})|\Z)"
    )
    title = value(
        r"^\s*(?:tytu[łltf](?:em|\s+przelewu|\s+p[łlt]atno[śs]ci)?|szczeg[oó][łlt]y\s+p[łlt]atno[śs]c\w*)\b[\s:.·]*([\s\S]+?)"
        + title_stop,
        text,
    )
    if not title:
        title = value(r"^\s*opis(?:\s+operacji)?\b[\s:.·]*([\s\S]+?)" + title_stop, text)
    if not title:
        title = value(
            r"^\s*((?:wpis\s+od\s+odwo[łlt]ania|op[łlt]ata\s+skarbowa\s+od\s+pe[łlt]nomocnictwa)[\s\S]+?)"
            + title_stop,
            text,
        )

    tx_num = value(
        r"(?:nr\s+ref\.?|numer\s+(?:referencyjny|transakcji(?:\s+operacji)?)|identyfikator\s+(?:operacji|transakcji))"
        + gap + r"([A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*)\b",
        text,
    )
    if not tx_num:
        tx_num = value(r"\bnumer\s+([A-Z0-9/-]*\d[A-Z0-9/-]*)\s*\n\s*transakcji\b", text)
    if not tx_num:
        tx_num = value(r"numer\s+referencyjny\s+transakcji[^\n]*\n[^\n]*?\b([A-Z]{2,}[0-9]{6,}[A-Z0-9]*)\b", text)
    if not tx_num:
        tx_num = value(r"\b(CEN\d{10,})\b", text)

    return {
        "przelew_bankowy": {
            "data_operacji": iso(transaction_date),
            "kwota": abs(float("".join(amount.split()).replace(",", "."))) if amount else None,
            "na_rachunek": recipient,
            "z_rachunku": sender,
            "tytul": title.removeprefix("„").removesuffix("”.") if title else None,
            "numer_transakcji": tx_num,
        }
    }


def authority(text):
    agents = {}
    for match in re.finditer(LAWYER, text):
        name = " ".join(match[1].split())
        pesel = value(r"^[\s,(]*PESEL\s*:?\s*(\d{11})\b", text[match.end():])
        agents.setdefault(name, {"imie_nazwisko": name, "pesel": None})
        if pesel:
            agents[name]["pesel"] = pesel

    # Also parse explicit "udziela pełnomocnictwa: ... do reprezentowania" lists (common on scans).
    grant = re.search(
        r"(?i)udziela(?:m|j[aą])?\s+pe[łlt]nomocnictwa\s*:?\s*([\s\S]+?)(?=\bdo\s+reprezentowania\b|\Z)",
        text,
    )
    if grant:
        grant_block = grant[1]
        # 1) Employees/agents with PESEL in parentheses: "Anna Topolska (..., PESEL: 12345678901)"
        for m in re.finditer(r"(" + NAME + r")\s*\([^)]*PESEL\s*:?\s*(\d{11})\b", grant_block):
            name = " ".join(m[1].split())
            agents.setdefault(name, {"imie_nazwisko": name, "pesel": None})
            agents[name]["pesel"] = m[2]
        # 2) Numbered list with title after name: "1. Mateusz Wierzbicki – adwokat, nr wpisu ..., PESEL: ..."
        for m in re.finditer(r"(?m)^\s*\d+[.)]\s*(" + NAME + r")\s*[-–—]\s*(?i:adwokat|radca\s+prawny)([^\n]*)", grant_block):
            name = " ".join(m[1].split())
            agents.setdefault(name, {"imie_nazwisko": name, "pesel": None})
            pesel = value(r"PESEL\s*:?\s*(\d{11})\b", m[2])
            if pesel:
                agents[name]["pesel"] = pesel
        # 2b) Inline parenthetical lawyers: "Grzegorz Kaczmarek (adwokat, nr wpisu ...)"
        for m in re.finditer(r"(" + NAME + r")\s*\(\s*(?i:adwokat|radca\s+prawny)\b([^)]*)\)", grant_block):
            name = " ".join(m[1].split())
            agents.setdefault(name, {"imie_nazwisko": name, "pesel": None})
            pesel = value(r"PESEL\s*:?\s*(\d{11})\b", m[2])
            if pesel:
                agents[name]["pesel"] = pesel
        # 3) Plain name-only lines inside the grant block (when nothing else matched yet)
        if not agents:
            for m in re.finditer(r"(?m)^\s*(" + NAME + r")\s*$", grant_block):
                name = " ".join(m[1].split())
                if not re.search(r"(?i)mocodawc|krajow|izb|zarz[aą]d|pe[łlt]nomocnictw", name):
                    agents.setdefault(name, {"imie_nazwisko": name, "pesel": None})

    role_re = r"(?i:prezes|wiceprezes|cz[łlti]onek|prokurent|dyrektor|wsp[oó]lnik)"

    def signed_by(block):
        names = []
        lines = [line for line in block.splitlines() if not re.fullmatch(r"\s*miejsce\s+na\s+piecz[eę][ćc](?:\s+firmow[aą])?\s*", line, re.I)]
        for prev, curr in zip(lines, lines[1:]):
            if re.match(r"^\s*" + role_re, curr):
                cleaned = re.sub(LAWYER, "", prev)
                names.extend(re.findall(r"\b(" + NAME + r")\b", cleaned))
        if not names:
            # Prefer explicit "reprezentowana przez:" order if present, else "Za Mocodawcę:"
            rep = re.search(r"(?i)repre[zż]entowan[ay]\s+przez\s*:?\s*([\s\S]+?)(?=\(dalej|niniejszym|\budziela|\Z)", block)
            rep_names = [" ".join(n.split()) for n in re.findall(r"(" + NAME + r")\s*[-–—]\s*" + role_re, rep[1])] if rep else []
            za = re.search(r"(?i)za\s+m[oe]codawc[eę]\s*:?\s*([\s\S]+?)(?=\n\s*(?:kancelaria|www\.|ul\.|nip\b)|\Z)", block)
            za_names = [" ".join(n.split()) for n in re.findall(r"(" + NAME + r")\s*[-–—]\s*" + role_re, za[1])] if za else []
            if rep_names and (not za_names or set(rep_names) >= set(za_names)):
                names = rep_names
            elif za_names:
                names = za_names
            else:
                names = re.findall(r"(" + NAME + r")\s*[-–—]\s*" + role_re, block)
        if not names:
            names = re.findall(r"(?i)dokument\s+podpisany\s+przez\s+(" + NAME + r")", block)
        return [{"imie_nazwisko": " ".join(n.split())} for n in dict.fromkeys(names)]

    signatures = list(re.finditer(r"(?im)^w imieniu\s+([^:\n]+):\s*$", text))
    if len(signatures) > 1:
        principals = []
        introduction = " ".join(text[:signatures[0].start()].split())
        for i, signature in enumerate(signatures):
            name = signature[1].strip()
            registry = value(re.escape(name) + r",\s*(.*?)(?:;\s*\d+\)|\(dalej|\budzielają|\Z)", introduction) or ""
            ids = identifiers(registry)
            section = text[signature.end():signatures[i + 1].start() if i + 1 < len(signatures) else None]
            principals.append({
                "data": {
                    "nazwa": name, "imie_nazwisko": None,
                    "typ": "osoba_prawna" if ids["krs"] else None, "nip": ids["nip"], "krs": ids["krs"],
                },
                "podpisujacy": signed_by(section),
            })
        return {"mocodawca": principals, "pelnomocnicy": list(agents.values())}

    # Skip letterhead before "PEŁNOMOCNICTWO" when mocodawca is defined in the body (or header is a law firm)
    body_start = re.search(r"(?im)^\s*pe[łlt]nomocnictwo\s*$", text)
    if body_start:
        after = text[body_start.end():]
        pre_grant_after = re.split(r"(?i)ustanawiam pe[łlt]nomocnikiem|udziela(?:m|j[aą])?\s+pe[łlt]nomocnictwa|§\s*2\.|\bpe[łlt]nomocnicy\b", after, maxsplit=1)[0]
        body = after if (re.search(r"(?i)kancelari", text[:body_start.start()]) or re.search(r"(?i)z\s+siedzib[aą]", pre_grant_after)) else text
    else:
        body = text

    signers = signed_by(body)
    principals = []
    legal_suffix = (
        r"(?:S\.A\.|sp\.\s*(?:z\s+o\.o\.?(?:\s+sp\.\s*k\.)?|[jkp]\.)|"
        r"sp[oó][łlt]k[aą]\s+z\s+ograniczon[aą]\s+odpowiedzialno[śsš]ci[aą]|"
        r"sp[oó][łlt]k[aą]\s+akcyjn[aą])"
    )
    # Handle consortium powers of attorney with numbered principals "1) ... 2) ..."
    pre_grant = re.split(r"(?i)ustanawiam pe[łlt]nomocnikiem|udziela(?:m|j[aą])?\s+pe[łlt]nomocnictwa|§\s*2\.|\bpe[łlt]nomocnicy\b", body, maxsplit=1)[0]
    numbered_principals = list(re.finditer(r"(?m)^\s*\d+\)\s*([\s\S]+?)(?=\n\s*\d+\)\s*|\(dalej|\Z)", pre_grant))
    if len(numbered_principals) > 1 and re.search(r"(?i)konsorcjum|wsp[oó]lnie", pre_grant):
        for match in numbered_principals:
            part = match[1].strip()
            company = value(r"^([^\n,]{2,140}?\s+" + legal_suffix + r")(?=,|\s|$)", part)
            ids = identifiers(part)
            part_signers = signed_by(part)
            if company or ids["krs"]:
                principals.append({
                    "data": {
                        "nazwa": company, "typ": "osoba_prawna",
                        "krs": ids["krs"], "nip": ids["nip"], "imie_nazwisko": None,
                    },
                    "podpisujacy": part_signers,
                })
        return {"mocodawca": principals, "pelnomocnicy": list(agents.values())}
    for block in re.split(r"(?im)^\s*(?:\d+[.)]\s*)?Mocodawca\s*\d+\s*:", body):
        name = value(r"(?:Mocodawcą jest|do reprezentacji|^Mocodawca\s*:?)\s+(.{2,160}?)(?=,?\s+ul\.|\s+\(KRS|\s+z siedzib[aą]|\n)", block)
        if name and name.endswith(" do"):
            name = None
        issuer_block = re.split(r"(?i)ustanawiam pe[łlt]nomocnikiem|udziela(?:m|j[aą])?\s+pe[łlt]nomocnictwa|§\s*2\.|\bpe[łlt]nomocnicy\b", block, maxsplit=1)[0]
        company = value(r"^(?:[ \t]*)(?:\d+[.)]\s*)?([^\n,]{2,140}?\s+" + legal_suffix + r")(?=,|\s|$)", issuer_block)
        name = re.sub(r"(?i)^(?:mocodawc(?:ą jest|a)|działając w imieniu)\s*:?\s*", "", company) if company else name
        if name and re.match(r"(?i)ustanawiam|upoważniam|udziela|jest\b", name):
            name = None
        ids = identifiers(issuer_block[:1600])
        person = value(r"(?i:mocodawc[aą](?: jest)?\s*:?)\s+(" + NAME + r")(?=\s*[,(\n])", block, re.M)
        sole_trader = value(r"(?i:niżej podpisan[ay])\s+(" + NAME + r")(?=,)", issuer_block, re.M)
        business = value(r"pod firmą\s+([^,]+)", issuer_block) if sole_trader else None
        if sole_trader:
            person, name = sole_trader, business
            if re.search(r"(?im)^mocodawca:\s*[.·_]+\s*" + re.escape(person) + r"\s*$", block):
                signers = [{"imie_nazwisko": person}]
        legal = bool(name and re.search(r"(?i)\bsp\.|\bs\.a\.|sp[oó][łlt]k", name))
        if name or person or ids["krs"]:
            principals.append({
                "data": {
                    "nazwa": name if business or not person or legal else None,
                    "typ": "osoba_prawna" if legal else "osoba_fizyczna" if person else None,
                    "krs": ids["krs"], "nip": ids["nip"], "imie_nazwisko": person if not legal else None,
                },
                "podpisujacy": signers if len(re.findall(r"(?im)^Mocodawca\s*\d+\s*:", body)) < 2 else [],
            })
    if not principals and signers:
        ids = identifiers(body[:1600])
        if ids["nip"] and not ids["krs"]:
            principals.append({
                "data": {
                    "nazwa": None, "typ": "osoba_fizyczna",
                    "krs": None, "nip": ids["nip"], "imie_nazwisko": signers[0]["imie_nazwisko"],
                },
                "podpisujacy": signers,
            })
    return {"mocodawca": principals, "pelnomocnicy": list(agents.values())}


ACTION_PATTERNS = [
    (7, r"(?:nie odrzuc|zaniech\w* odrzuc)"),
    (8, r"(?:nie wyklucz|zaniech\w* wyklucz)"),
    (10, r"zaniech\w* uniewa[żzž]ni"),
    (9, r"uniewa[żzž]ni"),
    (1, r"odrzuc"),
    (3, r"wyklucz"),
    (2, r"wyb[oó]r|wyborze"),
    (6, r"wyja[śs]ni|odpowiedzi"),
    (5, r"zmodyfik|modyfik"),
    (12, r"projektowan\w* postanow"),
    (11, r"opis\w* przedmiotu"),
    (4, r"specyfikac|publikacj\w* SWZ|\bSWZ\b"),
]


def _extract_entity_name(block):
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) > 1 and re.search(r"(?i)pod\s+firm[aą]\s*$", lines[0]):
        name = value(r"^(.+?)(?=,?\s+ul\.|\s+KRS\b|$)", lines[1])
    else:
        name = value(r"^(.+?)(?=,?\s+ul\.|\s+KRS\b|$)", lines[0])
        if name and len(lines) > 1 and re.search(r"(?i)\b(?:w|z|i|dla|do|na|o|wraz)$", name):
            nxt = lines[1]
            if not re.match(r"(?i)^(?:ul\.|al\.|pl\.|krs\b|nip\b|regon\b|tel\.|e-?mail|\d{2}-\d{3})", nxt):
                cont = value(r"^(.+?)(?=,?\s+ul\.|\s+KRS\b|$)", nxt)
                if cont:
                    name = f"{name} {cont}"
    if name:
        name = re.sub(r"\s*\((?i:lider\s+konsorcjum|partner\s+konsorcjum)\)\s*$", "", name).strip()
    return name


def appeal(text, evidence=None):
    city_date = re.search(r"(?m)^[ \t]*(" + WORD + r"(?:[ -]" + WORD + r")*),?\s+(?:dnia\s+)?(" + DATE + ")", text[:4500])
    first = re.split(r"(?i)zarzut\s+(?:nr\s+)?2\b", text, maxsplit=1)[0]
    first_clean = re.sub(r"(?im)^\s*(?:Strona\s+\d+\s+z\s+\d+|Odwołanie\s*[–-]\s*postępowanie\s+nr[^\n]*)\s*$", "", first)
    act = re.search(r"(?i)zamawiaj[aą]cy\s+w\s+dniu\s+(" + DATE + r")\s+(?:roku|r\.)?\s*([^.;]{1,320})", first_clean)
    poleg = value(r"polegaj[aą]cych\s+na:\s*1[.)]\s*([\s\S]+?)(?=;|\n\s*2[.)]|\Z)", first_clean)
    wobec = value(r"wobec\s*:[\s\S]{0,400}?(?m:^\s*1[.)]\s*)([\s\S]+?)(?=;|\n\s*2[.)]|\Z)", first_clean) if not poleg else None
    primary = poleg or wobec
    if primary:
        action = primary
        action_date = value(r"(?:w\s+dniu|z\s+dnia|pismem\s+z)\s+(" + DATE + ")", primary) or (act[1] if act else None)
    else:
        action = act[2] if act else None
        action_date = act[1] if act else None
    reason = None
    normalized_action = " ".join((action or "").split())
    for code, pattern in ACTION_PATTERNS:
        match = re.search(pattern, normalized_action, re.I)
        if match:
            reason = code
            if evidence is not None:
                left = max(0, match.start() - 80)
                snippet = normalized_action[left:left + 220]
                evidence["OdwolanieKIO.czynnosc_powod_id"] = snippet
                evidence["OdwolanieKIO.czynnosc_opis"] = snippet
            break
    info = {
        "data": iso(city_date[2]) if city_date else None,
        "miejscowosc": city_date[1] if city_date else None,
        "czynnosc_dzien": iso(action_date) if action_date else None,
        "czynnosc_powod_id": reason,
        "czynnosc_opis": ACTS.get(reason),
    }
    parties, buyers = [], []
    headers = list(re.finditer(r"(?im)^[ \t]*(?:\d+[.)]\s*)?(Odwo[łlt]uj[aą]cy|Zamawiaj[aą]cy)([^\n:]{0,90}):[ \t]*", text))
    for index, header in enumerate(headers):
        block = text[header.end():headers[index + 1].start() if index + 1 < len(headers) else None].strip()
        block = re.split(r"(?im)^\s*(?:dotyczy:|wykonawca wybrany|OD\s*WO[ŁLT]AN\s*I?\s*E\s*$|Dzia[łlt]aj[aą]c w imieniu)", block)[0]
        if not block or header[2].strip().startswith("("):
            continue
        is_appellant = header[1].lower().startswith("odwo")
        if not is_appellant:
            joint_items = list(re.finditer(r"(?m)^\s*\d+[.)]\s*([\s\S]+?)(?=\n\s*\d+[.)]\s*|\Z)", block))
            if len(joint_items) > 1 and re.search(r"(?i)wsp[oó]lnie", block):
                for item in joint_items:
                    part = item[1].strip()
                    name = _extract_entity_name(part)
                    row = {**identifiers(part), "nazwa": name}
                    is_lead = bool(re.search(r"(?i)prowadz[aą]cy\s+post[eę]powanie|lider", part))
                    row["prowadzacy_postepowanie"] = 1 if is_lead else 0
                    buyers.append(row)
                continue
        leader = re.search(r"(?im)^\s*\d+[.)]\s*lider konsorcjum:\s*([\s\S]+?)(?=\n\s*\d+[.)]|\Z)", block)
        if not leader and re.match(r"(?i)konsorcjum\b", block):
            leader = re.search(r"(?m)^\s*1[.)]\s*([\s\S]+?)(?=\n\s*2[.)]|\Z)", block)
        party_info = leader[1] if leader else block
        name = _extract_entity_name(party_info)
        row = {**identifiers(party_info), "nazwa": name}
        if is_appellant:
            agent = value(LAWYER, block, re.M)
            row["przez-pelnomocnika"] = 1 if agent else 0
            law_firm = value(r"(?:adres\s+do\s+korespondenc\w*:\s*)(Kancelaria[^,]+)", block)
            if law_firm:
                law_firm = re.split(r"\s+(?:ul\.|al\.|pl\.)\s+", law_firm, maxsplit=1)[0].strip()
            if not law_firm:
                law_firm = value(r"adres\s+do\s+korespondenc\w*\s*:\s*([^\n]+?)(?=,?\s+(?:ul\.|al\.|pl\.)|$)", block)
            row["pelnomocnik"] = {
                "imie_nazwisko": agent,
                "nazwa": law_firm,
                "mail": value(r"([\w.+-]+@[\w.-]+\.\w+)", block) if agent else None,
            }
            parties.append(row)
        else:
            role = header[2].lower()
            row["prowadzacy_postepowanie"] = 1 if "lider" in role else 0 if "partner" in role else None
            buyers.append(row)

    bzp = value(r"\b(\d{4}/BZP\s*[0-9oOlI]+(?:/[0-9oOlI]+)?)", text)
    if bzp:
        head, tail = bzp.split("BZP", 1)
        bzp = head + "BZP" + re.sub(r"\s", "", tail.translate(_OCR_DIGIT_MAP))
    ted = value(r"(?:TED|Unii\s+Europejskiej|Dzienniku\s+Urz[eę]dowym\s+(?:UE|Unii\s+Europejskiej))\s*(?:nr|:)?\s*(\d{6}-\d{4})", text)
    alternate = re.search(r"\b(\d{4})/S\s*\d{1,3}-(\d{6})", text)
    if alternate and not ted:
        ted = alternate[2] + "-" + alternate[1]
    return {
        "OdwolanieKIO": info,
        "Odwolujacy": parties,
        "Zamawiajacy": buyers,
        "ZamowieniePubliczne": {
            "numer_bzp": bzp,
            "numer_ted": ted,
            "numer_postepowania": value(r"(?:nr\s+referencyjny(?:\s+sprawy)?|numer\s+post[eę]powania)\s*:\s*([\w./-]+)", text),
            "tytul": value(r'pn\.\s*[„"]([^”"]{1,260}?)(?=[”"]|\s*(?:,\s*nr\s+referencyjny|og[łlt]oszenie\s+w\s+Dzienniku|Wpis\s+od\s+odwo|ODWO[ŁLT]ANIE))', text),
        },
    }
