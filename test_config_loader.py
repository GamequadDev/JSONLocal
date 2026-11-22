import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config_loader as cfg


def template(name, schema):
    return {"id": name, "label": name, "description": name, "schema": schema}


class ExtractionTests(unittest.TestCase):
    def trace(self, label, text, config=None):
        metadata = {}
        data, methods = cfg.extract_for_type(label, text, config, metadata=metadata)
        trace = cfg.build_extraction_trace(data, text, methods, metadata=metadata)
        return data, {row["field"]: row for row in trace}

    def test_configured_fields_use_their_own_match(self):
        schema = {"korekta": {
            "kwota": {"type": "number", "regex": r"Kwota: ([-\d,]+)"},
            "powod": {"type": "integer", "regex": r"Powód: (\d+)"},
            "ilosc": {"type": "integer"},
        }}
        config = {"document_types": [template("korekta", schema)]}
        data, trace = self.trace("korekta", "Data: 2026-11-21\nPozycja: 1\nPowód: 1\nKwota: -125,50", config)
        self.assertEqual(data["korekta"], {"kwota": -125.5, "powod": 1, "ilosc": None})
        self.assertEqual(trace["korekta.powod"]["snippet"], "Powód: 1")
        self.assertEqual(trace["korekta.ilosc"]["field_type"], "integer")

    def test_invalid_date_like_text(self):
        for code in ("2026-13-01", "2026-00-01", "2026-02-30"):
            text = f"Kod: {code}"
            trace = cfg.build_extraction_trace({"kod": code}, text)
            self.assertEqual(trace[0]["snippet"], text)

    def test_builtin_evidence_and_nullable_types(self):
        _, trace = self.trace("odwolanie", "Data: 2026-11-21\nZamawiający w dniu 20.11.2026 r. odrzucił ofertę.")
        reason = trace["OdwolanieKIO.czynnosc_powod_id"]
        self.assertEqual((reason["value"], reason["snippet"]), (1, "odrzucił ofertę"))
        _, trace = self.trace("przelew", "Brak kwoty")
        self.assertEqual(trace["przelew_bankowy.kwota"]["field_type"], "number")
        self.assertIsNone(trace["przelew_bankowy.kwota"]["value"])

    def test_failed_save_can_be_retried_and_concurrent_writes_survive(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "documents.json"
            original = '{"document_types": []}'
            path.write_text(original)
            entry = template("nowy", {"kod": {"type": "string"}})
            with patch.object(cfg, "CONFIG_PATH", path):
                with patch.object(Path, "replace", side_effect=OSError("write failed")):
                    with self.assertRaises(OSError):
                        cfg.save_document_type(entry)
                self.assertEqual(path.read_text(), original)
                self.assertIsNone(cfg.find_type_config("nowy"))
                with ThreadPoolExecutor(2) as pool:
                    list(pool.map(cfg.save_document_type, [entry, dict(entry, id="drugi")]))
                ids = {item["id"] for item in json.loads(path.read_text())["document_types"]}
                self.assertEqual(ids, {"nowy", "drugi"})

    def test_reset_restores_full_defaults_and_is_repeatable(self):
        with TemporaryDirectory() as temp:
            target = Path(temp) / "documents.json"
            defaults = cfg.load_config(cfg.DEFAULT_CONFIG_PATH)
            modified = cfg.load_config(cfg.DEFAULT_CONFIG_PATH)
            modified["fallback_class"]["label"] = "Zmieniono"
            modified["document_types"][0]["schema"] = {"nr": {"type": "integer"}}
            modified["document_types"].append(template("wlasny", {"rok": {"type": "integer"}}))
            target.write_text(json.dumps(modified))
            for _ in range(2):
                self.assertEqual(cfg.reset_document_config(target), cfg.public_config(defaults))
                self.assertEqual(cfg.load_config(target), defaults)
