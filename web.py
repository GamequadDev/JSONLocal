from email import message_from_bytes
from email.policy import default as email_policy
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.error import URLError

from PIL import UnidentifiedImageError

from classify_documents import classify_images, classify_pdf_with_meta, process_directory, result_with_trace
from config_loader import public_config, reset_document_config, save_document_type

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/tiff",
    "image/bmp",
})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})
INDEX_FILE = Path(__file__).with_name("index.html")
INPUT_DIR = Path(os.environ.get("INPUT_DIR", INDEX_FILE.parent / "input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", INDEX_FILE.parent / "output"))
ANALYSIS_LOCK = Lock()


def _parse_multipart(content_type_header, body):
    header = f"Content-Type: {content_type_header}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    message = message_from_bytes(header + body, policy=email_policy)
    if not message.is_multipart():
        return []
    files = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        files.append((
            part.get_filename() or "",
            part.get_content_type() or "",
            payload,
        ))
    return files


class AnalysisHandler(BaseHTTPRequestHandler):
    def write_response(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status, value):
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.write_response(status, content, "application/json; charset=utf-8")

    def do_GET(self):
        if self.path == "/":
            self.write_response(HTTPStatus.OK, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self.send_json(HTTPStatus.OK, public_config())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def handle_create_type(self):
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Oczekiwano danych JSON."})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Brakuje rozmiaru żądania."})
            return
        if size <= 0 or size > 2 * 1024 * 1024:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Zbyt duży rozmiar żądania."})
            return
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Niepoprawny JSON."})
            return
        if not isinstance(payload, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Oczekiwano obiektu JSON."})
            return
        try:
            entry = save_document_type(payload)
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except OSError:
            logging.exception("Failed to save documents.json")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Nie udało się zapisać pliku documents.json."})
            return

        doc_text = payload.get("text")
        used_ocr = bool(payload.get("ocr", False))
        resp = {"ok": True, "type": entry["id"], "config": public_config()}
        if isinstance(doc_text, str) and doc_text.strip():
            body_json, trace = result_with_trace(entry["id"], doc_text, used_ocr=used_ocr)
            resp["json"] = body_json
            resp["trace"] = trace
        self.send_json(HTTPStatus.OK, resp)

    def handle_batch(self):
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Oczekiwano żądania JSON."})
            return
        if not ANALYSIS_LOCK.acquire(blocking=False):
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Trwa inna analiza. Spróbuj ponownie za chwilę."})
            return
        saved = 0
        errors = []
        try:
            for item in process_directory(INPUT_DIR, OUTPUT_DIR):
                if "error" in item:
                    errors.append(item)
                else:
                    saved += 1
        except (ValueError, OSError) as error:
            self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        finally:
            ANALYSIS_LOCK.release()
        self.send_json(HTTPStatus.OK, {"saved": saved, "errors": errors})

    def do_POST(self):
        if self.path == "/api/analyze-folder":
            self.handle_batch()
            return
        if self.path == "/api/config/reset":
            if self.headers.get("X-Confirm-Reset") != "yes":
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Potwierdź przywrócenie ustawień."})
                return
            try:
                config = reset_document_config()
            except (OSError, ValueError):
                logging.exception("Configuration reset failed")
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Nie udało się przywrócić ustawień."})
                return
            self.send_json(HTTPStatus.OK, config)
            return
        if self.path == "/api/types":
            self.handle_create_type()
            return
        if self.path != "/api/analyze":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = self.headers.get_content_type()
        if (
            content_type != "application/pdf"
            and content_type not in IMAGE_MIME_TYPES
            and content_type != "multipart/form-data"
        ):
            self.send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Wybierz plik PDF lub skan (PNG, JPG, WebP, TIFF, BMP)."},
            )
            return

        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Brakuje rozmiaru pliku."})
            return
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "Wybierz plik lub zestaw skanów o łącznym rozmiarze do 25 MB."},
            )
            return

        self.connection.settimeout(30)
        try:
            data = self.rfile.read(size)
        except TimeoutError:
            self.send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "Przesyłanie pliku trwało zbyt długo."})
            return

        if len(data) != size:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Przesyłanie pliku zostało przerwane."})
            return

        pdf_bytes = None
        image_payloads = []
        if content_type == "application/pdf":
            if not data.startswith(b"%PDF-"):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Plik jest uszkodzony lub nie jest dokumentem PDF."})
                return
            pdf_bytes = data
        elif content_type in IMAGE_MIME_TYPES:
            image_payloads = [data]
        else:
            files = _parse_multipart(self.headers.get("Content-Type", ""), data)
            if not files:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Nie przesłano żadnego pliku."})
                return
            if len(files) == 1 and (
                files[0][1] == "application/pdf" or files[0][0].lower().endswith(".pdf")
            ):
                if not files[0][2].startswith(b"%PDF-"):
                    self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Plik jest uszkodzony lub nie jest dokumentem PDF."})
                    return
                pdf_bytes = files[0][2]
            else:
                for name, mime, payload in files:
                    ext = Path(name).suffix.lower()
                    if mime not in IMAGE_MIME_TYPES and ext not in IMAGE_EXTENSIONS:
                        self.send_json(
                            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                            {"error": "Możesz przesłać jeden plik PDF albo jeden lub kilka skanów (PNG, JPG, WebP, TIFF, BMP)."},
                        )
                        return
                    image_payloads.append(payload)

        if not ANALYSIS_LOCK.acquire(blocking=False):
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Trwa inna analiza. Spróbuj ponownie za chwilę."})
            return

        try:
            if pdf_bytes is not None:
                with TemporaryDirectory() as directory:
                    pdf = Path(directory) / "document.pdf"
                    pdf.write_bytes(pdf_bytes)
                    label, text, used_ocr = classify_pdf_with_meta(pdf)
            else:
                label, text = classify_images(image_payloads)
                used_ocr = True
        except UnidentifiedImageError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Plik skanu jest uszkodzony lub ma nieobsługiwany format obrazu."})
        except subprocess.SubprocessError:
            logging.exception("PDF processing failed")
            self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Nie udało się odczytać PDF-a. Sprawdź, czy plik się otwiera i czy jest odblokowany."})
        except (URLError, TimeoutError, KeyError):
            logging.exception("Model Runner request failed")
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": "Brak odpowiedzi z Docker Model Runnera. Sprawdź, czy jest uruchomiony."})
        except ValueError as error:
            self.send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
        except OSError:
            logging.exception("Analysis failed")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Nie udało się przetworzyć dokumentu."})
        else:
            body_json, trace = result_with_trace(label, text, used_ocr=used_ocr)
            self.send_json(
                HTTPStatus.OK,
                {"type": label, "json": body_json, "text": text, "ocr": used_ocr, "trace": trace},
            )
        finally:
            ANALYSIS_LOCK.release()


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", 8080), AnalysisHandler) as server:
        server.serve_forever()
