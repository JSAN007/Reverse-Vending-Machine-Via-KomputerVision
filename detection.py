"""Shared confidence gate for display, camera scans, and deposit confirmation."""
import math
import os
import re


def minimum_confidence():
    try:
        value = float(os.getenv("BOTTLE_MIN_CONFIDENCE", "0.90"))
    except ValueError as error:
        raise ValueError("BOTTLE_MIN_CONFIDENCE harus berupa angka antara 0 dan 1.") from error
    if not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("BOTTLE_MIN_CONFIDENCE harus lebih dari 0 dan maksimal 1.")
    return value


# Also supports a future classifier trained with an explicit negative class.
NEGATIVE_LABELS = {
    "background", "bukan botol", "non bottle", "not bottle", "no bottle",
    "non botol", "bukanbotol", "tidak ada botol", "no object", "empty",
    "none", "unknown", "other", "others", "lainnya",
}


def assess_prediction(prediction):
    threshold = minimum_confidence()
    reason = "no_prediction"
    if isinstance(prediction, dict):
        label, score = prediction.get("label"), prediction.get("confidence")
        valid = (isinstance(label, str) and bool(label.strip())
            and isinstance(score, (int, float)) and not isinstance(score, bool)
            and math.isfinite(score) and 0 <= score <= 1)
        if valid:
            normalized = re.sub(r"[\s_-]+", " ", label.strip().casefold())
            if normalized in NEGATIVE_LABELS:
                reason = "background"
            elif score < threshold:
                reason = "low_confidence"
            else:
                reason = None
    return {"bottle_detected": reason is None, "rejection_reason": reason,
        "min_confidence": threshold}


def filter_prediction(result):
    # Keep the model's suggestion for diagnostics without presenting it as a detection.
    raw = result.get("model_prediction", result.get("prediction"))
    decision = assess_prediction(raw)
    boxes = [item for item in result.get("detections", []) if assess_prediction(item)["bottle_detected"]] if decision["bottle_detected"] else []
    return {**result, **decision, "model_prediction": raw,
        "prediction": raw if decision["bottle_detected"] else None,
        "detections": boxes, "count": len(boxes), "has_boxes": bool(boxes)}
