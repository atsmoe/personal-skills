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
        return {"kind": "text", "group_id": self.gid, "target": self.target,
                "before": {"items": []}, "expected": {"items": self.project.load()["groups"][self.gid]["items"]}, "scope_checked": True}

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
        return {"kind": "summary", "target": {"file_id": "fixture-file", "sheet_id": "summary"},
                "scope_checked": True, "before": {"date": self.date, "item_count": 0, "daily_amount": "0.00", "status": "部分完成"},
                "expected": {"date": self.date, "item_count": len(rows), "daily_amount": "0.80" if extra else "0.30", "status": "完成"},
                "daily_snapshot": {"date": self.date, "items": rows}}

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
        evidence = {"operation_id": out["operation_id"], "target": self.target, "observed": {"items": []}, "evidence": "readback"}
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
            readback = {"operation_id": out["operation_id"], "target": self.target, "observed": payload["before"],
                        "absence_verified": True, "evidence": "entire-day-absence"}
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
        payload = {"kind": "image", "group_id": self.gid, "target": self.target, "before": {"image_ids": []},
                   "expected": {"image_ids": ["expired-id"]}, "scope_checked": True}
        with self.assertRaises(ValueError):
            self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        upload.update(image_id="fresh-id", uploaded_at=instant.isoformat(), expires_at=(instant + dt.timedelta(days=1)).isoformat())
        self.project.mutate(lambda state: daily.cache_image(self.project, state, upload))
        payload["expected"]["image_ids"] = ["fresh-id"]
        out = self.project.mutate(lambda state: daily.prepare(self.project, state, payload))
        readback = {"operation_id": out["operation_id"], "target": self.target, "observed": payload["expected"],
                    "locators": ["r1", "r2"], "evidence": "image-field-readback"}
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
        evidence = {"operation_id": out["operation_id"], "target": payload["target"], "observed": payload["expected"], "evidence": "summary-readback"}
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


if __name__ == "__main__":
    unittest.main()
