import io
import os
import sqlite3
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfReader

import main
from rvm import RVMStore, DEFAULT_SETTINGS
from detection import assess_prediction, filter_prediction, minimum_confidence


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / ".tmp" / "tests"
TEST_DIR.mkdir(parents=True, exist_ok=True)


class RVMTests(unittest.TestCase):
    def setUp(self):
        policy_patch = patch.dict(os.environ, {"BOTTLE_MIN_CONFIDENCE": "0.90"})
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        self.store = RVMStore(TEST_DIR / f"{uuid.uuid4()}.sqlite3")
        self.store_patch = patch.object(main, "store", self.store)
        self.store_patch.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.store_patch.stop)
        self.addCleanup(lambda: self.client_context.__exit__(None, None, None))
        self.config = dict(DEFAULT_SETTINGS)

    def start(self):
        self.assertEqual(self.client.put("/api/rvm/settings", json=self.config).status_code, 200)
        return self.client.post("/api/rvm/sessions").json()

    def scan(self, session_id, label="AQUA", confidence=.97):
        async def classify(file):
            return {"prediction": {"label": label, "confidence": confidence},
                "classes": [{"label": label, "confidence": confidence}], "detections": [], "width": 640, "height": 480}
        with patch.object(main, "classify_image", classify):
            return self.client.post(f"/api/rvm/sessions/{session_id}/scan", files={"file": ("frame.jpg", b"mock", "image/jpeg")})

    def ready(self, session_id):
        response = self.scan(session_id)
        self.assertEqual(response.status_code, 200)
        return response.json()["session"]["candidate"]["id"]

    def test_start_without_setup_and_price_validation(self):
        self.assertEqual(self.client.post("/api/rvm/sessions").status_code, 200)
        for change in ({"price_per_bottle": -1}, {"price_per_bottle": 1.5}, {"price_per_bottle": 100001}):
            self.assertEqual(self.client.put("/api/rvm/settings", json={**self.config, **change}).status_code, 422)

    def test_single_active_session_and_settings_for_next_session(self):
        session = self.start()
        self.assertEqual(self.client.post("/api/rvm/sessions").json()["id"], session["id"])
        self.assertEqual(self.client.put("/api/rvm/settings", json=self.config).status_code, 200)
        self.assertEqual(self.store.get(session["id"])["config"],session["config"])
        self.assertEqual(self.client.get("/api/rvm/current").json()["id"], session["id"])

    def test_three_brands_can_be_confirmed_without_manual_brand_setup(self):
        sid = self.start()["id"]
        for label in ("AQUA", "Le Minerale", "Nestle Pure Life"):
            result = self.scan(sid, label, .97).json()["session"]
            self.assertEqual(result["phase"], "ready")
            self.assertEqual(result["candidate"]["label"], label)
            self.assertEqual(result["bottles"], 0)
            self.store.reset_scan(sid)
        self.assertEqual(self.scan(sid, " ").json()["session"]["phase"], "scanning")

    def test_low_confidence_scan_has_no_detection_or_confirmable_candidate(self):
        sid = self.start()["id"]
        for confidence in (0, .3, .8999999):
            result = self.scan(sid, confidence=confidence).json()
            self.assertFalse(result["bottle_detected"])
            self.assertIsNone(result["prediction"])
            self.assertEqual(result["model_prediction"]["confidence"], confidence)
            self.assertEqual(result["min_confidence"], .9)
            self.assertEqual(result["rejection_reason"], "low_confidence")
            state = result["session"]
            self.assertEqual(state["phase"], "scanning")
            self.assertIsNone(state["candidate"])
            self.assertEqual((state["bottles"], state["total_rupiah"]), (0, 0))
        result = self.scan(sid, confidence=.9).json()
        self.assertTrue(result["bottle_detected"])
        self.assertEqual(result["session"]["phase"], "ready")

    def test_public_predict_applies_same_gate_and_removes_rejected_boxes(self):
        async def low_confidence(file):
            return {"prediction": {"label": "AQUA", "confidence": .4},
                "classes": [{"label": "AQUA", "confidence": .4}],
                "detections": [{"label": "AQUA", "confidence": .4, "box": [0, 0, 20, 20]}],
                "width": 32, "height": 32, "count": 1, "has_boxes": True}
        with patch.object(main, "classify_image", low_confidence):
            result = self.client.post("/predict", files={"file": ("frame.jpg", b"mock", "image/jpeg")}).json()
        self.assertFalse(result["bottle_detected"])
        self.assertIsNone(result["prediction"])
        self.assertEqual(result["detections"], [])
        self.assertEqual(result["count"], 0)
        self.assertFalse(result["has_boxes"])

    def test_negative_class_and_invalid_scores_are_not_bottles(self):
        sid = self.start()["id"]
        for label in ("bukan_botol", "background", "no-bottle", "other"):
            result = self.scan(sid, label, .99).json()
            self.assertFalse(result["bottle_detected"])
            self.assertEqual(result["rejection_reason"], "background")
            self.assertIsNone(result["session"]["candidate"])
        for score in (float("nan"), float("inf"), -1, 1.1, True, "0.99", None):
            self.assertFalse(assess_prediction({"label": "AQUA", "confidence": score})["bottle_detected"])

    def test_confidence_gate_cannot_be_bypassed_in_store_or_old_candidate(self):
        sid = self.start()["id"]
        version = self.store.scan_version(sid)
        result = self.store.observe(sid, version, {"label": "AQUA", "confidence": .4})
        self.assertFalse(result["scan_accepted"])
        self.assertIsNone(result["candidate"])
        candidate = self.ready(sid)
        with self.store.connection(True) as db:
            db.execute("UPDATE sessions SET candidate_confidence=.4 WHERE id=?", (sid,))
        self.assertEqual(self.client.post(f"/api/rvm/sessions/{sid}/confirm", json={"candidate_id": candidate}).status_code, 409)
        self.assertIsNone(self.store.current()["candidate"])
        self.assertEqual(self.store.get(sid)["bottles"], 0)

    def test_restart_clears_old_weak_candidate_without_changing_deposits(self):
        sid = self.start()["id"]
        self.store.confirm(sid, self.ready(sid))
        self.store.reset_scan(sid)
        self.ready(sid)
        with self.store.connection(True) as db:
            db.execute("UPDATE sessions SET candidate_confidence=.4 WHERE id=?", (sid,))
        self.store.initialize()
        self.assertEqual(self.store.get(sid)["phase"], "scanning")
        self.assertIsNone(self.store.get(sid)["candidate"])
        self.assertEqual(self.store.get(sid)["total_rupiah"], 100)

    def test_percent_scores_are_normalized_before_thresholding(self):
        raw = {"class": "AQUA", "confidence": 95, "x": 20, "y": 20, "width": 10, "height": 10}
        result = filter_prediction(main.normalize_result(raw))
        self.assertTrue(result["bottle_detected"])
        self.assertEqual(result["prediction"]["confidence"], .95)
        self.assertEqual(result["detections"][0]["confidence"], .95)
        self.assertFalse(filter_prediction(main.normalize_result({**raw, "confidence": 40}))["bottle_detected"])
        self.assertFalse(filter_prediction(main.normalize_result({**raw, "confidence": True}))["bottle_detected"])

    def test_threshold_override_and_bad_configuration(self):
        with patch.dict(os.environ, {"BOTTLE_MIN_CONFIDENCE": "0.95"}):
            self.assertFalse(assess_prediction({"label": "AQUA", "confidence": .94})["bottle_detected"])
            self.assertTrue(assess_prediction({"label": "AQUA", "confidence": .95})["bottle_detected"])
        for value in ("bad", "nan", "0", "1.1"):
            with patch.dict(os.environ, {"BOTTLE_MIN_CONFIDENCE": value}):
                with self.assertRaises(ValueError):
                    minimum_confidence()

    def test_no_credit_before_confirmation_and_no_repeat_frame_credit(self):
        sid = self.start()["id"]
        candidate = self.ready(sid)
        self.assertEqual(self.store.get(sid)["bottles"], 0)
        self.assertEqual(self.scan(sid).status_code, 409)
        path = f"/api/rvm/sessions/{sid}/confirm"
        first = self.client.post(path, json={"candidate_id": candidate}).json()
        second = self.client.post(path, json={"candidate_id": candidate}).json()
        self.assertEqual((first["bottles"], first["total_rupiah"], first["phase"]), (1, 100, "await_next"))
        self.assertTrue(second["already_recorded"])
        self.assertEqual(second["bottles"], 1)
        self.assertEqual(self.scan(sid).status_code, 409)
        self.assertEqual(self.client.post(f"/api/rvm/sessions/{sid}/next").status_code, 200)
        candidate2 = self.ready(sid)
        self.assertNotEqual(candidate, candidate2)
        self.client.post(path, json={"candidate_id": candidate2})
        self.assertEqual(self.store.get(sid)["total_rupiah"], 200)
        self.assertEqual(self.store.summary()["total_rupiah"], 200)

    def test_forged_expired_and_cross_session_candidates_rejected(self):
        sid = self.start()["id"]
        self.assertEqual(self.client.post(f"/api/rvm/sessions/{sid}/confirm", json={"candidate_id": "fake"}).status_code, 409)
        candidate = self.ready(sid)
        with self.store.connection(True) as db:
            db.execute("UPDATE sessions SET expires_at=0 WHERE id=?", (sid,))
        self.assertEqual(self.client.post(f"/api/rvm/sessions/{sid}/confirm", json={"candidate_id": candidate}).status_code, 409)
        self.assertEqual(self.store.get(sid)["phase"], "scanning")
        self.assertEqual(self.store.get(sid)["bottles"], 0)

    def test_old_scan_cannot_qualify_after_reset(self):
        sid = self.start()["id"]
        version = self.store.scan_version(sid)
        self.store.reset_scan(sid)
        with self.assertRaises(HTTPException):
            self.store.observe(sid, version, {"label": "AQUA", "confidence": .97})

    def test_inference_error_leaves_nothing_to_confirm(self):
        sid = self.start()["id"]
        async def failure(file):
            raise HTTPException(504, "Roboflow timeout")
        with patch.object(main, "classify_image", failure):
            response = self.client.post(f"/api/rvm/sessions/{sid}/scan", files={"file": ("frame.jpg", b"mock", "image/jpeg")})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.store.get(sid)["streak"], 0)
        self.assertIsNone(self.store.get(sid)["candidate"])

    def test_parallel_confirmations_credit_once(self):
        sid = self.start()["id"]
        candidate = self.ready(sid)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.confirm(sid, candidate), range(8)))
        self.assertEqual(sum(not item["already_recorded"] for item in results), 1)
        self.assertEqual(self.store.get(sid)["bottles"], 1)
        self.assertEqual(self.store.summary()["total_rupiah"], 100)

    def test_deposit_and_value_ledger_rollback_together(self):
        sid = self.start()["id"]
        candidate = self.ready(sid)
        with self.store.connection(True) as db:
            db.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON value_ledger BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.confirm(sid, candidate)
        self.assertEqual(self.store.get(sid)["bottles"], 0)
        self.assertEqual(self.store.summary()["total_rupiah"], 0)
        self.assertEqual(self.store.get(sid)["phase"], "ready")

    def test_restart_finish_receipt_and_price_snapshot(self):
        sid = self.start()["id"]
        self.assertEqual(self.client.get(f"/api/rvm/sessions/{sid}/receipt.pdf").status_code, 409)
        candidate = self.ready(sid)
        self.store.confirm(sid, candidate)
        restarted = RVMStore(self.store.path)
        restarted.initialize()
        self.assertEqual(restarted.current()["bottles"], 1)
        completed = self.client.post(f"/api/rvm/sessions/{sid}/finish").json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.client.post(f"/api/rvm/sessions/{sid}/finish").json()["finished_at"], completed["finished_at"])
        self.assertEqual(self.scan(sid).status_code, 409)
        self.assertTrue(self.store.confirm(sid, candidate)["already_recorded"])
        self.assertIsNone(self.store.current())
        self.client.put("/api/rvm/settings", json={**self.config, "price_per_bottle": 500})
        self.assertEqual(self.client.post("/api/rvm/sessions").json()["config"]["price_per_bottle"], 500)
        self.assertEqual(self.store.get(sid)["total_rupiah"], 100)
        pdf = self.client.get(f"/api/rvm/sessions/{sid}/receipt.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertIn("attachment", pdf.headers["content-disposition"])
        reader = PdfReader(io.BytesIO(pdf.content))
        self.assertEqual(len(reader.pages), 1)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.width), 80 * 72 / 25.4, places=3)
        self.assertLess(float(reader.pages[0].mediabox.height), 210 * 72 / 25.4)
        self.assertLess(len(pdf.content), 12000)
        text = "".join(page.extract_text() for page in reader.pages)
        for expected in ("AQUA", "Rp100", sid, "WIB", "VOUCHER BELANJA", "NILAI VOUCHER", "Penukaran voucher belum tersedia"):
            self.assertIn(expected, text)
        self.assertEqual(self.client.get("/api/rvm/history").json()["total"], 1)

    def test_mixed_brands_in_one_session_and_one_small_receipt(self):
        sid = self.start()["id"]
        for label in ("AQUA", "Le Minerale", "Nestle Pure Life", "AQUA"):
            state = self.scan(sid, label).json()["session"]
            self.store.confirm(sid, state["candidate"]["id"])
            self.store.reset_scan(sid)
        completed = self.store.finish(sid)
        self.assertEqual((completed["bottles"], completed["total_rupiah"]), (4, 400))
        self.assertEqual(len({item["label"] for item in completed["deposits"]}), 3)
        content = self.client.get(f"/api/rvm/sessions/{sid}/receipt.pdf").content
        reader = PdfReader(io.BytesIO(content))
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        for expected in ("AQUA", "Le Minerale", "Nestle Pure Life", "2 x Rp100", "Rp400"):
            self.assertIn(expected, text)

    def test_retiring_legacy_rules_preserves_session_and_tariff(self):
        legacy = {"price_per_bottle": 250, "accepted_labels": ["AQUA"], "min_confidence": .99, "stable_frames": 8}
        with self.store.connection(True) as db:
            import json
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(legacy),))
        sid = self.store.start()["id"]
        self.store.initialize()
        self.assertEqual(self.store.settings(), {"price_per_bottle": 250, "brand_prices": {}})
        self.assertEqual(self.store.current()["id"], sid)
        self.assertIsNone(self.scan(sid, "Le Minerale", .4).json()["session"]["candidate"])
        state = self.scan(sid, "Le Minerale", .95).json()["session"]
        result = self.store.confirm(sid, state["candidate"]["id"])
        self.assertEqual(result["total_rupiah"], 250)

    def test_long_receipt_wraps_and_keeps_all_items(self):
        from receipt import build_receipt
        from rvm import now
        sample = {"id": str(uuid.uuid4()), "finished_at": now(), "bottles": 70, "total_rupiah": 7000,
            "deposits": [{"label": f"Merek-{i:02d}-" + "Botol" * 18, "amount_rupiah": 100} for i in range(70)]}
        content = build_receipt(sample)
        reader = PdfReader(io.BytesIO(content))
        text = "".join(page.extract_text() for page in reader.pages)
        for i in range(70):
            self.assertIn(f"Merek-{i:02d}-", text)
        self.assertIn("Rp7.000", text)
        for page in reader.pages:
            self.assertAlmostEqual(float(page.mediabox.width), 80 * 72 / 25.4, places=3)
            self.assertLessEqual(float(page.mediabox.height), 281 * 72 / 25.4)

    def test_finish_invalidates_unconfirmed_candidate(self):
        sid = self.start()["id"]
        candidate = self.ready(sid)
        self.store.finish(sid)
        with self.assertRaises(HTTPException):
            self.store.confirm(sid, candidate)
        self.assertEqual(self.store.get(sid)["total_rupiah"], 0)

    def test_public_predict_does_not_count_and_invalid_file_rejected(self):
        from PIL import Image
        picture = io.BytesIO(); Image.new("RGB", (32, 32)).save(picture, format="JPEG")
        class Upstream:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                import httpx
                return httpx.Response(200, json={"predictions": {"top": "AQUA", "confidence": .98}}, request=httpx.Request("POST", "https://example.invalid"))
        with patch.dict(os.environ, {"ROBOFLOW_API_KEY": "test-only"}), patch.object(main.httpx, "AsyncClient", return_value=Upstream()):
            response = self.client.post("/predict", files={"file": ("photo.jpg", picture.getvalue(), "image/jpeg")})
            self.assertEqual(response.json()["prediction"]["label"], "AQUA")
            self.assertEqual(self.store.summary()["bottles"], 0)
            self.assertEqual(self.client.post("/predict", files={"file": ("photo.jpg", b"not an image", "image/jpeg")}).status_code, 400)

    def test_history_pagination_and_missing_session(self):
        self.start()
        for _ in range(3):
            self.store.finish(self.store.current()["id"])
            self.store.start()
        data = self.client.get("/api/rvm/history?limit=2&offset=2").json()
        self.assertEqual((data["total"], len(data["items"])), (3, 1))
        self.assertEqual(self.client.get("/api/rvm/history?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/rvm/sessions/missing").status_code, 404)

    def test_transaction_views_group_mixed_brands_and_preserve_empty_sessions(self):
        first = self.start()["id"]
        for label in ("AQUA", "Le Minerale", "AQUA"):
            state = self.scan(first, label).json()["session"]
            self.store.confirm(first, state["candidate"]["id"])
            self.store.reset_scan(first)
        completed = self.store.finish(first)
        second = self.store.start()["id"]
        # Applying the upgrade repeatedly is safe and retains the live session.
        self.store.create_transaction_views()
        self.store.create_transaction_views()
        with self.store.connection() as db:
            rows = {row["transaksi_id"]: dict(row) for row in db.execute("SELECT * FROM transaksi")}
            self.assertEqual(len(rows), 2)
            one = rows[first]
            self.assertEqual((one["jumlah_botol"], one["jumlah_jenis"], one["total_rupiah"]), (3, 2, 300))
            self.assertEqual(one["status"], "selesai")
            self.assertIn("AQUA x2", one["rincian_merek"])
            self.assertIn("Le Minerale x1", one["rincian_merek"])
            self.assertEqual(rows[second]["jumlah_botol"], 0)
            self.assertEqual(rows[second]["total_rupiah"], 0)
            self.assertEqual(rows[second]["status"], "berlangsung")
            self.assertEqual(rows[second]["rincian_merek"], "Belum ada botol")
            self.assertIsNone(rows[second]["selesai_wib"])
            details = [dict(row) for row in db.execute("SELECT * FROM rincian_transaksi WHERE transaksi_id=? ORDER BY merek", (first,))]
            self.assertEqual(len(details), 2)
            self.assertEqual(details[0]["jumlah_botol"], 2)
            self.assertEqual(details[0]["subtotal_rupiah"], 200)
            self.assertEqual(details[0]["tarif_per_botol_rupiah"], 100)
            from datetime import datetime, timedelta
            expected = (datetime.fromisoformat(completed["finished_at"]) + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
            self.assertEqual(one["selesai_wib"], expected)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deposits").fetchone()[0], 3)
        # Views stay current on later inserts and retain each session's own tariff.
        self.store.confirm(second, self.ready(second))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT total_rupiah FROM transaksi WHERE transaksi_id=?", (second,)).fetchone()[0], 100)
            self.assertEqual(db.execute("SELECT SUM(total_rupiah) FROM transaksi").fetchone()[0], 400)

    def test_transaction_crud_updates_ledger_views_and_pdf(self):
        body = {"items": [{"label": "AQUA", "quantity": 2, "price_per_bottle": 100},
            {"label": "Le Minerale", "quantity": 1, "price_per_bottle": 150}]}
        response = self.client.post("/api/rvm/transactions", json=body)
        self.assertEqual(response.status_code, 201)
        created = response.json()
        sid = created["id"]
        self.assertEqual((created["bottles"], created["total_rupiah"]), (3, 350))
        self.assertTrue(all(item["entry_method"] == "manual" and item["confidence"] == 0 for item in created["deposits"]))
        self.assertEqual(self.client.get(f"/api/rvm/transactions/{sid}").json()["id"], sid)
        self.assertEqual(self.client.get("/api/rvm/transactions").json()["total"], 1)
        updated = self.client.put(f"/api/rvm/transactions/{sid}", json={"expected_version": created["version"],
            "items": [body["items"][0], {"label": "Le Minerale", "quantity": 2, "price_per_bottle": 200}]}).json()
        self.assertEqual((updated["bottles"], updated["total_rupiah"]), (4, 600))
        self.assertEqual(self.store.summary()["total_rupiah"], 600)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT total_rupiah FROM transaksi WHERE transaksi_id=?", (sid,)).fetchone()[0], 600)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM value_ledger WHERE session_id=?", (sid,)).fetchone()[0], 4)
        pdf = self.client.get(f"/api/rvm/sessions/{sid}/receipt.pdf")
        self.assertIn("Rp600", "".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages))
        self.assertEqual(self.client.delete(f"/api/rvm/transactions/{sid}?expected_version={created['version']}").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/rvm/transactions/{sid}?expected_version={updated['version']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/rvm/transactions/{sid}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/rvm/sessions/{sid}/receipt.pdf").status_code, 404)
        self.assertEqual(self.store.summary()["total_rupiah"], 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM value_ledger").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deposits").fetchone()[0], 0)
            self.assertEqual([row[0] for row in db.execute("SELECT action FROM transaction_changes ORDER BY id")], ["create", "update", "delete"])
            self.assertIn('"total_rupiah": 600', db.execute("SELECT before_json FROM transaction_changes WHERE action='delete'").fetchone()[0])

    def test_edit_retains_unchanged_camera_evidence_and_prevents_stale_write(self):
        sid = self.start()["id"]
        candidate = self.ready(sid)
        self.store.confirm(sid, candidate)
        original = self.store.finish(sid)
        body = {"expected_version": original["version"], "items": [{"label": "AQUA", "quantity": 2, "price_per_bottle": 100}]}
        response = self.client.put(f"/api/rvm/transactions/{sid}", json=body)
        self.assertEqual(response.status_code, 200)
        updated = response.json()
        kept = next(item for item in updated["deposits"] if item["id"] == candidate)
        self.assertEqual((kept["confidence"], kept["entry_method"]), (.97, "camera"))
        self.assertEqual(updated["finished_at"], original["finished_at"])
        self.assertEqual(self.client.put(f"/api/rvm/transactions/{sid}", json=body).status_code, 409)
        self.assertEqual(self.store.get(sid)["bottles"], 2)

    def test_active_session_cannot_be_edited_or_deleted(self):
        sid = self.start()["id"]
        body = {"expected_version": 0, "items": [{"label": "AQUA", "quantity": 1, "price_per_bottle": 100}]}
        self.assertEqual(self.client.put(f"/api/rvm/transactions/{sid}", json=body).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/rvm/transactions/{sid}?expected_version=0").status_code, 409)
        self.assertEqual(self.client.post("/api/rvm/transactions", json={"items": body["items"]}).status_code, 201)
        self.assertEqual(self.store.current()["id"], sid)
        self.assertEqual(self.store.current()["bottles"], 0)

    def test_manual_create_retries_do_not_duplicate_transactions(self):
        request_id = str(uuid.uuid4())
        body = {"request_id": request_id, "items": [{"label": "AQUA", "quantity": 1, "price_per_bottle": 100}]}
        first = self.client.post("/api/rvm/transactions", json=body).json()
        second = self.client.post("/api/rvm/transactions", json=body).json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.summary()["bottles"], 1)
        changed = {**body, "items": [{**body["items"][0], "quantity": 2}]}
        self.assertEqual(self.client.post("/api/rvm/transactions", json=changed).status_code, 409)
        self.store.delete_transaction(first["id"], first["version"])
        self.assertEqual(self.client.post("/api/rvm/transactions", json=body).status_code, 409)

    def test_manual_input_validation(self):
        base = {"label": "AQUA", "quantity": 1, "price_per_bottle": 100}
        for item in ({**base, "label": " "}, {**base, "quantity": 0}, {**base, "quantity": 1.5},
            {**base, "price_per_bottle": -1}, {**base, "price_per_bottle": 100001}):
            self.assertEqual(self.client.post("/api/rvm/transactions", json={"items": [item]}).status_code, 422)
        self.assertEqual(self.client.post("/api/rvm/transactions", json={"items": []}).status_code, 422)
        self.assertEqual(self.client.post("/api/rvm/transactions", json={"items": [{**base, "quantity": 600}]*2}).status_code, 422)
        self.assertEqual(self.store.summary()["bottles"], 0)

    def test_failed_manual_edit_rolls_back_entire_transaction(self):
        original = self.store.create_transaction([{"label": "AQUA", "quantity": 1, "price_per_bottle": 100}])
        with self.store.connection(True) as db:
            db.execute("CREATE TRIGGER fail_manual_ledger BEFORE INSERT ON value_ledger BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_transaction(original["id"], original["version"], [{"label": "Le Minerale", "quantity": 2, "price_per_bottle": 200}])
        self.assertEqual(self.store.get(original["id"]), original)
        self.assertEqual(self.store.summary()["total_rupiah"], 100)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transaction_changes").fetchone()[0], 1)

    def test_entry_method_migration_preserves_legacy_scan(self):
        sid = self.start()["id"]
        self.store.confirm(sid, self.ready(sid))
        with self.store.connection(True) as db:
            db.execute("ALTER TABLE deposits DROP COLUMN entry_method")
        self.store.initialize()
        self.store.initialize()
        state = self.store.get(sid)
        self.assertEqual(state["bottles"], 1)
        self.assertEqual(state["deposits"][0]["entry_method"], "camera")
        self.assertEqual(state["deposits"][0]["confidence"], .97)

    def test_scan_prices_each_brand_from_session_snapshot(self):
        prices = {"price_per_bottle": 50, "brand_prices": {"AQUA": 100, "Nestlé Pure Life": 150, "Le_Minerale": 200}}
        response = self.client.put("/api/rvm/settings", json=prices)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["brand_prices"], {"aqua": 100, "nestle pure life": 150, "le minerale": 200})
        sid = self.client.post("/api/rvm/sessions").json()["id"]
        for label, expected in [("AQUA",100),("Nestle_Pure-Life",150),("LE MINERALE",200),("Merek Lain",50)]:
            scan = self.scan(sid, label).json()["session"]
            self.assertEqual(scan["candidate"]["price_per_bottle"], expected)
            confirmed = self.client.post(f"/api/rvm/sessions/{sid}/confirm", json={"candidate_id": scan["candidate"]["id"], "price_per_bottle": 99999}).json()
            self.assertEqual(next(item for item in confirmed["deposits"] if item["id"]==scan["candidate"]["id"])["amount_rupiah"],expected)
            self.store.reset_scan(sid)
        completed = self.store.finish(sid)
        self.assertEqual((completed["bottles"],completed["total_rupiah"]),(4,500))
        self.assertEqual(self.store.summary()["total_rupiah"],500)
        content = self.client.get(f"/api/rvm/sessions/{sid}/receipt.pdf").content
        text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)
        for expected in ("Rp100","Rp150","Rp200","Rp50","Rp500"):
            self.assertIn(expected,text)
        changed = self.client.put("/api/rvm/settings",json={"price_per_bottle": 999,"brand_prices":{"AQUA":300}})
        self.assertEqual(changed.status_code,200)
        self.store.initialize()
        self.assertEqual(self.store.settings()["brand_prices"],{"aqua":300})
        self.assertEqual(self.store.get(sid)["total_rupiah"],500)
        self.assertEqual(self.store.get(sid)["config"]["brand_prices"]["aqua"],100)
        new_id = self.client.post("/api/rvm/sessions").json()["id"]
        self.assertEqual(self.scan(new_id).json()["session"]["candidate"]["price_per_bottle"],300)

    def test_brand_price_validation_and_legacy_settings_update(self):
        for mapping in ({"AQUA":-1},{"AQUA":1.5},{"AQUA":True},{"AQUA":100001},{" ":100},{"AQUA":100,"aqua":200}):
            self.assertEqual(self.client.put("/api/rvm/settings",json={"price_per_bottle":100,"brand_prices":mapping}).status_code,422)
        saved = self.client.put("/api/rvm/settings",json={"brand_prices":{"Aqua":0,"Le Minerale":200}})
        self.assertEqual(saved.status_code,200)
        self.store.initialize()
        self.assertEqual(self.store.settings(),{"price_per_bottle":100,"brand_prices":{"aqua":0,"le minerale":200}})
        self.client.put("/api/rvm/settings",json={"price_per_bottle":300})
        self.assertEqual(self.store.settings(),{"price_per_bottle":300,"brand_prices":{"aqua":0,"le minerale":200}})
        sid = self.client.post("/api/rvm/sessions").json()["id"]
        candidate = self.scan(sid).json()["session"]["candidate"]
        self.assertEqual(candidate["price_per_bottle"],0)
        self.assertEqual(self.store.confirm(sid,candidate["id"])["total_rupiah"],0)

    def test_legacy_uniform_pricing_is_preserved_for_old_active_session(self):
        import json
        with self.store.connection(True) as db:
            db.execute("UPDATE settings SET value=? WHERE id=1",(json.dumps({"price_per_bottle":3000}),))
        old_id = self.store.start()["id"]
        self.store.initialize()
        self.assertEqual(self.store.settings(),{"price_per_bottle":3000,"brand_prices":{}})
        saved = self.client.put("/api/rvm/settings",json={"brand_prices":{"Aqua":100,"Nestle Pure Life":150,"Le Minerale":200}})
        self.assertEqual(saved.status_code,200)
        self.assertEqual(saved.json()["brand_prices"],{"aqua":100,"nestle pure life":150,"le minerale":200})
        self.assertEqual(self.store.get(old_id)["config"],{"price_per_bottle":3000})
        candidate = self.scan(old_id,"Nestle Pure Life").json()["session"]["candidate"]
        self.assertEqual(candidate["price_per_bottle"],3000)
        self.assertEqual(self.store.confirm(old_id,candidate["id"])["total_rupiah"],3000)
        self.store.finish(old_id)
        new_id = self.store.start()["id"]
        self.assertEqual(self.scan(new_id,"Nestle Pure Life").json()["session"]["candidate"]["price_per_bottle"],150)


if __name__ == "__main__":
    unittest.main()
