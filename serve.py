"""Query inbox: a localhost page listing the newest .sql files under a folder, with a Copy button each
and a viewer that breaks the query into a flow diagram, steps and highlighted SQL.
.xlsx / .csv inbox (/xlsx): the same for data files -- newest .xlsx and .csv files, each sheet rendered in
the browser (workbooks with their formatting), plus Open in Excel / Show in Finder.

Every editor / agent session writes its queries to disk, so the page is session-agnostic: nothing
rides on the shared clipboard until you click.

    python serve.py --root ~/my-sql-workspace [--port 8765] [--days 3] [--lint my_lint.py]

Deep link to one query: http://127.0.0.1:8765/#p=<path relative to root>[&t=flow|steps|sql]
"""
import argparse
import importlib.util
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import subprocess

from sqlviz import analyze, table_target
from xlsxview import csv_columns, read_csv, read_sheet, sheet_names

PAGE = Path(__file__).with_name("index.html")
XLSX_PAGE = Path(__file__).with_name("xlsx.html")
LIMIT = 60
SKIP = {".git", "node_modules", "__pycache__"}
ROOT = Path.cwd()
DAYS = 3
LINT = None


def recent_files(ext=".sql", describe_fn=None):
    describe_fn = describe_fn or describe
    cutoff = time.time() - DAYS * 86400
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith((".venv", "venv"))]
        for name in filenames:
            if name.endswith(ext) and not name.startswith("~$"):  # ~$ = Excel's lock file
                p = Path(dirpath, name)
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m >= cutoff:
                    found.append((m, p))
    found.sort(reverse=True)
    out = []
    for m, p in found:
        rel = p.relative_to(ROOT)
        worktree = None
        # Claude Code worktrees: show a copy once if it matches the main checkout
        if rel.parts[:2] == (".claude", "worktrees") and len(rel.parts) > 3:
            worktree = rel.parts[2]
            main = ROOT.joinpath(*rel.parts[3:])
            if main.is_file() and main.stat().st_size == p.stat().st_size and main.read_bytes() == p.read_bytes():
                continue
        item = {"path": str(rel), "mtime": m, "worktree": worktree, "desc": describe_fn(p), "size": p.stat().st_size}
        if p.suffix == ".sql":
            try:
                with p.open(errors="replace") as fh:
                    item["table"] = table_target("".join(line for _, line in zip(range(40), fh)))
            except OSError:
                pass
        out.append(item)
        if len(out) == LIMIT:
            break
    return out


def describe(p):
    """First comment line that says something (skips ==== rules and blank --)."""
    try:
        with p.open(errors="replace") as fh:
            for _, line in zip(range(15), fh):
                text = line.strip().lstrip("-").strip()
                if line.lstrip().startswith("--") and text and set(text) - set("=-") and not text.startswith("@"):
                    return text[:160]
    except OSError:
        pass
    return ""


DATA_EXT = (".xlsx", ".csv")


def describe_xlsx(p):
    if p.suffix == ".csv":
        cols = csv_columns(p)
        return f"{len(cols)} column{'s' if len(cols) != 1 else ''}: " + ", ".join(cols) if cols else ""
    names = sheet_names(p)
    return f"{len(names)} sheet{'s' if len(names) != 1 else ''}: " + ", ".join(names) if names else ""


def recent_sql():
    return recent_files(".sql")


def inside_root(rel, ext):
    """A path from the browser, only if it is a file of this type inside the root."""
    p = (ROOT / rel).resolve()
    exts = ext if isinstance(ext, tuple) else (ext,)
    return p if p.suffix in exts and p.is_relative_to(ROOT) and p.is_file() else None


def load_lint(path):
    """Optional: a Python file defining lint(sql) -> list[str], run on every BigQuery-dialect query."""
    spec = importlib.util.spec_from_file_location("user_lint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.lint


class Handler(BaseHTTPRequestHandler):
    def send(self, body, ctype):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self.send(PAGE.read_text(), "text/html")
        q = parse_qs(url.query)
        if url.path == "/xlsx":
            return self.send(XLSX_PAGE.read_text(), "text/html")
        if url.path == "/api/xlsx/list":
            return self.send(json.dumps(recent_files(DATA_EXT, describe_xlsx)), "application/json")
        if url.path == "/api/xlsx/sheet":
            p = inside_root(q.get("p", [""])[0], DATA_EXT)
            if p:
                try:
                    data = read_csv(p) if p.suffix == ".csv" else read_sheet(p, q.get("s", ["0"])[0])
                except Exception as e:  # a workbook openpyxl cannot read shouldn't take the page down
                    data = {"error": f"{type(e).__name__}: {e}"}
                data["mtime"] = p.stat().st_mtime
                return self.send(json.dumps(data), "application/json")
        if url.path == "/api/list":
            return self.send(json.dumps(recent_sql()), "application/json")
        if url.path in ("/api/file", "/api/viz"):
            p = (ROOT / parse_qs(url.query).get("p", [""])[0]).resolve()
            # only .sql files inside the root
            if p.suffix == ".sql" and p.is_relative_to(ROOT) and p.is_file():
                sql = p.read_text(errors="replace")
                if url.path == "/api/file":
                    return self.send(sql, "text/plain")
                try:
                    viz = analyze(sql, p.name, lint=LINT)
                    viz["mtime"] = p.stat().st_mtime
                except Exception as e:  # a parser crash shouldn't take the page down
                    viz = {"dialect": "bigquery", "issues": [], "steps": [],
                           "error": {"message": f"viewer could not analyse this query ({type(e).__name__}: {e})", "line": None, "col": None}}
                return self.send(json.dumps(viz), "application/json")
        self.send_error(404)

    def do_POST(self):
        # Open in Excel / Show in Finder. The custom header makes a cross-site request need a CORS
        # preflight, which this server never answers, so only this page can trigger it.
        url = urlparse(self.path)
        p = inside_root(parse_qs(url.query).get("p", [""])[0], DATA_EXT)
        if url.path in ("/api/xlsx/open", "/api/xlsx/reveal") and p and self.headers.get("X-Inbox") == "1":
            subprocess.Popen(["open", str(p)] if url.path.endswith("open") else ["open", "-R", str(p)])
            return self.send("ok", "text/plain")
        self.send_error(403)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="folder to scan for .sql files (default: current folder)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--days", type=float, default=3, help="list files saved in the last N days")
    ap.add_argument("--lint", help="optional .py file defining lint(sql) -> list[str]")
    args = ap.parse_args()
    ROOT, DAYS = Path(args.root).expanduser().resolve(), args.days
    LINT = load_lint(args.lint) if args.lint else None
    print(f"Query inbox for {ROOT} on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
