"""本地答案记忆：记住平台判定过的答案，下次作答优先复用或避开。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime

from MoocMain.log import Logger

logger = Logger(__name__).get_log()

MAX_WRONG = 20
# Platform-confirmed answers shipped with the repo; read-only, local memory wins.
SHARED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared_answers.json")


class AnswerMemory:
    def __init__(self, path, shared=None):
        self.path = None if path is None else _path(path)
        self.shared = None if shared is None else _path(shared)
        self._lock = threading.RLock()
        self._items = {}
        self._shared_items = None
        self._loaded = False
        self._dirty = False
        self._changed = set()

    @staticmethod
    def key(prepared):
        public = prepared.get("public") or {}
        payload = {
            "kind": prepared.get("kind"),
            "title": public.get("title") or "",
            "options": sorted(str(option.get("text") or "")
                              for option in public.get("options") or []),
            "blanks": prepared.get("blank_count") or 0,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def lookup(self, prepared):
        if self.path is None:
            return {"correct": None, "wrong": []}
        key = self.key(prepared)
        with self._lock:
            self._load()
            item = self._items.get(key)
            if not isinstance(item, dict):
                item = {}
            wrong = [list(combo) for combo in item.get("wrong") or []]
            for correct in (item.get("correct"), self._shared_correct(key)):
                if correct and not any(_canonical(prepared, combo) == _canonical(prepared, correct)
                                       for combo in wrong):
                    return {"correct": list(correct), "wrong": wrong}
            return {"correct": None, "wrong": wrong}

    def export_shared(self):
        """Merge every locally confirmed answer into the shared file; returns its size."""
        with self._lock:
            self._load()
            items = {key: {"correct": item["correct"]}
                     for key, item in _read_items(self.shared).items() if item["correct"]}
            items.update((key, {"correct": item["correct"]})
                         for key, item in self._items.items() if item.get("correct"))
            self.shared.write_text(json.dumps(
                {"version": 1, "items": dict(sorted(items.items()))},
                ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
            self._shared_items = None
            return len(items)

    def remember(self, prepared, answer_texts, correct):
        if self.path is None:
            return
        texts = [str(text).strip() for text in answer_texts or [] if str(text).strip()]
        if not texts:
            return
        key = self.key(prepared)
        with self._lock:
            self._load()
            item = self._items.setdefault(
                key, {"correct": None, "wrong": [], "updated": None})
            if correct:
                # A fresh confirmation outranks an older rejection of the same answer.
                canonical = _canonical(prepared, texts)
                item["correct"] = texts
                item["wrong"] = [combo for combo in item.get("wrong") or []
                                 if _canonical(prepared, combo) != canonical]
            else:
                canonical = _canonical(prepared, texts)
                wrong = item.get("wrong")
                if not isinstance(wrong, list):
                    wrong = []
                if not any(_canonical(prepared, combo) == canonical for combo in wrong):
                    wrong.append(texts)
                item["wrong"] = wrong[-MAX_WRONG:]
            item["updated"] = datetime.now().isoformat(timespec="seconds")
            self._dirty = True
            self._changed.add(key)

    def remember_correct(self, prepared, answer_texts):
        self.remember(prepared, answer_texts, True)

    def save(self):
        if self.path is None:
            return
        with self._lock:
            self._load()
            if not self._dirty:
                return
            self._write()

    # ---------------------------------------------------------------- 内部

    def _shared_correct(self, key):
        if self.shared is None:
            return None
        if self._shared_items is None:
            try:
                self._shared_items = _read_items(self.shared)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("读取共享答案失败，本次不使用共享答案：%s", exc)
                self._shared_items = {}
        return (self._shared_items.get(key) or {}).get("correct")

    def _load(self):
        if self._loaded or self.path is None:
            return
        self._loaded = True
        try:
            self._items = _read_items(self.path)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("读取本机答案记忆失败，本次不使用记忆：%s", exc)
            self._items = {}

    def _write(self):
        directory = self.path.parent
        temporary = None
        data = {"version": 1, "items": self._items}
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".zhs-answers-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._dirty = False
            self._changed.clear()
        except OSError as exc:
            logger.warning("保存本机答案记忆失败，本次记忆未写入：%s", exc)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


class DatabaseAnswerMemory(AnswerMemory):
    """One memory for every user of a Studia server, in a table of its SQLite database.

    Keep a single instance per process and hand it to every runner: lookups and updates
    go through its lock, and save() writes only the questions that changed."""

    TABLE = "zhs_answers"

    def __init__(self, db_path, shared=None):
        super().__init__(db_path, shared)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS %s ("
                        "key TEXT PRIMARY KEY, correct TEXT, wrong TEXT NOT NULL, updated TEXT)" % self.TABLE)

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        self._items = {}
        for key, correct, wrong, updated in self.db.execute("SELECT key, correct, wrong, updated FROM %s" % self.TABLE):
            self._items[key] = {"correct": json.loads(correct) if correct else None,
                                "wrong": json.loads(wrong)[-MAX_WRONG:], "updated": updated}

    def _write(self):
        rows = [(key, json.dumps(item["correct"], ensure_ascii=False) if item.get("correct") else None,
                 json.dumps(item.get("wrong") or [], ensure_ascii=False), item.get("updated"))
                for key, item in ((key, self._items.get(key)) for key in self._changed) if item]
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.executemany("INSERT OR REPLACE INTO %s VALUES (?,?,?,?)" % self.TABLE, rows)
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            logger.warning("保存共享答案记忆失败，本次记忆未写入：%s", exc)
            return
        self._dirty = False
        self._changed.clear()

    def merge_file(self, path):
        """Fold a per-user zhs-answers.json into the shared memory; returns how many questions it held."""
        items = _read_items(_path(path))
        with self._lock:
            self._load()
            for key, theirs in items.items():
                mine = self._items.setdefault(key, {"correct": None, "wrong": [], "updated": None})
                if theirs["correct"] and not mine.get("correct"):
                    mine["correct"] = theirs["correct"]
                for combo in theirs["wrong"]:
                    if combo not in mine["wrong"] and combo != mine.get("correct"):
                        mine["wrong"].append(combo)
                mine["wrong"] = mine["wrong"][-MAX_WRONG:]
                mine["updated"] = max(filter(None, (mine.get("updated"), theirs.get("updated"))), default=None)
                self._changed.add(key)
            self._dirty = bool(self._changed)
            self.save()
        return len(items)

    def stats(self):
        with self._lock:
            self._load()
            self._shared_correct("")  # loads the bundled file once
            return {"questions": len(self._items),
                    "confirmed": sum(1 for item in self._items.values() if item.get("correct")),
                    "bundled": len(self._shared_items or {})}


def _read_items(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        raise ValueError("记忆文件版本不符")
    items = data.get("items")
    if not isinstance(items, dict):
        raise ValueError("记忆文件格式不符")
    cleaned = {}
    for key, item in items.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            continue
        correct = item.get("correct")
        if not isinstance(correct, list) or not all(isinstance(text, str) for text in correct):
            correct = None
        wrong = item.get("wrong")
        wrong = [[str(text) for text in combo] for combo in wrong if isinstance(combo, list)] \
            if isinstance(wrong, list) else []
        cleaned[key] = {"correct": correct or None, "wrong": wrong[-MAX_WRONG:],
                        "updated": item.get("updated")}
    return cleaned


def _path(path):
    from pathlib import Path
    return Path(path)


def _canonical(prepared, texts):
    values = [str(text).strip() for text in texts or []]
    if prepared.get("kind") == 3:
        return tuple(values)
    return tuple(sorted(set(values)))


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from settings import SettingsStore
    local = Path(SettingsStore().path).parent / "zhs-answers.json"
    count = AnswerMemory(local, shared=SHARED).export_shared()
    print("共享答案已更新：%s（%d 题）" % (SHARED, count))
