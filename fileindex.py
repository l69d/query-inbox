"""A background index of the files the inbox shows, so a list request never walks the disk.

A full walk of a 25k-file workspace took ~6 s. The index keeps every folder's listing and re-lists a folder
only when its mtime changes (a file added, removed or renamed in it); files it already knows are just
re-stat'ed (an edit changes the file's mtime, not its folder's). A refresh is then a few thousand stat
calls, ~50 ms, run every REFRESH_S seconds on a thread. Python environments, .git and node_modules are
never entered. Self-check: python fileindex.py
"""
import os
import threading
import time
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", "site-packages", ".mypy_cache", ".pytest_cache", ".ipynb_checkpoints"}
REFRESH_S = 2.0


def skip_dir(entry):
    name = entry.name
    if name in SKIP_DIRS or name.startswith((".venv", "venv")):
        return True
    return os.path.exists(os.path.join(entry.path, "pyvenv.cfg"))  # any virtualenv, whatever it is called


class FileIndex:
    def __init__(self, root, exts, on_refresh=None):
        self.root, self.exts, self.on_refresh = str(root), tuple(exts), on_refresh
        self.dirs = {}    # dir path -> (mtime, [subdirs], [wanted files])
        self.files = {}   # file path -> (mtime, size)
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def _scan_dir(self, path, dirs, files):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return
        old = self.dirs.get(path)
        if old and old[0] == mtime:
            subdirs, wanted = old[1], old[2]
        else:
            subdirs, wanted = [], []
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if not skip_dir(e):
                                    subdirs.append(e.path)
                            elif e.name.endswith(self.exts) and not e.name.startswith("~$"):  # ~$ = Excel lock file
                                wanted.append(e.path)
                        except OSError:
                            continue
            except OSError:
                return
        dirs[path] = (mtime, subdirs, wanted)
        for f in wanted:
            try:
                st = os.stat(f)
                files[f] = (st.st_mtime, st.st_size)
            except OSError:
                pass
        for d in subdirs:
            self._scan_dir(d, dirs, files)

    def refresh(self):
        dirs, files = {}, {}
        self._scan_dir(self.root, dirs, files)
        with self.lock:
            self.dirs, self.files = dirs, files
        self.ready.set()

    def start(self):
        def loop():
            while True:
                try:
                    self.refresh()
                    if self.on_refresh:  # e.g. pre-build the lists, so even the first page load is instant
                        self.on_refresh()
                except Exception:  # never let the indexer die; the next pass retries
                    pass
                time.sleep(REFRESH_S)
        threading.Thread(target=loop, daemon=True).start()
        return self

    def recent(self, exts, since):
        """[(mtime, size, Path)] for files with one of exts, modified at or after `since`, newest first."""
        self.ready.wait(timeout=30)
        with self.lock:
            items = [(m, s, Path(p)) for p, (m, s) in self.files.items() if m >= since and p.endswith(tuple(exts))]
        return sorted(items, key=lambda x: x[0], reverse=True)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a").mkdir()
        (root / "a" / "q1.sql").write_text("select 1")
        (root / "venvx").mkdir()
        (root / "venvx" / "pyvenv.cfg").write_text("")
        (root / "venvx" / "lib.sql").write_text("select 1")       # inside a virtualenv: never listed
        (root / "~$book.xlsx").write_text("")                     # Excel lock file: never listed
        idx = FileIndex(root, (".sql", ".xlsx"))
        idx.refresh()
        names = lambda: [p.name for _, _, p in idx.recent((".sql", ".xlsx"), 0)]
        assert names() == ["q1.sql"], names()
        time.sleep(0.01)
        (root / "a" / "q2.sql").write_text("select 2")           # new file: its folder's mtime changes
        idx.refresh()
        assert sorted(names()) == ["q1.sql", "q2.sql"], names()
        os.utime(root / "a" / "q1.sql", (time.time() + 5, time.time() + 5))  # edited: newest first
        idx.refresh()
        assert names()[0] == "q1.sql", names()
        (root / "a" / "q2.sql").unlink()
        idx.refresh()
        assert names() == ["q1.sql"], names()
    print("ok")
