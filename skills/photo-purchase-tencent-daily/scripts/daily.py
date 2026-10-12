#!/usr/bin/env python3
"""Single-day local inventory and durable journal; no OCR or network calls."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
from tencent_adapter import TencentAdapter, number, text

VERSION = "1.1.0"
MAX_SNAPSHOT_AGE = 60
MAX_JSON = 8 * 1024 * 1024
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
ITEM_FIELDS = ("name", "spec", "quantity", "unit", "unit_price", "amount", "note")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_JSON:
        raise ValueError("JSON exceeds 8 MiB")
    def invalid(value):
        raise ValueError("Non-finite JSON number: " + value)
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=invalid)


def atomic_save(path, value):
    content = canonical(value).encode("utf-8")
    if len(content) > MAX_JSON:
        raise ValueError("State exceeds 8 MiB; split input")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def active(state):
    return [op for op in state["operations"] if op["status"] in {"prepared", "uncertain"}]


class ConflictError(ValueError):
    """A scoped online observation differs from the durable write baseline."""


def compare_versions(before, current):
    """Compare actual scoped snapshots, not an unsupported document version token."""
    if before != current:
        raise ConflictError("Current target differs from the saved snapshot; preserve the external edit")


def fresh_evidence(data, earliest=None):
    if not data.get("evidence") or not isinstance(data.get("captured_at"), str):
        raise ValueError("Timestamped current Tencent read evidence required")
    captured = dt.datetime.fromisoformat(data["captured_at"].replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("Evidence time must include its timezone")
    age = (dt.datetime.now(dt.timezone.utc) - captured).total_seconds()
    if age < -5 or age > MAX_SNAPSHOT_AGE or (earliest and captured < dt.datetime.fromisoformat(earliest.replace("Z", "+00:00"))):
        raise ValueError("Evidence is stale or predates the operation; read the target again")


def stale_summary(state):
    # Keep the last actual write as a conflict baseline even when a new source arrives.
    state["summary"] = {**state["summary"], "status": "stale"}


def normalized_daily(project, snapshot):
    rows = snapshot.get("items")
    if snapshot.get("date") != project.date or not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("Fresh full-day snapshot required for the daily summary")
    return [{key: number(row.get(key), key == "amount") if key in {"quantity", "unit_price", "amount"} else text(row.get(key))
             for key in ITEM_FIELDS} for row in rows]


def daily_fingerprint(rows):
    # Sorting preserves duplicates while allowing harmless view-order changes.
    return digest(sorted(canonical(row) for row in rows))


def check_current(project, state, data, current):
    """Check a connector read supplied by the host before recording any write intent."""
    if not isinstance(current, dict):
        raise ValueError("Read the current Tencent target before prepare")
    fresh_evidence(current)
    if current.get("target") != data["target"] or current.get("date") != project.date:
        raise ConflictError("Current read target/date does not match the intended write")
    compare_versions(data["before"], current.get("observed"))
    structure = data.get("before_structure")
    if not isinstance(structure, dict) or not structure:
        raise ValueError("Actual field mapping, date bounds and merged-cell baseline required")
    compare_versions(structure, current.get("structure"))
    previous_structure = state.get("structures", {}).get(digest(data["target"]))
    if previous_structure is not None:
        compare_versions(previous_structure, current["structure"])
    guards = current.get("groups")
    verified = {gid: group for gid, group in state["groups"].items() if group["text_status"] == "verified"}
    if not isinstance(guards, dict) or set(guards) != set(verified):
        raise ValueError("Read every previously verified purchase in this date before writing")
    relocated = {}
    for gid, group in verified.items():
        guard = guards[gid]
        if guard.get("target") != group.get("target") or guard.get("identity_verified") is not True:
            raise ConflictError("Purchase identity or scoped target changed: " + gid)
        compare_versions(group["text_snapshot"], guard.get("text"))
        group_structure = state.get("structures", {}).get(digest(group["target"]))
        if group_structure is not None:
            compare_versions(group_structure, guard.get("structure"))
        if "image_snapshot" in group:
            compare_versions(group["image_snapshot"], guard.get("image"))
        locators = TencentAdapter.locators(project.config["document_type"], guard.get("locators"), len(group["items"]))
        if project.config["document_type"] == "smartsheet":
            compare_versions(group["locators"], locators)
        relocated[gid] = locators
    summary = state["summary"]
    if data["kind"] == "summary" and "data" in summary:
        compare_versions(summary["target"], data["target"])
        compare_versions(summary["data"], current["observed"])
    return relocated


def image_cache_status(state, sha):
    upload = state["uploads"].get(sha)
    associated = any(sha in group.get("image_sha256", group["physical_sha256"] if group["image_status"] == "verified" else [])
                     for group in state["groups"].values())
    usable = bool(upload and dt.datetime.fromisoformat(upload["expires_at"].replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc))
    return {"sha256": sha, "associated": associated, "usable": usable,
            "status": "usable" if usable else "expired" if upload else "missing", **(upload or {})}


class Project:
    def __init__(self, root, date=None):
        self.root = Path(root).resolve(strict=True)
        self.config = read_json(self.root / ".purchase-tencent-daily.json")
        if self.config.get("schema") != "purchase-tencent-daily/v1":
            raise ValueError("Unsupported project configuration")
        timezone = ZoneInfo(self.config.get("timezone", "Asia/Shanghai"))
        self.date = date or dt.datetime.now(timezone).date().isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.date):
            raise ValueError("Use a complete YYYY-MM-DD date")
        self.day = dt.date.fromisoformat(self.date)
        self.month = self.config.get("monthly_documents", {}).get(self.date[:7])
        if not self.month or not self.month.get("file_id") or "REPLACE_" in self.month["file_id"]:
            raise ValueError("Confirmed target missing for this month; no previous-month fallback")
        if self.config.get("document_type") not in {"sheet", "smartsheet"}:
            raise ValueError("document_type must be sheet or smartsheet")
        if self.config.get("photo_scan_recursive", False):
            raise ValueError("Daily scan is nonrecursive")
        folder = self.config["photos_directory"].format(date=self.date, mmdd=self.day.strftime("%m%d"))
        source = Path(folder)
        source = source if source.is_absolute() else self.root / source
        if source.is_symlink():
            raise ValueError("Date directory cannot be a symbolic link")
        self.photos = source.resolve()
        if "{date}" not in self.config["photos_directory"] and "{mmdd}" not in self.config["photos_directory"] and self.photos.name not in {self.date, self.day.strftime("%m%d")}:
            raise ValueError("Photos directory must select exactly this date")
        work = Path(self.config.get("work_directory", ".work/tencent-daily"))
        work = work if work.is_absolute() else self.root / work
        if work.is_symlink():
            raise ValueError("Work directory cannot be a symbolic link")
        work = work.resolve()
        if not work.is_relative_to(self.root) or work == self.root or work == self.photos or work.is_relative_to(self.photos):
            raise ValueError("Use an auxiliary work directory under the selected project, outside photos")
        identity = {"file_id": self.month["file_id"], "type": self.config["document_type"],
                    "daily_sheet_format": self.month.get("daily_sheet_format"),
                    "daily_ranges": self.month.get("daily_ranges", {}),
                    "summary_sheet": self.month.get("summary_sheet"),
                    "daily_fields": self.config.get("daily_fields", {}),
                    "summary_fields": self.config.get("summary_fields", {})}
        self.target_hash = digest(identity)
        self.directory = work / self.target_hash[:24] / self.date
        self.path = self.directory / "state.json"

    def initial(self):
        return {"schema": "purchase-tencent-daily-state/v1", "version": VERSION,
                "date": self.date, "file_id": self.month["file_id"], "target_hash": self.target_hash,
                "photos": {}, "groups": {}, "operations": [], "uploads": {}, "structures": {},
                "summary": {"status": "pending"}, "revision": 0}

    def load(self):
        state = read_json(self.path) if self.path.exists() else self.initial()
        if (state.get("schema"), state.get("date"), state.get("file_id"), state.get("target_hash")) != (
                "purchase-tencent-daily-state/v1", self.date, self.month["file_id"], self.target_hash):
            raise ValueError("State identity mismatch")
        state["version"] = VERSION
        if "data" not in state["summary"]:
            previous = next((op for op in reversed(state["operations"]) if op["kind"] == "summary" and op["status"] == "verified"), None)
            if previous:
                state["summary"].update(data=previous["observed"], target=previous["target"],
                                        at=previous.get("resolved_at", previous["at"]))
        return state

    def input(self, path):
        resolved = Path(path).resolve(strict=True)
        if not (resolved.is_relative_to(self.photos) or resolved.is_relative_to(self.directory)):
            raise ValueError("Input evidence must be inside this date directory or state directory")
        return read_json(resolved)

    def mutate(self, callback):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or (self.directory / ".lock").is_symlink():
            raise ValueError("State files cannot be symbolic links")
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            result = callback(state)
            state["revision"] += 1
            state["updated_at"] = now()
            atomic_save(self.path, state)
            return result


def file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(part)
    return result.hexdigest()


def creation(path):
    try:
        from PIL import Image
        with Image.open(path) as image:
            exif = image.getexif()
            details = exif.get_ifd(34665) if 34665 in exif else exif
            value = details.get(36867) or exif.get(36867)
            if value:
                # EXIF without timezone is a local clock, not a UTC instant.
                stamp = dt.datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").isoformat()
                return {"creation_time": stamp, "time_source": "exif", "timezone_known": False}
    except (ImportError, OSError, ValueError, SyntaxError):
        pass
    stat = path.stat()
    birth = getattr(stat, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = stat.st_ctime
    if birth is not None:
        return {"creation_time": dt.datetime.fromtimestamp(birth, dt.timezone.utc).isoformat(),
                "time_source": "birthtime", "timezone_known": True}
    return {"creation_time": None, "time_source": "unknown", "timezone_known": False}


def scan(project, state, order_file=None):
    if active(state):
        raise ValueError("Resolve the uncertain online operation before changing source state")
    if not project.photos.is_dir():
        raise ValueError("Selected date photo directory does not exist")
    order = {}
    if order_file:
        data = project.input(order_file)
        for entry in data.get("images", data.get("order", [])):
            sha = entry["sha256"]
            stamp = entry.get("creation_timestamp", entry.get("timestamp"))
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                stamp = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()
            parsed = dt.datetime.fromisoformat(stamp)
            if sha in order:
                raise ValueError("Duplicate hash in order manifest")
            order[sha] = {"creation_time": parsed.isoformat(), "time_source": "original_manifest",
                          "timezone_known": parsed.tzinfo is not None}
    previous = state["photos"]
    current, total, files = {}, 0, 0
    for path in project.photos.iterdir():
        if path.suffix.lower() not in SUFFIXES:
            continue
        if path.is_symlink():
            raise ValueError("Photo links are not scanned across dates")
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        files += 1
        if size > 100 * 1024 * 1024 or total > 1024 * 1024 * 1024 or files > 2000:
            raise ValueError("Daily photo limits exceeded (100MiB/file, 1GiB/day, 2000 files)")
        sha = file_hash(path)
        if sha not in current:
            cached = {key: previous[sha][key] for key in ("creation_time", "time_source", "timezone_known")} if sha in previous else None
            metadata = order.get(sha) or cached or creation(path)
            current[sha] = {"files": [], "bytes": size, **metadata}
        current[sha]["files"].append(path.name)
    if set(order) - set(current):
        raise ValueError("Order manifest contains a source outside this day's current photo set")
    if set(current) != set(previous):
        stale_summary(state)
    state["photos"] = current
    for group in state["groups"].values():
        group["source_missing"] = sorted(set(group["receipt_sha256"] + group["physical_sha256"]) - set(current))
    state["last_scan_at"] = now()
    return status(project, state)


def identify(project, state, data):
    if active(state):
        raise ValueError("An online operation is unresolved")
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups or len(groups) > 2000:
        raise ValueError("Supply a bounded nonempty groups list")
    for supplied in groups:
        receipts = sorted(set(supplied.get("receipt_sha256", [])))
        physical = sorted(set(supplied.get("physical_sha256", [])))
        sources = set(receipts + physical)
        if not receipts or set(receipts) & set(physical) or not sources <= set(state["photos"]):
            raise ValueError("Receipt/physical roles must be distinct and reference current scanned hashes")
        if not sources <= set(supplied.get("viewed_sha256", [])):
            raise ValueError("Every source must have been viewed as an original image")
        basis = text(supplied.get("pairing_basis"))
        if physical and not basis:
            raise ValueError("Physical pairing evidence is required")
        group_id = "g_" + digest({"date": project.date, "receipts": receipts})[:24]
        for key, other in state["groups"].items():
            if key != group_id and set(receipts) & set(other["receipt_sha256"]):
                raise ValueError("Receipt source already belongs to another purchase group")
        items, issues, incomplete, warnings = [], [], [], []
        raw_items = supplied.get("items", [])
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 200:
            raise ValueError("Group must contain 1..200 observed items")
        settlement_note = text(supplied.get("settlement_note"))
        for index, item in enumerate(raw_items):
            normalized = {key: number(item.get(key), key == "amount") if key in {"quantity", "unit_price", "amount"} else text(item.get(key)) for key in ITEM_FIELDS}
            if not normalized["name"] or normalized["amount"] is None:
                issues.append("item_%d_missing_name_or_amount" % (index + 1))
            for field in ("quantity", "unit", "unit_price"):
                if normalized[field] in (None, ""):
                    incomplete.append("item_%d_missing_%s" % (index + 1, field))
            q, price, amount = (normalized[key] for key in ("quantity", "unit_price", "amount"))
            if all(value is not None for value in (q, price, amount)):
                if (Decimal(q) * Decimal(price)).quantize(Decimal("0.01")) != Decimal(amount) and not (normalized["note"] or settlement_note):
                    issues.append("item_%d_unexplained_settlement" % (index + 1))
            items.append(normalized)
        total = number(supplied.get("receipt_total"), True)
        item_total = sum((Decimal(item["amount"]) for item in items if item["amount"] is not None), Decimal(0))
        if total is None:
            issues.append("receipt_total_missing")
        elif item_total != Decimal(total):
            difference = "receipt_item_total_difference:" + format(Decimal(total) - item_total, "f")
            (warnings if settlement_note else issues).append(difference)
        old = state["groups"].get(group_id)
        business = {"receipt_sha256": receipts, "items": items, "receipt_total": total,
                    "settlement_note": settlement_note}
        if old and old["text_status"] == "verified" and any(old[key] != value for key, value in business.items()):
            raise ValueError("Written purchase changed; explicitly review online correction instead of replacing its state")
        changed = not old or any(old.get(key) != value for key, value in {**business, "physical_sha256": physical}.items())
        group = {**(old or {}), **business, "physical_sha256": physical,
                 "viewed_sha256": sorted(sources), "pairing_basis": basis,
                 "issues": issues, "incomplete_fields": incomplete, "warnings": warnings,
                 "source_missing": [], "item_total": format(item_total, ".2f"),
                 "text_status": old["text_status"] if old else "pending",
                 "image_status": old["image_status"] if old and old["physical_sha256"] == physical else "pending"}
        state["groups"][group_id] = group
        if changed:
            stale_summary(state)
    return status(project, state)


def prepare(project, state, data, read_current=None, *, _adopting=False):
    if active(state):
        raise ValueError("Uncertain operation exists; read back before another submission")
    kind = data.get("kind")
    if kind not in {"text", "image", "summary", "structure"} or data.get("scope_checked") is not True:
        raise ValueError("Verified scope and supported operation kind required")
    target = data.get("target", {})
    if target.get("file_id") != state["file_id"] or not target.get("sheet_id"):
        raise ValueError("Operation target does not match the selected monthly document")
    before, expected = data.get("before"), data.get("expected")
    if not isinstance(before, dict) or not isinstance(expected, dict) or not expected or before == expected:
        raise ValueError("Distinct normalized before/expected snapshots required")
    if data.get("observation_only"):
        raise ValueError("Use adopt for observations; prepare always checks the online write baseline")
    current = None
    relocated = {}
    if not _adopting:
        # A host can read through its connector here. The CLI consumes the same
        # envelope from the immediately preceding MCP read; it has no credentials.
        current = read_current(data) if read_current else data.get("current")
        relocated = check_current(project, state, data, current)
    group_id = data.get("group_id")
    if kind in {"text", "image"}:
        group = state["groups"].get(group_id)
        if not group or group["source_missing"]:
            raise ValueError("Current purchase group sources are required")
        if kind == "text" and (group["issues"] or expected != {"items": group["items"]}):
            raise ValueError("Text expectation must match the observed group without unresolved amounts")
        if kind == "image" and (group["text_status"] != "verified" or not group["physical_sha256"]):
            raise ValueError("Verify text and physical sources before image association")
        if kind == "image":
            if target != group.get("target"):
                raise ValueError("Photo association must use the verified text target")
            if project.config["document_type"] == "smartsheet":
                ids = expected.get("image_ids")
                if set(expected) != {"image_ids"} or not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
                    raise ValueError("SmartSheet photo expectation requires actual image IDs")
                if not _adopting:
                    old_ids = before.get("image_ids")
                    if set(before) != {"image_ids"} or not isinstance(old_ids, list) or any(not isinstance(value, str) or not value for value in old_ids):
                        raise ValueError("Read existing photo IDs before appending")
                    associated = set(group.get("image_sha256", group["physical_sha256"] if group["image_status"] == "verified" else []))
                    needed = [sha for sha in group["physical_sha256"] if sha not in associated]
                    if any(not image_cache_status(state, sha)["usable"] for sha in needed):
                        raise ValueError("Missing or expired upload; refresh before associating")
                    wanted = set(old_ids) | {state["uploads"][sha]["image_id"] for sha in needed}
                    if set(ids) != wanted:
                        raise ValueError("Preserve existing photo IDs and append only the cached physical sources")
            elif expected != {"visual_sha256": group["physical_sha256"]}:
                raise ValueError("SHEET image verification must identify this group's physical originals")
            elif not _adopting:
                associated = set(group.get("image_sha256", group["physical_sha256"] if group["image_status"] == "verified" else []))
                needed = set(group["physical_sha256"]) - associated
                upload_ids, originals = data.get("upload_ids"), data.get("original_sha256")
                if (upload_ids is None) == (originals is None):
                    raise ValueError("SHEET insertion requires cached upload IDs or explicit original-image hashes")
                if upload_ids is not None:
                    if not isinstance(upload_ids, dict) or set(upload_ids) != needed or any(
                            not image_cache_status(state, sha)["usable"] or upload_ids[sha] != state["uploads"][sha]["image_id"] for sha in needed):
                        raise ValueError("SHEET image IDs must match unexpired source uploads")
                elif not isinstance(originals, list) or set(originals) != needed or len(originals) != len(needed):
                    raise ValueError("SHEET insertion must use the actual new physical originals")
    summary_basis = None
    if kind == "summary":
        if project.config["document_type"] == "smartsheet":
            if not isinstance(target.get("record_id"), str) or not target["record_id"]:
                raise ValueError("Summary requires the actual date-specific record ID")
        elif not isinstance(target.get("row_index"), int) or isinstance(target["row_index"], bool) or target["row_index"] < 0:
            raise ValueError("Summary requires the actual date-specific zero-based row")
        if not _adopting and (set(before) != {"date", "item_count", "daily_amount", "status"} or before["date"] not in {project.date, "", None}):
            raise ValueError("Summary baseline must be this date's managed fields or a confirmed blank slot")
        if set(expected) != {"date", "item_count", "daily_amount", "status"} or expected["date"] != project.date:
            raise ValueError("Summary must contain only this date's four managed fields")
        if isinstance(expected["item_count"], bool) or not isinstance(expected["item_count"], int) or expected["item_count"] < 0:
            raise ValueError("Invalid daily item count")
        if expected["status"] not in {"完成", "部分完成"} or number(expected["daily_amount"], True) is None:
            raise ValueError("Invalid daily amount or completion status")
        if expected["status"] == "完成" and not groups_ready(state):
            raise ValueError("Unresolved sources, amounts or images cannot produce a completed summary")
        daily = data.get("daily_snapshot", {})
        if not _adopting:
            fresh_evidence(daily)
        normalized = normalized_daily(project, daily)
        present = Counter(canonical(row) for row in normalized)
        owned = Counter(canonical(row) for group in state["groups"].values() if group["text_status"] == "verified" for row in group["items"])
        if owned - present:
            raise ValueError("Daily snapshot omitted or changed a verified purchase")
        total = sum((Decimal(row["amount"]) for row in normalized if row["amount"] is not None), Decimal(0))
        if expected["item_count"] != len(normalized) or expected["daily_amount"] != format(total, ".2f"):
            raise ValueError("Summary must include all current daily purchases, including collaborators")
        if expected["status"] == "完成" and any(row["amount"] is None for row in normalized):
            raise ValueError("Missing daily amounts cannot produce a completed summary")
        summary_basis = daily_fingerprint(normalized)
    attempt_key = digest({"kind": kind, "group_id": group_id, "target": target, "expected": expected})
    attempts = [op for op in state["operations"] if op["attempt_key"] == attempt_key]
    if any(op["status"] == "verified" for op in attempts):
        raise ValueError("This operation already verified; do not submit it again")
    if len(attempts) >= 2:
        raise ValueError("Retry limit reached; review the failed operation")
    for gid, locators in relocated.items():
        state["groups"][gid]["locators"] = locators
    operation_id = "op_" + digest({"key": attempt_key, "revision": state["revision"]})[:24]
    checked = ({key: current[key] for key in ("captured_at", "evidence", "target", "date")} |
               {key + "_sha256": digest(current[key]) for key in ("observed", "structure", "groups")}) if current else None
    state["operations"].append({"operation_id": operation_id, "attempt_key": attempt_key,
                                "kind": kind, "group_id": group_id, "target": target,
                                "before": before, "expected": expected, "status": "prepared", "at": now(),
                                "observation_only": _adopting, "summary_basis": summary_basis,
                                "before_structure": data.get("before_structure"), "current_evidence": checked,
                                "image_sha256": state["groups"][group_id]["physical_sha256"][:] if kind == "image" else None})
    return {"operation_id": operation_id, "status": "prepared", "state": str(project.path)}


def resolve(project, state, data):
    operation = next((op for op in state["operations"] if op["operation_id"] == data.get("operation_id")), None)
    if not operation or operation not in active(state) or data.get("target") != operation["target"]:
        raise ValueError("Select the pending operation and its exact target")
    if not data.get("evidence") or not isinstance(data.get("observed"), dict):
        raise ValueError("Fresh readback evidence is required")
    writing = not operation.get("observation_only")
    if writing:
        fresh_evidence(data, earliest=operation["at"])
    observed = data["observed"]
    result = "uncertain"
    structure_matches = True
    if writing and observed == operation["expected"]:
        if not isinstance(data.get("structure"), dict) or not data["structure"]:
            raise ValueError("Read back the affected field mapping and merged-cell structure")
        wanted_structure = operation["expected"] if operation["kind"] == "structure" else operation.get("before_structure")
        if wanted_structure is None:
            if data.get("legacy_readback_reviewed") is not True:
                raise ValueError("Legacy operation lacks a structure baseline; explicitly review its current target")
        else:
            structure_matches = data["structure"] == wanted_structure
    summary_matches = True
    if operation["kind"] == "summary" and observed == operation["expected"] and not operation.get("observation_only"):
        daily = data.get("daily_snapshot", {})
        fresh_evidence(daily, earliest=operation["at"])
        rows = normalized_daily(project, daily)
        if operation.get("summary_basis") is None:
            if data.get("legacy_readback_reviewed") is not True:
                raise ValueError("Legacy summary lacks its calculation fingerprint; review the full current date")
            present = Counter(canonical(row) for row in rows)
            owned = Counter(canonical(row) for group in state["groups"].values() if group["text_status"] == "verified" for row in group["items"])
            total = sum((Decimal(row["amount"]) for row in rows if row["amount"] is not None), Decimal(0))
            summary_matches = not (owned - present) and len(rows) == observed["item_count"] and format(total, ".2f") == observed["daily_amount"]
            if observed["status"] == "完成" and any(row["amount"] is None for row in rows):
                summary_matches = False
        else:
            summary_matches = daily_fingerprint(rows) == operation["summary_basis"]
    if observed == operation["expected"]:
        result = "verified" if summary_matches and structure_matches else "uncertain"
        if operation["kind"] in {"text", "image"} and not data.get("locators"):
            raise ValueError("Verified group requires actual record IDs or row locators")
        if operation["kind"] in {"text", "image"}:
            group = state["groups"][operation["group_id"]]
            locators = TencentAdapter.locators(project.config["document_type"], data["locators"], len(group["items"]))
            if operation["kind"] == "image" and locators != group.get("locators"):
                raise ValueError("Photo locators do not match the verified text mapping")
    elif observed == operation["before"] and data.get("absence_verified") is True:
        result = "not_applied"
    operation.update(status=result, observed=observed, evidence=data["evidence"], resolved_at=now())
    if data.get("legacy_readback_reviewed") is True:
        operation["legacy_readback_reviewed"] = True
    operation.pop("conflict_reason", None)
    if not summary_matches:
        operation["conflict_reason"] = "Daily purchases changed while updating the summary"
    elif not structure_matches:
        operation["conflict_reason"] = "Target structure changed during the write"
    if result == "verified":
        kind = operation["kind"]
        if writing:
            state.setdefault("structures", {})[digest(operation["target"])] = data["structure"]
        if kind in {"text", "image"}:
            group = state["groups"][operation["group_id"]]
            group[kind + "_status"] = "verified"
            group[kind + "_snapshot"] = observed
            group["locators"] = data["locators"]
            group["target"] = operation["target"]
            if kind == "image":
                group["image_sha256"] = operation.get("image_sha256") or group["physical_sha256"][:]
            stale_summary(state)
        elif kind == "summary":
            state["summary"] = {"status": "verified", "data": observed, "target": operation["target"], "at": now()}
    return {"operation_id": operation["operation_id"], "status": result,
            **({"error": operation["conflict_reason"]} if result == "uncertain" and "conflict_reason" in operation else {})}


def adopt(project, state, data):
    if data.get("observed") != data.get("expected") or not data.get("evidence"):
        raise ValueError("Adoption requires current matching evidence; it never writes online")
    # Journal an observation and resolve in the same atomic local transaction.
    payload = {**data, "before": {"observation_only": True}}
    prepared = prepare(project, state, payload, _adopting=True)
    return resolve(project, state, {**data, "operation_id": prepared["operation_id"]})


def cache_image(project, state, data):
    sha = data.get("sha256")
    physical = {sha for group in state["groups"].values() for sha in group["physical_sha256"]}
    if sha not in state["photos"] or sha not in physical or not isinstance(data.get("image_id"), str) or not data["image_id"]:
        raise ValueError("Upload must refer to an observed physical photo")
    uploaded, expiry = (dt.datetime.fromisoformat(data[key].replace("Z", "+00:00")) for key in ("uploaded_at", "expires_at"))
    if (uploaded.tzinfo is None or expiry.tzinfo is None or expiry <= uploaded or
            expiry > uploaded + dt.timedelta(days=1) or uploaded > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)):
        raise ValueError("Timezone-aware valid upload expiry required")
    state["uploads"][sha] = {key: data[key] for key in ("image_id", "uploaded_at", "expires_at")}
    return image_cache_status(state, sha)


def pending_photos(state):
    assigned = {sha for group in state["groups"].values() for sha in group["viewed_sha256"]}
    return set(state["photos"]) - assigned


def groups_ready(state):
    return bool(state["groups"]) and not pending_photos(state) and all(
        not group["issues"] and not group.get("incomplete_fields") and not group["source_missing"] and
        group["text_status"] == "verified" and group["image_status"] == "verified"
        for group in state["groups"].values())


def status(project, state):
    groups = [{"group_id": key, "text": group["text_status"], "image": group["image_status"],
               "issues": group["issues"][:5], "incomplete_fields": group.get("incomplete_fields", [])[:5],
               "warnings": group.get("warnings", [])[:5], "source_missing": group["source_missing"][:5]}
              for key, group in state["groups"].items()]
    complete = groups_ready(state) and not active(state) and state["summary"].get("status") == "verified" and state["summary"].get("data", {}).get("status") == "完成"
    return {"date": project.date, "status": "completed" if complete else "partial",
            "state": str(project.path), "scanned": len(state["photos"]),
            "unreviewed": [{"sha256": sha, "files": state["photos"][sha]["files"][:3]} for sha in sorted(pending_photos(state))[:10]],
            "unreviewed_total": len(pending_photos(state)), "groups": groups[:10], "groups_total": len(groups),
            "unknown_time": sum(photo["time_source"] == "unknown" for photo in state["photos"].values()),
            "pending_operations": [{"operation_id": op["operation_id"], "kind": op["kind"], "status": op["status"]} for op in active(state)],
            "summary": state["summary"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "identify", "prepare", "resolve", "adopt", "cache-image", "image-cache", "adapt", "status"])
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--date", help="ISO date selected by the user; otherwise today in project timezone")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--order-file", type=Path)
    args = parser.parse_args()
    project = Project(args.project, args.date)
    if args.command == "status":
        result = status(project, project.load())
    elif args.command == "image-cache":
        state = project.load()
        physical = {sha for group in state["groups"].values() for sha in group["physical_sha256"]}
        selected = sorted(physical)[:20]
        if args.input:
            selected = project.input(args.input).get("sha256")
            if not isinstance(selected, list) or len(selected) > 20 or any(not isinstance(sha, str) or sha not in physical for sha in selected):
                raise ValueError("Select at most 20 current physical hashes")
        result = {"date": project.date, "images": [image_cache_status(state, sha) for sha in selected], "total": len(physical)}
    elif args.command == "scan":
        result = project.mutate(lambda state: scan(project, state, args.order_file))
    else:
        if not args.input:
            parser.error("--input is required for this command")
        data = project.input(args.input)
        if args.command == "adapt":
            sheet_type = project.config["document_type"]
            if data.get("kind") == "merge":
                result = {"payload": TencentAdapter.merge_range(sheet_type, data["rows"], data["column"],
                          data["allowed_range"], data["current_merges"], data["cells_blank"])}
            else:
                result = {"payload": TencentAdapter.write_cell(sheet_type, data.get("value"), data["field_type"], project.config.get("timezone", "Asia/Shanghai"))}
        else:
            command = {"identify": identify, "prepare": prepare, "resolve": resolve, "adopt": adopt, "cache-image": cache_image}[args.command]
            result = project.mutate(lambda state: command(project, state, data))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except ConflictError as error:
        print(json.dumps({"status": "conflict", "error": str(error)}, ensure_ascii=False))
        sys.exit(2)
    except (ValueError, OSError, KeyError, TypeError, InvalidOperation) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        sys.exit(2)
