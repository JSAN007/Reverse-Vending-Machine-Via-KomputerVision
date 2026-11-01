from __future__ import annotations

import base64
import io
import os
from uuid import UUID
from typing import Annotated
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator, model_validator
from rvm import RVMStore, price_key
from receipt import build_receipt
from detection import filter_prediction, minimum_confidence

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
WORKFLOW_URL = os.getenv("ROBOFLOW_WORKFLOW_URL", "https://serverless.roboflow.com/si-pais/workflows/klasifikasi-botol-v2-vklasifikasi-botol-v2-1-vit-base-patch16-224-in21k-t1-logic")
MAX_BYTES = 10 * 1024 * 1024
store = RVMStore(os.getenv("RVM_DB_PATH", str(BASE / "data" / "rvm.sqlite3")))


@asynccontextmanager
async def lifespan(app):
    minimum_confidence()
    store.initialize()
    yield


app = FastAPI(title="EcoVision - RVM Prototype", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def normalize_result(raw):
    """Best effort for Roboflow Workflow classification/detection outputs.
    Unknown schemas remain visible in raw_result for troubleshooting.
    """
    entries = raw if isinstance(raw, list) else [raw]
    classes = []
    boxes = []
    visited = set()

    def add_class(name, confidence):
        if not isinstance(name, str) or not name.strip():
            return
        if isinstance(confidence, bool):
            return
        try:
            score = float(confidence)
        except (ValueError, TypeError):
            return
        if score > 1 and score <= 100:
            score /= 100
        if not 0 <= score <= 1:
            return
        classes.append({"label": name, "confidence": score})
        return score

    def walk(node, depth=0):
        if depth > 12 or not isinstance(node, (list, dict)):
            return
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        label = node.get("class") or node.get("class_name") or node.get("label") or node.get("top") or node.get("top_class")
        confidence = node.get("confidence", node.get("score", node.get("top_confidence")))
        if label is not None and confidence is not None:
            score = add_class(label, confidence)
            if score is not None and all(k in node for k in ("x", "y", "width", "height")):
                try:
                    x, y = float(node["x"]), float(node["y"])
                    w, h = float(node["width"]), float(node["height"])
                    boxes.append({"label": str(label), "confidence": score,
                                  "box": [x-w/2, y-h/2, x+w/2, y+h/2]})
                except (ValueError, TypeError):
                    pass
        # Some classifiers report a mapping of class names to probabilities.
        for key in ("class_confidences", "scores", "probabilities"):
            scores = node.get(key)
            if isinstance(scores, dict):
                for name, value in scores.items():
                    add_class(name, value)
        for value in node.values():
            walk(value, depth + 1)

    walk(entries)
    # Keep maximum confidence per class, prioritize highest class.
    dedup = {}
    for item in classes:
        name = item["label"]
        if name not in dedup or item["confidence"] > dedup[name]["confidence"]:
            dedup[name] = item
    ranked = sorted(dedup.values(), key=lambda item: item["confidence"], reverse=True)
    best = ranked[0] if ranked else None
    return {"prediction": best, "classes": ranked, "detections": boxes,
            "count": len(boxes), "has_boxes": bool(boxes), "raw_result": raw}


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "pricing_version": 2, "provider": "Roboflow Workflows",
            "configured": bool(os.getenv("ROBOFLOW_API_KEY")), "mode": "workflow",
            "workflow_url": WORKFLOW_URL,
            "min_confidence": minimum_confidence()}


async def classify_image(file: UploadFile):
    key = os.getenv("ROBOFLOW_API_KEY")
    if not key:
        raise HTTPException(503, "ROBOFLOW_API_KEY belum disetel dalam file .env")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(400, "File harus berupa gambar.")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Maksimal gambar 10 MB.")
    try:
        picture = Image.open(io.BytesIO(data))
        picture.verify()
        picture = Image.open(io.BytesIO(data)).convert("RGB")
        if picture.width * picture.height > 20_000_000:
            raise HTTPException(413, "Resolusi terlalu besar.")
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(400, "Gambar tidak valid.")
    # Resize and compress to control bandwidth for video frames.
    picture.thumbnail((1024, 1024))
    output = io.BytesIO()
    picture.save(output, format="JPEG", quality=82)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    payload = {"inputs": {"image": {"type": "base64", "value": encoded}}}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(WORKFLOW_URL, json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            response.raise_for_status()
            raw = response.json()
    except httpx.TimeoutException:
        raise HTTPException(504, "Roboflow timeout. Coba lagi.")
    except httpx.HTTPStatusError as err:
        code = err.response.status_code
        if code in (401, 403):
            raise HTTPException(502, "Autentikasi Roboflow gagal. Periksa API key.")
        raise HTTPException(502, f"Roboflow HTTP {code}: {err.response.text[:250]}")
    except (httpx.RequestError, ValueError):
        raise HTTPException(502, "Roboflow tidak bisa dihubungi atau respons tidak valid.")
    result = normalize_result(raw)
    result.update({"width": picture.width, "height": picture.height})
    return result


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    return filter_prediction(await classify_image(file))


class SettingsInput(BaseModel):
    price_per_bottle: int | None = Field(default=None, ge=0, le=100000, strict=True)
    brand_prices: dict[str, Annotated[int, Field(ge=0, le=100000, strict=True)]] | None = Field(default=None, max_length=50)

    @field_validator("brand_prices")
    @classmethod
    def clean_brand_prices(cls, values):
        if values is None:
            return None
        cleaned = {}
        for label, price in values.items():
            key = price_key(label)
            if not key or len(label) > 120:
                raise ValueError("Nama merek tarif wajib diisi dan maksimal 120 karakter.")
            if key in cleaned and cleaned[key] != price:
                raise ValueError("Merek yang sama tidak boleh memiliki dua tarif berbeda.")
            cleaned[key] = price
        return cleaned


class ConfirmInput(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)


class TransactionItem(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    quantity: int = Field(ge=1, le=1000, strict=True)
    price_per_bottle: int = Field(ge=0, le=100000, strict=True)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Merek wajib diisi.")
        return value


class TransactionInput(BaseModel):
    items: list[TransactionItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_total(self):
        if sum(item.quantity for item in self.items) > 1000:
            raise ValueError("Maksimal 1.000 botol per transaksi manual.")
        return self


class TransactionUpdateInput(TransactionInput):
    expected_version: int = Field(ge=0, strict=True)


class TransactionCreateInput(TransactionInput):
    request_id: UUID | None = None


@app.get("/api/rvm/settings")
def get_settings():
    return store.settings()


@app.put("/api/rvm/settings")
def put_settings(body: SettingsInput):
    return store.update_settings(body.model_dump(exclude_none=True))


@app.get("/api/rvm/current")
def current_session():
    return store.current()


@app.post("/api/rvm/sessions")
def start_session():
    return store.start()


@app.get("/api/rvm/summary")
def summary():
    return store.summary()


@app.get("/api/rvm/history")
def history(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return store.history(limit, offset)


@app.get("/api/rvm/sessions/{session_id}")
def get_session(session_id: str):
    return store.get(session_id)


@app.post("/api/rvm/sessions/{session_id}/scan")
async def scan_session(session_id: str, file: UploadFile = File(...)):
    version = store.scan_version(session_id)
    try:
        result = filter_prediction(await classify_image(file))
    except HTTPException:
        # Failed inference cannot leave a confirmable result. Ignore a stale scan.
        try:
            store.observe(session_id, version, None)
        except HTTPException:
            pass
        raise
    result["session"] = store.observe(session_id, version, result["prediction"])
    return result


@app.post("/api/rvm/sessions/{session_id}/confirm")
def confirm_bottle(session_id: str, body: ConfirmInput):
    return store.confirm(session_id, body.candidate_id)


@app.post("/api/rvm/sessions/{session_id}/next")
def next_bottle(session_id: str):
    return store.reset_scan(session_id)


@app.post("/api/rvm/sessions/{session_id}/finish")
def finish_session(session_id: str):
    return store.finish(session_id)


@app.get("/api/rvm/sessions/{session_id}/receipt.pdf")
def receipt_pdf(session_id: str):
    session = store.get(session_id)
    if session["status"] != "completed":
        raise HTTPException(409, "Selesaikan sesi sebelum mengunduh struk.")
    return Response(build_receipt(session), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="EcoVision-{session["id"]}.pdf"',
        "Cache-Control": "no-store",
    })


@app.get("/api/rvm/transactions")
def list_transactions(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return store.history(limit, offset)


@app.get("/api/rvm/transactions/{session_id}")
def read_transaction(session_id: str):
    return store.get(session_id)


@app.post("/api/rvm/transactions", status_code=201)
def create_transaction(body: TransactionCreateInput):
    return store.create_transaction([item.model_dump() for item in body.items], str(body.request_id) if body.request_id else None)


@app.put("/api/rvm/transactions/{session_id}")
def update_transaction(session_id: str, body: TransactionUpdateInput):
    return store.update_transaction(session_id, body.expected_version, [item.model_dump() for item in body.items])


@app.delete("/api/rvm/transactions/{session_id}")
def delete_transaction(session_id: str, expected_version: int = Query(ge=0)):
    return store.delete_transaction(session_id, expected_version)
