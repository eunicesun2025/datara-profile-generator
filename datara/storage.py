import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .domain import Profile, uid


class Conflict(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        for folder in ["profiles", "samples", "tests", "exports", "imports", "references",
                       "optimizer/prompt_states", "optimizer/prompt_versions", "optimizer/test_cases",
                       "optimizer/ground_truth", "optimizer/runs", "optimizer/iterations",
                       "optimizer/extractions", "optimizer/promotions"]:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, folder: str, identity: str, extension=".json") -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity):
            raise ValueError("无效的文件 ID")
        return self.root / folder / (identity + extension)

    def read_json(self, folder: str, identity: str):
        return json.loads(self.path(folder, identity).read_text(encoding="utf-8"))

    def list_json(self, folder: str):
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in (self.root / folder).glob("*.json")]

    def write_json(self, path: Path, data):
        temp = path.with_suffix("." + uid() + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self, identity: str) -> Profile:
        return Profile.model_validate_json(self.path("profiles", identity).read_text(encoding="utf-8"))

    def list_profiles(self):
        result = []
        for path in (self.root / "profiles").glob("*.json"):
            p = Profile.model_validate_json(path.read_text(encoding="utf-8"))
            result.append({"id": p.id, "name": p.name, "revision": p.revision, "updated_at": p.updated_at,
                           "table_count": len(p.tables), "field_count": sum(len(t.fields) for t in p.tables)})
        return sorted(result, key=lambda p: p["updated_at"] or "", reverse=True)

    def save(self, p: Profile) -> Profile:
        with self.lock:
            path = self.path("profiles", p.id)
            current = self.load(p.id) if path.exists() else None
            if (current and p.revision != current.revision) or (not current and p.revision != 0):
                raise Conflict("草稿已有更新，请重新打开后合并修改；当前内容尚未覆盖保存")
            saved = p.model_copy(deep=True)
            saved.revision += 1
            saved.updated_at = datetime.now(timezone.utc).isoformat()
            self.write_json(path, saved.model_dump())
            return saved
