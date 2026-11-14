"""Local HTTP interface for the review workbench."""

import json
import logging
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import assist
from .sources import ATTACHMENT_TEXT_EXTENSIONS, read_source, source_path
from .store import ReviewStore

logger = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
ASSETS = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/static/app.js": ("app.js", "application/javascript"),
    "/static/style.css": ("style.css", "text/css"),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug(fmt, *args)

    @property
    def store(self):
        return self.server.store

    def _headers(self, status, ctype, size, attachment=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(attachment)}")
        self.end_headers()

    def _send(self, data, status=200, ctype="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, ctype, len(body))
        self.wfile.write(body)

    def _download(self, path, name, ctype):
        if path is None or not path.is_file():
            return self._send({"error": "没有可下载的文件"}, 404)
        with path.open("rb") as stream:
            self._headers(200, ctype, os.fstat(stream.fileno()).st_size, name)
            shutil.copyfileobj(stream, self.wfile)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if self.headers.get_content_type() != "application/json" or not 0 < length <= 65536:
            raise ValueError("请求格式无效")
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            raise ValueError("请从本工作台页面提交")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("请求格式无效")
        return body

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            self._get(url.path, query)
        except (BrokenPipeError, ConnectionResetError):
            return
        except KeyError:
            self._send({"error": "复核组或记录已不存在"}, 404)
        except ValueError as exc:
            self._send({"error": str(exc)}, 400)
        except Exception:
            logger.exception("GET %s 失败", url.path)
            self._send({"error": "读取失败，请查看服务日志"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            self._post(path, self._body())
        except (BrokenPipeError, ConnectionResetError):
            return
        except (KeyError, ValueError) as exc:
            self._send({"error": str(exc)}, 400)
        except Exception:
            logger.exception("POST %s 失败", path)
            self._send({"error": "保存或读取失败，请查看服务日志"}, 500)

    def _get(self, path, q):
        if path in ASSETS:
            name, mime = ASSETS[path]
            return self._send((STATIC / name).read_bytes(), ctype=f"{mime}; charset=utf-8")
        store = self.store
        case, key = q.get("id", ""), q.get("item", "")
        if path == "/api/overview":
            return self._send(store.overview())
        if path == "/api/cases":
            return self._send(store.query(q.get("category", ""), q.get("status", "open"), q.get("q", ""),
                                          offset=max(0, int(q.get("offset", 0)))))
        if path == "/api/case":
            return self._send(store.detail(case, key, offset=max(0, int(q.get("offset", 0)))))
        if path == "/api/review-evidence":
            with store.lock:
                report = store.max_review(case) or {}
                evidence = report.get("evidence", [])
                index = int(q.get("index", "-1"))
                entry = evidence[index] if 0 <= index < len(evidence) else {}
            source = source_path(store.source_root, entry.get("file"))
            if source is not None:
                extension = source.suffix.casefold()
                if extension == ".pdf":
                    return self._download(source, None, "application/pdf")
                if entry.get("kind") == "attachment" and extension in ATTACHMENT_TEXT_EXTENSIONS:
                    return self._download(source, source.name, "application/octet-stream")
            return self._send({"error": "找不到对应的原件"}, 404)
        if path in {"/api/source", "/api/source-file"}:
            _, item = store.member(case, key)
            if path.endswith("-file"):
                source = source_path(store.source_root, item["file"])
                return self._download(source, Path(item["file"]).name, "application/octet-stream")
            result = read_source(store.source_root, item["file"], item["sheet"],
                                 item["source_row"], item["source_line"], item["column"])
            source = source_path(store.source_root, item["file"])
            result["downloadable"] = bool(source and source.is_file())
            return self._send(result)
        if path == "/api/linked":
            return self._send(store.linked_rows(case, key))
        if path == "/api/export":
            which = q.get("which", "decisions")
            exports = {"decisions": store.decisions_path, "annotations": store.annotations_path}
            with store.lock:
                return self._download(exports.get(which), f"{which}.csv", "text/csv; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(b"", 204)
        return self._send({"error": "页面不存在"}, 404)

    def _post(self, path, body):
        store = self.store
        case, key = body.get("id", ""), body.get("item_key", "")
        if path == "/api/decision":
            return self._send(store.save_decision(body))
        if path == "/api/annotation":
            return self._send(store.save_annotation(case, key, str(body.get("note") or "")))
        if path == "/api/assist":
            group, item = store.member(case, key)
            if group["category"] != "unit":
                raise ValueError("模型辅助当前用于单位与标尺复核")
            model = body.get("model", "flash")
            if model not in {"flash", "max"}:
                raise ValueError("请选择可用模型")
            return self._send(assist.suggest_item(store.source_root, item, model))
        return self._send({"error": "接口不存在"}, 404)


def serve(run_dir, source_root=".", port=8765, open_browser=False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.store = ReviewStore(run_dir, source_root)
        url = f"http://127.0.0.1:{server.server_port}/"
        overview = server.store.overview()
        logger.info("复核工作台：%s · %s · %d 个复核组", url, overview["batch"], overview["cases"])
        if open_browser:
            threading.Timer(0.3, lambda: __import__("webbrowser").open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("工作台已停止")
    return url
