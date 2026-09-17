import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .domain import Profile, uid


class Conflict(Exception):
    pass


#: Folders holding records that belong to exactly one Profile. All of them are removed
#: together with the Profile itself. Uploaded samples, reference files, and Excel imports
#: are shared workspace objects, so they are deliberately never deleted here.
PROFILE_OWNED_FOLDERS = ("profiles", "tests", "exports", "optimizer/prompt_states",
                         "optimizer/prompt_versions", "optimizer/test_cases",
                         "optimizer/ground_truth", "optimizer/runs", "optimizer/iterations",
                         "optimizer/extractions", "optimizer/promotions")


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

    def delete_profile(self, identity: str) -> dict[str, int]:
        """Remove one Profile plus every stored record that belongs only to it.

        Derived artifacts are deleted with the Profile: prompt versions and prompt state,
        optimizer test cases, ground truth, runs, iterations, extractions, promotions,
        model job records, and export metadata/ZIP archives. The return value maps each
        folder in ``PROFILE_OWNED_FOLDERS`` to the number of files removed so the caller can
        summarize the cleanup. Sample, reference, and import uploads are shared workspace
        objects and are kept. Raises ``ValueError`` for an unsafe identifier and
        ``FileNotFoundError`` when the Profile does not exist.
        """
        profile_path = self.path("profiles", identity)  # also rejects unsafe identifiers
        if not profile_path.exists():
            raise FileNotFoundError(identity)
        removed = dict.fromkeys(PROFILE_OWNED_FOLDERS, 0)
        with self.lock:
            # Collect the identifiers that link child records to this Profile before removing
            # anything, so a half-deleted run can no longer be matched by its parent.
            run_ids = {r["id"] for r in self.list_json("optimizer/runs") if r.get("profile_id") == identity}
            case_ids = {c["id"] for c in self.list_json("optimizer/test_cases") if c.get("profile_id") == identity}
            version_ids = {v["id"] for v in self.list_json("optimizer/prompt_versions")
                           if v.get("profile_id") == identity}
            iteration_ids = {i["id"] for i in self.list_json("optimizer/iterations")
                             if i.get("run_id") in run_ids}
            owners = {
                "tests": lambda r: r.get("profile_id") == identity,
                "exports": lambda r: (r.get("profile") or {}).get("id") == identity,
                "optimizer/prompt_versions": lambda r: r.get("profile_id") == identity,
                "optimizer/test_cases": lambda r: r.get("profile_id") == identity,
                "optimizer/ground_truth": lambda r: r.get("test_case_id") in case_ids,
                "optimizer/runs": lambda r: r.get("profile_id") == identity,
                "optimizer/iterations": lambda r: r.get("run_id") in run_ids,
                "optimizer/extractions": lambda r: (r.get("run_id") in run_ids
                                                    or r.get("iteration_id") in iteration_ids
                                                    or r.get("test_case_id") in case_ids),
                # Not written by the current release; matched defensively so future
                # promotion records cannot survive the Profile they were created for.
                "optimizer/promotions": lambda r: (r.get("profile_id") == identity
                                                   or r.get("run_id") in run_ids
                                                   or r.get("prompt_version_id") in version_ids),
            }
            state_path = self.path("optimizer/prompt_states", identity)
            if state_path.exists():
                state_path.unlink()
                removed["optimizer/prompt_states"] = 1
            for folder, owned_by_profile in owners.items():
                for path in sorted((self.root / folder).glob("*.json")):
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue  # one unreadable record must never block the deletion
                    if not isinstance(record, dict) or not owned_by_profile(record):
                        continue
                    path.unlink(missing_ok=True)
                    if folder == "exports":
                        path.with_suffix(".zip").unlink(missing_ok=True)
                    removed[folder] += 1
            profile_path.unlink(missing_ok=True)
            removed["profiles"] = 1
        return removed
