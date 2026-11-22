"""Configuration loader, deterministic schema-driven extractor, and extraction trace builder."""
import json
import math
import os
from pathlib import Path
import re
import string
from tempfile import TemporaryDirectory
from threading import Lock

from extraction import MONTHS, appeal, authority, iso, transfer

CONFIG_PATH = Path(os.environ.get("DOCUMENTS_CONFIG", Path(__file__).parent / "config" / "documents.json"))
DEFAULT_CONFIG_PATH = Path(__file__).with_name("documents.default.json")
BUILTIN_EXTRACTORS = {
    "odwolanie": appeal,
    "pelnomocnictwo": authority,
    "przelew": transfer,
}
_CONFIG_WRITE_LOCK = Lock()


def load_config(path=None):
    target = Path(path) if path else CONFIG_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("document_types"), list):
        raise ValueError("Niepoprawny format pliku konfiguracyjnego documents.json.")
    return data


def build_classes(config=None):
    cfg = config or load_config()
    doc_types = list(cfg["document_types"])
    fallback = cfg.get("fallback_class") or {
        "id": "inny",
        "label": "Dokument spoza zakresu",
        "description": "Inny dokument spoza skonfigurowanych klas.",
    }
    all_items = doc_types + [fallback]
    if len(all_items) > len(string.ascii_uppercase):
        raise ValueError("Zbyt wiele klas dokumentów w documents.json (maks. 26).")
    classes = {}
    for code, item in zip(string.ascii_uppercase, all_items):
        classes[code] = (item["id"], item["description"])
    return classes


def public_config(config=None):
    cfg = config or load_config()
    fallback = cfg.get("fallback_class") or {"id": "inny", "label": "Dokument spoza zakresu", "description": ""}
    types = [
        {
            "id": item["id"],
            "label": item.get("label", item["id"]),
            "description": item.get("description", ""),
            "builtin": bool(item.get("builtin_extractor")),
        }
        for item in cfg["document_types"]
    ]
    types.append({
        "id": fallback["id"],
        "label": fallback.get("label", fallback["id"]),
        "description": fallback.get("description", ""),
        "builtin": False,
        "fallback": True,
    })
    return {"document_types": types}


def find_type_config(label, config=None):
    cfg = config or load_config()
    for item in cfg["document_types"]:
        if item["id"] == label:
            return item
    return None


def _validate_schema_node(node):
    if not isinstance(node, dict):
        raise ValueError("Schemat pól musi być obiektem JSON.")
    for key, spec in node.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
            raise ValueError(f"Niepoprawna nazwa pola: {key!r}.")
        if not isinstance(spec, dict):
            raise ValueError(f"Definicja pola {key!r} musi być obiektem.")
        if "type" not in spec and "regex" not in spec and "description" not in spec:
            _validate_schema_node(spec)
            continue
        if spec.get("type", "string") not in {"string", "date", "number", "integer", "digits", "boolean"}:
            raise ValueError(f"Nieobsługiwany typ pola {key!r}: {spec.get('type')!r}.")
        if pattern := spec.get("regex"):
            try:
                re.compile(pattern, re.I | re.M)
            except re.error as err:
                raise ValueError(f"Niepoprawny regex w polu {key!r}: {err}") from err


def save_document_type(type_def, path=None):
    """Validate and atomically save/update a document type in documents.json."""
    with _CONFIG_WRITE_LOCK:
        return _save_document_type(type_def, (Path(path) if path else CONFIG_PATH).resolve())


def _save_document_type(type_def, target):
    cfg = load_config(target)
    fallback_id = (cfg.get("fallback_class") or {}).get("id", "inny")

    type_id = str(type_def.get("id", "")).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", type_id) or type_id == fallback_id:
        raise ValueError("Identyfikator typu musi zaczynać się od litery (a-z, 0-9, _) i być inny niż 'inny'.")
    label = str(type_def.get("label", "")).strip()
    description = str(type_def.get("description", "")).strip()
    if not label or not description:
        raise ValueError("Podaj nazwę oraz opis typu dokumentu.")

    schema = type_def.get("schema") or {}
    if not isinstance(schema, dict) or not schema:
        raise ValueError("Dodaj co najmniej jedno pole do szablonu.")
    _validate_schema_node(schema)

    entry = {
        "id": type_id,
        "label": label,
        "description": description,
        "schema": schema,
    }
    existing_idx = next((i for i, item in enumerate(cfg["document_types"]) if item["id"] == type_id), None)
    if existing_idx is not None:
        if cfg["document_types"][existing_idx].get("builtin_extractor"):
            raise ValueError(f"Nie można nadpisać wbudowanego typu {type_id!r} z kreatora.")
        cfg["document_types"][existing_idx] = entry
    else:
        if len(cfg["document_types"]) >= 25:
            raise ValueError("Osiągnięto limit 25 typów dokumentów.")
        cfg["document_types"].append(entry)

    _write_config(target, cfg)
    return entry


def reset_document_config(path=None):
    target = (Path(path) if path else CONFIG_PATH).resolve()
    with _CONFIG_WRITE_LOCK:
        defaults = load_config(DEFAULT_CONFIG_PATH)
        _write_config(target, defaults)
    return public_config(defaults)


def _write_config(target, cfg):
    serialized = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    with TemporaryDirectory(dir=target.parent) as temp:
        staged = Path(temp) / target.name
        staged.write_text(serialized, encoding="utf-8")
        staged.replace(target)


def _coerce_value(raw, field_spec):
    if raw is None:
        return None
    raw_str = " ".join(str(raw).split()).strip()
    if not raw_str or raw_str.lower() in {"null", "brak", "none"}:
        return None
    ftype = field_spec.get("type", "string")
    if ftype == "date":
        return iso(raw_str)
    if ftype == "digits":
        digits = re.sub(r"\D", "", raw_str.translate(str.maketrans("oOlI", "0011")))
        return digits or None
    if ftype == "number":
        cleaned = re.sub(r"[^\d,.-]", "", raw_str).replace(",", ".")
        try:
            number = float(cleaned)
            return number if math.isfinite(number) else None
        except ValueError:
            return None
    if ftype == "integer":
        cleaned = re.sub(r"[^\d-]", "", raw_str)
        try:
            return int(cleaned)
        except ValueError:
            return None
    if ftype == "boolean":
        return raw_str.lower() in {"1", "true", "tak", "yes"}
    enum = field_spec.get("enum")
    if enum and raw_str not in enum:
        for opt in enum:
            if str(opt).lower() == raw_str.lower():
                return opt
        return None
    return raw_str


def _extract_schema_node(schema_node, text, prefix, metadata, out):
    for key, spec in schema_node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(spec, dict) and "type" not in spec and "regex" not in spec and "description" not in spec:
            if out.get(key) is None:
                out[key] = {}
            if isinstance(out[key], dict):
                _extract_schema_node(spec, text, path, metadata, out[key])
            continue
        if out.get(key) is not None:
            continue
        val = None
        snippet = None
        if isinstance(spec, dict) and spec.get("regex"):
            m = re.search(spec["regex"], text, re.I | re.M)
            if m:
                raw = m.group(1) if m.lastindex else m.group(0)
                val = _coerce_value(raw, spec)
                if val is not None:
                    start, end = m.span(1 if m.lastindex else 0)
                    left = max(text.rfind("\n", 0, start) + 1, start - 80)
                    right = text.find("\n", end)
                    snippet = text[left:right if right >= 0 else None][:220].strip()
        out[key] = val
        metadata[path] = {"method": "config_regex", "field_type": spec.get("type", "string"), "snippet": snippet}


def extract_for_type(label, text, config=None, *, metadata=None):
    """Extract structured JSON dict and per-field method map for a classified document."""
    cfg = config or load_config()
    fallback_id = (cfg.get("fallback_class") or {}).get("id", "inny")
    if label == fallback_id:
        return None, {}

    type_cfg = find_type_config(label, cfg)
    if not type_cfg:
        return None, {}

    if metadata is None:
        metadata = {}
    result = {}
    builtin_name = type_cfg.get("builtin_extractor")
    if builtin_name == "odwolanie":
        evidence = {}
        result = appeal(text, evidence=evidence)
        for path, snippet in evidence.items():
            metadata[path] = {"method": "builtin", "snippet": snippet}
    elif builtin_name in BUILTIN_EXTRACTORS:
        result = BUILTIN_EXTRACTORS[builtin_name](text)

    schema = type_cfg.get("schema") or {}
    if schema:
        _extract_schema_node(schema, text, "", metadata, result)

    return result, {path: info["method"] for path, info in metadata.items()}


def _find_snippet(value, lines):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Build candidate search patterns for the value (ISO dates, formatted numbers/accounts, plain strings)
    patterns = []
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) and iso(s):
        yyyy, mm, dd = s.split("-")
        m_int, d_int = int(mm), int(dd)
        month_pl = MONTHS[m_int - 1]
        patterns.extend([
            re.escape(s),
            rf"\b0?{d_int}[./\s-]+0?{m_int}[./\s-]+{yyyy}\b",
            rf"\b0?{d_int}\s+{month_pl[:4]}\w*\s+{yyyy}\b",
        ])
    elif isinstance(value, int):
        # Built-in integer fields are derived codes and flags.
        return None
    elif isinstance(value, float):
        whole = f"{int(value)}"
        dec = f"{value:.2f}".split(".")[1]
        grouped = re.sub(r"(?<=\d)(?=(\d{3})+$)", "[ \u00a0]?", whole)
        patterns.append(rf"{grouped}[,.]{dec}")
    elif re.fullmatch(r"(?:PL)?\d{9,26}", s):
        body = s[2:] if s.startswith("PL") else s
        spaced = r"[-\s]*".join(body)
        patterns.append((r"(?:PL\s*)?" if s.startswith("PL") else "") + spaced)
    else:
        probe = s[:48]
        patterns.append(re.escape(probe))

    matches = set()
    for pat in patterns:
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            continue
        for line in lines:
            if match := rx.search(line):
                matches.add(line[max(0, match.start() - 80):][:220])
    return matches.pop() if len(matches) == 1 else None


def build_extraction_trace(data, text, methods=None, used_ocr=False, metadata=None):
    """Build transparent field-by-field provenance trace for GUI/audit."""
    if data is None:
        return []
    methods = methods or {}
    metadata = metadata or {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    source_layer = "RapidOCR + Hunspell PL" if used_ocr else "pdftotext"
    trace = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            if not node:
                trace.append({
                    "field": path,
                    "value": [],
                    "method": methods.get(path, "builtin"),
                    "source": source_layer,
                    "snippet": None,
                })
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        else:
            details = metadata.get(path, {})
            field_type = details.get("field_type", "string")
            if "field_type" not in details:
                if path == "przelew_bankowy.kwota":
                    field_type = "number"
                elif isinstance(node, int) or path.endswith((".czynnosc_powod_id", ".prowadzacy_postepowanie", ".przez-pelnomocnika")):
                    field_type = "integer"
            trace.append({
                "field_type": field_type,
                "snippet_inferred": not details,
                "field": path,
                "value": node,
                "method": methods.get(path, "builtin"),
                "source": source_layer,
                "snippet": details["snippet"] if details else _find_snippet(node, lines),
            })

    walk(data, "")
    return trace
