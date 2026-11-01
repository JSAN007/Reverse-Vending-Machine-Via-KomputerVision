"""Isolated browser QA server: mocked AI, real SQLite/API/PDF. Never use for real deposits."""
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main
from rvm import RVMStore

main.store = RVMStore(ROOT / ".tmp" / f"browser-{uuid.uuid4()}.sqlite3")
qa_mode = "low"


@main.app.post("/__qa/mode/{mode}")
def set_qa_mode(mode: str):
    global qa_mode
    if mode not in {"low", "high", "background"}:
        raise main.HTTPException(400, "Unknown QA mode")
    qa_mode = mode
    return {"mode": qa_mode}


async def fake_classify(file):
    session = main.store.current()
    brands = ["AQUA", "Le Minerale", "Nestle Pure Life"]
    label = brands[session["bottles"] % len(brands)] if session else "AQUA"
    if qa_mode == "background":
        label = "bukan_botol"
    score = .35 if qa_mode == "low" else .97
    return {"prediction": {"label": label, "confidence": score},
        "classes": [{"label": label, "confidence": score}],
        "detections": [], "width": 640, "height": 480}


main.classify_image = fake_classify

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=8001, log_level="warning")
