#!/usr/bin/env python3
"""Validate and compile the model-facing reference-image contract."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


MODEL_REFERENCE_FIELDS = ("model_role", "spatial_map", "use", "ignore")
FIELD_LABELS = {
    "model_role": "Represents",
    "spatial_map": "Spatial map",
    "use": "Use",
    "ignore": "Ignore",
}
POSITION_MARKER_RE = re.compile(
    r"\b(?:whole|entire|full|single|left|center|centre|middle|right|top|bottom|upper|lower|"
    r"foreground|background|row|column|panel|frame|quadrant|non[- ]?positional)\b|"
    r"(?:全图|整图|整张|整体|单人|单一主体|左|中|右|上|下|前景|背景|第[一二三四五六七八九十0-9]+格|"
    r"无位置|不按位置)",
    re.IGNORECASE,
)
GENERIC_PLACEHOLDERS = {
    "reference",
    "reference image",
    "image",
    "photo",
    "picture",
    "character reference",
    "style reference",
    "same as reference",
    "参考图",
    "图片",
    "照片",
    "人物参考图",
    "风格参考图",
}


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _placeholder_key(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", " ", value.casefold()).strip()


def _local_identifiers(upload: dict[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    for key in ("source_path", "path"):
        raw = _normalized_text(upload.get(key))
        if not raw:
            continue
        identifiers.add(raw.casefold())
        name = Path(raw).name.strip()
        if name:
            identifiers.add(name.casefold())
    return identifiers


def _validate_semantic_value(
    *,
    index: int,
    field: str,
    value: Any,
    upload: dict[str, Any],
) -> str:
    text = _normalized_text(value)
    if not text:
        raise ValueError(f"Reference Image {index} is missing required field: {field}")
    key = _placeholder_key(text)
    placeholders = GENERIC_PLACEHOLDERS | {
        f"reference image {index}",
        f"image {index}",
        f"ref {index}",
        f"参考图 {index}",
        f"图片 {index}",
    }
    if key in placeholders or len(re.sub(r"\W+", "", text, flags=re.UNICODE)) < 4:
        raise ValueError(
            f"Reference Image {index} field {field} is a generic placeholder: {text!r}"
        )
    if any(identifier in text.casefold() for identifier in _local_identifiers(upload)):
        raise ValueError(
            f"Reference Image {index} field {field} must describe image semantics, not a local "
            f"path or filename: {text!r}"
        )
    return text


def compile_model_reference_map(model_reference_map: list[dict[str, Any]]) -> str:
    """Compile semantic entries without leaking uploader-side identifiers."""
    if not model_reference_map:
        return ""
    lines = [
        "REFERENCE IMAGE MAP",
        "Numbering follows the attachment order exactly. Apply each image only as defined below.",
    ]
    for entry in model_reference_map:
        index = int(entry["index"])
        lines.append("")
        lines.append(f"Reference Image {index}")
        for field in MODEL_REFERENCE_FIELDS:
            lines.append(f"- {FIELD_LABELS[field]}: {_normalized_text(entry[field])}")
    lines.extend(
        [
            "",
            "For each image, use only its Use field and obey its Ignore field. These written "
            "instructions override conflicting text, numbers, captions, layouts, clothing, or "
            "relationships visible inside the reference images.",
        ]
    )
    return "\n".join(lines)


def validate_reference_contract(
    *,
    reference_images: list[dict[str, Any]],
    model_reference_map: list[dict[str, Any]],
    ordered_upload_paths: list[str],
    compiled_model_reference_map: str,
) -> None:
    """Fail closed when upload order and model semantics cannot be proven equivalent."""
    if not reference_images:
        if model_reference_map or ordered_upload_paths or compiled_model_reference_map.strip():
            raise ValueError("Reference metadata exists, but no reference images were declared")
        return

    expected_indices = list(range(1, len(reference_images) + 1))
    upload_indices = [int(entry.get("index") or 0) for entry in reference_images]
    if upload_indices != expected_indices:
        raise ValueError(
            "reference image numbering must match upload-array order and be consecutive from 1; "
            f"got {upload_indices}"
        )
    model_indices = [int(entry.get("index") or 0) for entry in model_reference_map]
    if model_indices != expected_indices:
        raise ValueError(
            "model reference map must contain exactly one ordered entry for every upload; "
            f"expected {expected_indices}, got {model_indices}"
        )

    expected_paths = [_normalized_text(entry.get("path")) for entry in reference_images]
    if any(not path for path in expected_paths):
        raise ValueError("every reference image requires an uploader-side path")
    normalized_ordered_paths = [_normalized_text(path) for path in ordered_upload_paths]
    if normalized_ordered_paths != expected_paths:
        raise ValueError(
            "ordered_upload_paths must exactly match reference_images array order; "
            f"expected {expected_paths}, got {normalized_ordered_paths}"
        )

    canonical_model_map: list[dict[str, Any]] = []
    for upload, entry in zip(reference_images, model_reference_map, strict=True):
        index = int(entry["index"])
        canonical: dict[str, Any] = {"index": index}
        missing = [field for field in MODEL_REFERENCE_FIELDS if not _normalized_text(entry.get(field))]
        if missing:
            raise ValueError(
                f"Reference Image {index} is missing required model-facing fields: "
                + ", ".join(missing)
            )
        for field in MODEL_REFERENCE_FIELDS:
            canonical[field] = _validate_semantic_value(
                index=index,
                field=field,
                value=entry.get(field),
                upload=upload,
            )
        if not POSITION_MARKER_RE.search(canonical["spatial_map"]):
            raise ValueError(
                f"Reference Image {index} spatial_map must explicitly identify the whole image, "
                "a single subject, or positional regions such as left/center/right or top/bottom"
            )
        canonical_model_map.append(canonical)

    expected_compiled = compile_model_reference_map(canonical_model_map)
    if compiled_model_reference_map.strip() != expected_compiled:
        raise ValueError(
            "compiled_model_reference_map is missing or stale; re-run prepare_job.py before "
            "submitting the job"
        )


def validate_model_message(
    *,
    message: str,
    reference_images: list[dict[str, Any]],
    compiled_model_reference_map: str,
) -> None:
    """Ensure the submitted message contains the semantic map and no local identifiers."""
    if not reference_images:
        return
    if not compiled_model_reference_map or compiled_model_reference_map not in message:
        raise ValueError(
            "model-facing prompt does not contain the compiled Reference Image 1..N map"
        )
    folded_message = message.casefold()
    for upload in reference_images:
        for identifier in _local_identifiers(upload):
            if identifier and identifier in folded_message:
                raise ValueError(
                    "model-facing prompt contains a local reference path or filename; local "
                    "identifiers are allowed only in uploader/job metadata"
                )


def reference_audit_fields(job: dict[str, Any]) -> dict[str, Any]:
    """Return separated uploader-side and model-side evidence for reports and sessions."""
    uploads = [entry for entry in job.get("reference_images", []) if isinstance(entry, dict)]
    ordered_paths = [str(path) for path in job.get("ordered_upload_paths", [])]
    if not ordered_paths and uploads:
        ordered_paths = [str(entry.get("path") or "") for entry in uploads]
    return {
        "reference_upload_order": [
            {"index": int(entry.get("index") or 0), "path": str(entry.get("path") or "")}
            for entry in uploads
        ],
        "ordered_upload_paths": ordered_paths,
        "model_reference_map": [
            dict(entry)
            for entry in job.get("model_reference_map", [])
            if isinstance(entry, dict)
        ],
        "compiled_model_reference_map": str(job.get("compiled_model_reference_map") or ""),
    }
