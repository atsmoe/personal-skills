"""Offline regression checks. All business artifacts stay in temporary projects."""
import copy
import datetime as dt
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from decimal import Decimal

import daily


def png(rgb):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0" + bytes(rgb))) + chunk(b"IEND", b"")


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.date = "2026-10-12"
        self.folder = self.root / "photos" / self.date
        self.folder.mkdir(parents=True)
        self.config = {"schema": "purchase-tencent-daily/v1", "timezone": "Asia/Shanghai",
                       "document_type": "smartsheet", "photos_directory": "photos/{date}",
                       "work_directory": ".work/daily", "monthly_documents": {
                           "2026-10": {"file_id": "fixture-file", "daily_sheet_format": "MMDD", "summary_sheet": "汇总"}}}
        self.write_config()
        for name, rgb in [("receipt.png", (255, 0, 0)), ("physical.png", (0, 255, 0))]:
            (self.folder / name).write_bytes(png(rgb))
        self.receipt = daily.file_hash(self.folder / "receipt.png")
        self.physical = daily.file_hash(self.folder / "physical.png")
        self.project = daily.Project(self.root, self.date)
        self.project.mutate(lambda state: daily.scan(self.project, state))
        self.raw = {"receipt_sha256": [self.receipt], "physical_sha256": [self.physical],
                    "viewed_sha256": [self.receipt, self.physical], "pairing_basis": "收据和实物对应",
                    "receipt_total": "0.30", "settlement_note": "", "items": [
                        {"name": "A", "spec": "", "quantity": "1", "unit": "个", "unit_price": "0.1", "amount": "0.10", "note": ""},
                        {"name": "B", "spec": "", "quantity": "1", "unit": "个", "unit_price": "0.2", "amount": "0.20", "note": ""}]}
        self.project.mutate(lambda state: daily.identify(self.project, state, {"groups": [self.raw]}))
        self.gid = next(iter(self.project.load()["groups"]))
        self.target = {"file_id": "fixture-file", "sheet_id": "day-1012"}

    def write_config(self):
        (self.root / ".purchase-tencent-daily.json").write_text(json.dumps(self.config), encoding="utf-8")

    def text_payload(self):
        return self.write_context({"kind": "text", "group_id": self.gid, "target": self.target,
                "before": {"items": []}, "expected": {"items": self.project.load()["groups"][self.gid]["items"]}, "scope_checked": True})

    def write_context(self, data, project=None):
        project = project or self.project
        state = project.load()
        structure = {"fields": {"amount": "currency"}, "date": self.date, "merged_cells": [], "daily_bounds": [0, 20]}
        guards = {}
        for gid, group in state["groups"].items():
            if group["text_status"] == "verified":
                guards[gid] = {"target": group["target"], "identity_verified": True,
                               "locators": group["locators"], "text": group["text_snapshot"]}
                if "image_snapshot" in group:
                    guards[gid]["image"] = group["image_snapshot"]
                if daily.digest(group["target"]) in state.get("structures", {}):
                    guards[gid]["structure"] = state["structures"][daily.digest(group["target"])]
        data["before_structure"] = structure
        data["current"] = {"date": self.date, "target": data["target"], "captured_at": daily.now(),
                           "evidence": "connector-current", "observed": copy.deepcopy(data["before"]),
                           "structure": copy.deepcopy(structure), "groups": copy.deepcopy(guards)}
        return data

    def readback(self, payload, **changes):
        data = {"target": payload["target"], "observed": payload["expected"], "evidence": "connector-readback",
                "captured_at": daily.now(), "structure": copy.deepcopy(payload["before_structure"])}
        if "daily_snapshot" in payload:
            data["daily_snapshot"] = {**copy.deepcopy(payload["daily_snapshot"]), "captured_at": daily.now()}
        data.update(changes)
        return data

    def adopt_text(self):
        data = self.text_payload()
        data.update(observed=data["expected"], locators=["r1", "r2"], evidence="fixture-readback")
        return self.project.mutate(lambda state: daily.adopt(self.project, state, data))

    def adopt_image(self):
        data = {"kind": "image", "group_id": self.gid, "target": self.target,
                "expected": {"image_ids": ["already-embedded-id"]}, "observed": {"image_ids": ["already-embedded-id"]},
                "scope_checked": True, "locators": ["r1", "r2"], "evidence": "fixture-picture-readback"}
        return self.project.mutate(lambda state: daily.adopt(self.project, state, data))

    def summary_payload(self, extra=False):
        rows = copy.deepcopy(self.project.load()["groups"][self.gid]["items"])
        if extra:
            rows.append({"name": "协作者采购", "spec": "", "quantity": "1", "unit": "个", "unit_price": "0.5", "amount": "0.50", "note": ""})
        return self.write_context({"kind": "summary", "target": {"file_id": "fixture-file", "sheet_id": "summary", "record_id": "summary-1012"},
                "scope_checked": True, "before": {"date": self.date, "item_count": 0, "daily_amount": "0.00", "status": "部分完成"},
                "expected": {"date": self.date, "item_count": len(rows), "daily_amount": "0.80" if extra else "0.30", "status": "完成"},
                "daily_snapshot": {"date": self.date, "items": rows, "captured_at": daily.now(), "evidence": "all-current-day-rows"}})

    def test_decimal_and_duplicate_files(self):
        (self.folder / "copy.png").write_bytes((self.folder / "receipt.png").read_bytes())
        out = self.project.mutate(lambda state: daily.scan(self.project, state))
        self.assertEqual(out["scanned"], 2)
        self.assertEqual(out["unreviewed_total"], 0)
        state = self.project.load()
        self.assertEqual(state["groups"][self.gid]["item_total"], "0.30")
        self.assertEqual(state["groups"][self.gid]["issues"], [])
        self.assertEqual(len(state["photos"][self.receipt]["files"]), 2)

    def test_date_scope_and_month_boundary(self):
        previous = self.root / "photos" / "2026-10-11"
        previous.mkdir()
        (previous / "bad.png").write_bytes(b"not-an-image")
        evidence = previous / "groups.json"
        evidence.write_text("{}")
        with self.assertRaises(ValueError):
            self.project.input(evidence)
        with self.assertRaises(ValueError):
            daily.Project(self.root, "2026-11-01")
        (self.folder / "linked.png").symlink_to(previous / "bad.png")
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.scan(self.project, state))
        self.assertEqual(len(self.project.load()["photos"]), 2)

    def test_same_day_other_document_has_separate_state(self):
        self.config["monthly_documents"]["2026-10"]["file_id"] = "another-file"
        self.write_config()
        other = daily.Project(self.root, self.date)
        self.assertNotEqual(other.path, self.project.path)
        self.assertEqual(other.load()["groups"], {})

    def test_crash_and_uncertain_recovery_never_readds(self):
        payload = self.text_payload()
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        resumed = daily.Project(self.root, self.date)
        with self.assertRaises(ValueError):
            resumed.mutate(lambda state: daily.prepare(resumed, state, payload))
        evidence = self.readback(payload, operation_id=out["operation_id"], observed={"items": []})
        result = resumed.mutate(lambda state: daily.resolve(resumed, state, evidence))
        self.assertEqual(result["status"], "uncertain")
        with self.assertRaises(ValueError):
            resumed.mutate(lambda state: daily.prepare(resumed, state, payload))
        evidence.update(observed=payload["expected"], locators=["r1", "r2"])
        result = resumed.mutate(lambda state: daily.resolve(resumed, state, evidence))
        self.assertEqual(result["status"], "verified")
        with self.assertRaises(ValueError):
            resumed.mutate(lambda state: daily.prepare(resumed, state, payload))

    def test_retry_requires_verified_absence_and_is_bounded(self):
        payload = self.text_payload()
        for _ in range(2):
            out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
            readback = self.readback(payload, operation_id=out["operation_id"], observed=payload["before"], absence_verified=True)
            result = self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
            self.assertEqual(result["status"], "not_applied")
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload))

    def test_missing_fields_can_be_written_but_cannot_complete(self):
        data = copy.deepcopy(self.raw)
        data["items"][0]["quantity"] = None
        self.project.mutate(lambda state: daily.identify(self.project, state, {"groups": [data]}))
        self.adopt_text()
        self.adopt_image()
        self.assertFalse(daily.groups_ready(self.project.load()))
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, self.summary_payload()))

    def test_written_purchase_cannot_be_replaced_and_import_is_atomic(self):
        self.adopt_text()
        revision = self.project.load()["revision"]
        data = copy.deepcopy(self.raw)
        data["items"][0]["amount"] = "0.11"
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.identify(self.project, state, {"groups": [data]}))
        self.assertEqual(self.project.load()["revision"], revision)
        self.assertEqual(self.project.load()["groups"][self.gid]["items"][0]["amount"], "0.10")

    def test_photo_expiry_then_image_only_recovery(self):
        self.adopt_text()
        instant = dt.datetime.now(dt.timezone.utc)
        upload = {"sha256": self.physical, "image_id": "expired-id", "uploaded_at": (instant - dt.timedelta(days=2)).isoformat(),
                  "expires_at": (instant - dt.timedelta(days=1)).isoformat()}
        self.project.mutate(lambda state: daily.cache_image(self.project, state, upload))
        payload = self.write_context({"kind": "image", "group_id": self.gid, "target": self.target, "before": {"image_ids": []},
                   "expected": {"image_ids": ["expired-id"]}, "scope_checked": True})
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        upload.update(image_id="fresh-id", uploaded_at=instant.isoformat(), expires_at=(instant + dt.timedelta(days=1)).isoformat())
        self.project.mutate(lambda state: daily.cache_image(self.project, state, upload))
        payload["expected"]["image_ids"] = ["fresh-id"]
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        readback = self.readback(payload, operation_id=out["operation_id"], locators=["r1", "r2"])
        self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
        state = self.project.load()
        self.assertEqual(state["groups"][self.gid]["text_status"], "verified")
        self.assertEqual(state["groups"][self.gid]["image_status"], "verified")
        self.assertEqual(sum(op["kind"] == "text" for op in state["operations"]), 1)

    def test_summary_includes_collaborators_and_new_source_invalidates_completion(self):
        self.adopt_text()
        self.adopt_image()
        payload = self.summary_payload(extra=True)
        wrong = copy.deepcopy(payload)
        wrong["expected"].update(item_count=2, daily_amount="0.30")
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, wrong))
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        evidence = self.readback(payload, operation_id=out["operation_id"])
        self.project.mutate(lambda state: daily.resolve(self.project, state, evidence))
        self.assertEqual(daily.status(self.project, self.project.load())["status"], "completed")
        (self.folder / "new.png").write_bytes(png((0, 0, 255)))
        output = self.project.mutate(lambda state: daily.scan(self.project, state))
        self.assertEqual(output["status"], "partial")
        self.assertEqual(output["unreviewed_total"], 1)
        self.assertEqual(output["summary"]["status"], "stale")

    def test_group_id_stable_when_physical_evidence_added(self):
        data = copy.deepcopy(self.raw)
        data["physical_sha256"] = []
        self.project.mutate(lambda state: daily.identify(self.project, state, {"groups": [data]}))
        self.adopt_text()
        self.project.mutate(lambda state: daily.identify(self.project, state, {"groups": [self.raw]}))
        self.assertEqual(list(self.project.load()["groups"]), [self.gid])
        self.assertEqual(self.project.load()["groups"][self.gid]["text_status"], "verified")

    def test_source_disappearance_blocks_writes(self):
        (self.folder / "receipt.png").unlink()
        self.project.mutate(lambda state: daily.scan(self.project, state))
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, self.text_payload()))

    def test_two_cli_writers_only_one_prepares(self):
        source = self.folder / "operation.json"
        source.write_text(json.dumps(self.text_payload()), encoding="utf-8")
        args = [sys.executable, str(Path(daily.__file__)), "prepare", "--project", str(self.root), "--date", self.date, "--input", str(source)]
        processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 2], outputs)
        self.assertEqual(len(daily.active(self.project.load())), 1)

    def test_sheet_picture_evidence_uses_real_row_locators(self):
        self.config["document_type"] = "sheet"
        self.write_config()
        project = daily.Project(self.root, self.date)
        project.mutate(lambda state: daily.scan(project, state))
        project.mutate(lambda state: daily.identify(project, state, {"groups": [self.raw]}))
        data = {"kind": "text", "group_id": self.gid, "target": self.target, "scope_checked": True,
                "expected": {"items": project.load()["groups"][self.gid]["items"]}, "locators": [2, 3], "evidence": "cells"}
        data["observed"] = data["expected"]
        project.mutate(lambda state: daily.adopt(project, state, data))
        data.update(kind="image", expected={"visual_sha256": [self.physical]}, observed={"visual_sha256": [self.physical]}, evidence="current-ui-picture")
        project.mutate(lambda state: daily.adopt(project, state, data))
        self.assertTrue(daily.groups_ready(project.load()))

    def test_changed_current_target_is_blocked_before_journaling(self):
        payload = self.text_payload()
        revision = self.project.load()["revision"]
        actual = copy.deepcopy(payload["current"])
        actual["observed"] = {"items": [{"name": "协作者刚写入"}]}
        calls = []
        def read_current(operation):
            calls.append(operation["target"])
            return actual
        with self.assertRaises(daily.ConflictError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload, read_current))
        self.assertEqual(calls, [self.target])
        self.assertEqual(self.project.load()["operations"], [])
        self.assertEqual(self.project.load()["revision"], revision)

    def test_missing_stale_wrong_target_and_structure_reads_are_rejected(self):
        for defect in ("missing", "stale", "wrong_target", "changed_structure", "bypass"):
            payload = self.text_payload()
            if defect == "missing":
                payload.pop("current")
            elif defect == "stale":
                payload["current"]["captured_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat()
            elif defect == "wrong_target":
                payload["current"]["target"] = {"file_id": "another", "sheet_id": "other-day"}
            elif defect == "changed_structure":
                payload["current"]["structure"]["fields"]["amount"] = "formula"
            else:
                payload["observation_only"] = True
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.assertEqual(self.project.load()["operations"], [])

    def test_saved_purchase_detects_edit_even_with_a_new_before_snapshot(self):
        self.adopt_text()
        payload = self.write_context({"kind": "structure", "target": self.target, "scope_checked": True,
                                     "before": {"merged": False}, "expected": {"merged": True}})
        payload["current"]["groups"][self.gid]["text"]["items"][0]["amount"] = "9.00"
        with self.assertRaises(daily.ConflictError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.assertEqual(len(self.project.load()["operations"]), 1)  # Only the read-only adoption.

    def test_smart_record_identity_cannot_be_replaced_by_an_identical_record(self):
        self.adopt_text()
        payload = self.write_context({"kind": "structure", "target": self.target, "scope_checked": True,
                                     "before": {"merged": False}, "expected": {"merged": True}})
        payload["current"]["groups"][self.gid]["locators"] = ["replacement1", "replacement2"]
        with self.assertRaises(daily.ConflictError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload))

    def test_summary_readback_checks_every_managed_field_and_fresh_daily_rows(self):
        self.adopt_text()
        self.adopt_image()
        payload = self.summary_payload(extra=True)
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        for field, value in [("date", "2026-10-11"), ("item_count", 2), ("daily_amount", "0.81"), ("status", "部分完成")]:
            observed = {**payload["expected"], field: value}
            readback = self.readback(payload, operation_id=out["operation_id"], observed=observed)
            result = self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
            self.assertEqual(result["status"], "uncertain", field)
        readback = self.readback(payload, operation_id=out["operation_id"])
        readback["daily_snapshot"]["items"][-1]["name"] = "计算汇总期间被他人改过"
        result = self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
        self.assertEqual(result["status"], "uncertain")  # Same count/amount still detects a changed purchase.
        self.assertIn("changed", result["error"])
        self.assertNotEqual(daily.status(self.project, self.project.load())["status"], "completed")
        readback = self.readback(payload, operation_id=out["operation_id"])
        readback["daily_snapshot"]["items"].reverse()  # Harmless view ordering is accepted.
        result = self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
        self.assertEqual(result["status"], "verified")

    def test_saved_summary_survives_invalidation_and_detects_external_edits(self):
        self.adopt_text()
        self.adopt_image()
        payload = self.summary_payload()
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.project.mutate(lambda state: daily.resolve(self.project, state, self.readback(payload, operation_id=out["operation_id"])))
        (self.folder / "new.png").write_bytes(png((0, 0, 255)))
        self.project.mutate(lambda state: daily.scan(self.project, state))
        self.assertEqual(self.project.load()["summary"]["data"], payload["expected"])
        second = self.summary_payload()
        second["expected"]["status"] = "部分完成"
        second["before"] = {**payload["expected"], "daily_amount": "8.00"}
        second = self.write_context(second)
        with self.assertRaises(daily.ConflictError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, second))

    def test_summary_ignores_unrelated_document_version_changes(self):
        self.adopt_text()
        self.adopt_image()
        payload = self.summary_payload()
        payload["current"]["document_version"] = "someone-edited-another-date"
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.assertEqual(out["status"], "prepared")
        self.assertEqual(payload["target"]["record_id"], "summary-1012")

    def test_expired_embedded_photo_is_preserved_when_adding_another_photo(self):
        self.adopt_text()
        self.adopt_image()
        instant = dt.datetime.now(dt.timezone.utc)
        self.project.mutate(lambda state: daily.cache_image(self.project, state, {
            "sha256": self.physical, "image_id": "already-embedded-id",
            "uploaded_at": (instant - dt.timedelta(days=2)).isoformat(), "expires_at": (instant - dt.timedelta(days=1)).isoformat()}))
        info = daily.image_cache_status(self.project.load(), self.physical)
        self.assertTrue(info["associated"])
        self.assertFalse(info["usable"])
        (self.folder / "second-photo.png").write_bytes(png((0, 0, 255)))
        added = daily.file_hash(self.folder / "second-photo.png")
        self.project.mutate(lambda state: daily.scan(self.project, state))
        raw = copy.deepcopy(self.raw)
        raw["physical_sha256"].append(added)
        raw["viewed_sha256"].append(added)
        self.project.mutate(lambda state: daily.identify(self.project, state, {"groups": [raw]}))
        self.project.mutate(lambda state: daily.cache_image(self.project, state, {
            "sha256": added, "image_id": "new-id", "uploaded_at": instant.isoformat(),
            "expires_at": (instant + dt.timedelta(days=1)).isoformat()}))
        payload = self.write_context({"kind": "image", "group_id": self.gid, "target": self.target,
            "scope_checked": True, "before": {"image_ids": ["already-embedded-id"]},
            "expected": {"image_ids": ["already-embedded-id", "new-id"]}})
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        result = self.project.mutate(lambda state: daily.resolve(self.project, state,
                    self.readback(payload, operation_id=out["operation_id"], locators=["r1", "r2"])))
        self.assertEqual(result["status"], "verified")

    def test_saved_structure_detects_change_between_calls(self):
        payload = self.text_payload()
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.project.mutate(lambda state: daily.resolve(self.project, state,
            self.readback(payload, operation_id=out["operation_id"], locators=["r1", "r2"])))
        payload = self.write_context({"kind": "structure", "target": self.target, "scope_checked": True,
                                     "before": {"merged": False}, "expected": {"merged": True}})
        payload["before_structure"]["merged_cells"] = [{"start_row": 1, "end_row": 3}]
        payload["current"]["structure"] = copy.deepcopy(payload["before_structure"])
        with self.assertRaises(daily.ConflictError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload))

    def test_type_adapters_and_decimal_precision(self):
        adapter = daily.TencentAdapter
        self.assertEqual(adapter.write_cell("sheet", Decimal("0.30"), "money"), {"value_type": "NUMBER", "number_value": 0.3})
        self.assertEqual(adapter.write_cell("smartsheet", Decimal("0.30"), "currency"), {"number_value": 0.3})
        self.assertEqual(adapter.write_cell("smartsheet", "水泥", "text"), {"text_value": {"items": [{"text": "水泥", "type": "text"}]}})
        self.assertEqual(adapter.write_cell("smartsheet", self.date, "dateTime"), {"string_value": "1791734400000"})
        self.assertEqual(adapter.write_cell("smartsheet", ["id", "id"], "image"), {"image_value": {"items": [{"image_id": "id"}]}})
        self.assertIsNone(adapter.write_cell("sheet", None, "number"))
        for value, field in [(True, "number"), ("0.001", "money"), ("123456789.123456789", "number")]:
            with self.assertRaises(ValueError):
                adapter.write_cell("sheet", value, field)

    def test_merge_adapter_prevents_cross_date_or_existing_merge_damage(self):
        merge = daily.TencentAdapter.merge_range
        bounds = {"start_row": 2, "end_row": 5, "start_col": 0, "end_col": 7}
        expected = {"start_row": 2, "end_row": 3, "start_col": 7, "end_col": 7}
        self.assertEqual(merge("sheet", [2, 3], 7, bounds, [], True), {**expected, "merge_type": "all"})
        self.assertIsNone(merge("sheet", [2, 3], 7, bounds, [expected], False))
        for sheet, rows, merges, blank in [("smartsheet", [2, 3], [], True), ("sheet", [1, 2], [], True),
                                          ("sheet", [2, 3], [{**expected, "end_row": 4}], True), ("sheet", [2, 3], [], False)]:
            with self.assertRaises(ValueError):
                merge(sheet, rows, 7, bounds, merges, blank)

    def sheet_project_with_text(self):
        self.config["document_type"] = "sheet"
        self.write_config()
        project = daily.Project(self.root, self.date)
        project.mutate(lambda state: daily.scan(project, state))
        project.mutate(lambda state: daily.identify(project, state, {"groups": [self.raw]}))
        expected = {"items": project.load()["groups"][self.gid]["items"]}
        project.mutate(lambda state: daily.adopt(project, state, {"kind": "text", "group_id": self.gid,
            "target": self.target, "scope_checked": True, "expected": expected, "observed": expected,
            "locators": [2, 3], "evidence": "current-cells"}))
        return project

    def test_sheet_expired_upload_is_blocked_but_original_insertion_is_allowed(self):
        project = self.sheet_project_with_text()
        instant = dt.datetime.now(dt.timezone.utc)
        project.mutate(lambda state: daily.cache_image(project, state, {
            "sha256": self.physical, "image_id": "expired-sheet-id",
            "uploaded_at": (instant - dt.timedelta(days=2)).isoformat(), "expires_at": (instant - dt.timedelta(days=1)).isoformat()}))
        payload = self.write_context({"kind": "image", "group_id": self.gid, "target": self.target, "scope_checked": True,
            "before": {"visual_sha256": []}, "expected": {"visual_sha256": [self.physical]},
            "upload_ids": {self.physical: "expired-sheet-id"}}, project)
        with self.assertRaises(ValueError):
            project.mutate(lambda state: daily.prepare(project, state, payload))
        payload.pop("upload_ids")
        payload["original_sha256"] = [self.physical]
        out = project.mutate(lambda state: daily.prepare(project, state, payload))
        result = project.mutate(lambda state: daily.resolve(project, state,
            self.readback(payload, operation_id=out["operation_id"], locators=[2, 3])))
        self.assertEqual(result["status"], "verified")

    def test_sheet_relocates_verified_purchase_only_with_current_identity_evidence(self):
        project = self.sheet_project_with_text()
        payload = self.write_context({"kind": "image", "group_id": self.gid, "target": self.target, "scope_checked": True,
            "before": {"visual_sha256": []}, "expected": {"visual_sha256": [self.physical]},
            "original_sha256": [self.physical]}, project)
        payload["current"]["groups"][self.gid]["locators"] = [4, 5]
        out = project.mutate(lambda state: daily.prepare(project, state, payload))
        self.assertEqual(project.load()["groups"][self.gid]["locators"], [4, 5])
        with self.assertRaises(ValueError):
            project.mutate(lambda state: daily.resolve(project, state,
                self.readback(payload, operation_id=out["operation_id"], locators=[2, 3])))
        result = project.mutate(lambda state: daily.resolve(project, state,
            self.readback(payload, operation_id=out["operation_id"], locators=[4, 5])))
        self.assertEqual(result["status"], "verified")

    def test_legacy_pending_operation_requires_explicit_current_review(self):
        payload = self.text_payload()
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.project.mutate(lambda state: state["operations"][-1].pop("before_structure"))
        readback = self.readback(payload, operation_id=out["operation_id"], locators=["r1", "r2"])
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
        readback["legacy_readback_reviewed"] = True
        result = self.project.mutate(lambda state: daily.resolve(self.project, state, readback))
        self.assertEqual(result["status"], "verified")

    def test_legacy_stale_summary_restores_actual_baseline_from_journal(self):
        self.adopt_text()
        self.adopt_image()
        payload = self.summary_payload()
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        self.project.mutate(lambda state: daily.resolve(self.project, state,
            self.readback(payload, operation_id=out["operation_id"])))
        self.project.mutate(lambda state: state.update(summary={"status": "stale"}))
        restored = daily.Project(self.root, self.date).load()["summary"]
        self.assertEqual(restored["status"], "stale")
        self.assertEqual(restored["data"], payload["expected"])

    def test_cli_conflict_leaves_no_prepared_operation(self):
        payload = self.text_payload()
        payload["current"]["observed"] = {"items": [{"name": "已被他人占用"}]}
        source = self.folder / "conflict.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(daily.__file__)), "prepare", "--project", str(self.root),
                                 "--date", self.date, "--input", str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "conflict")
        self.assertEqual(self.project.load()["operations"], [])

    def test_cli_adapter_and_cache_queries_do_not_mutate_state(self):
        before = self.project.path.read_bytes()
        source = self.folder / "adapter.json"
        source.write_text(json.dumps({"field_type": "money", "value": "0.30"}), encoding="utf-8")
        base = [sys.executable, str(Path(daily.__file__))]
        options = ["--project", str(self.root), "--date", self.date]
        result = subprocess.run(base + ["adapt"] + options + ["--input", str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["payload"], {"number_value": 0.3})
        source.write_text(json.dumps({"sha256": [self.physical]}), encoding="utf-8")
        result = subprocess.run(base + ["image-cache"] + options + ["--input", str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["images"][0]["status"], "missing")
        self.assertEqual(self.project.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
