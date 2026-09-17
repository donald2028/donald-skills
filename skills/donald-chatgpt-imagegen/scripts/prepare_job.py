#!/usr/bin/env python3
"""Prepare a reusable ChatGPT Web image-generation job manifest."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from output_paths import resolve_tool_output_root
from reference_contract import (
    MODEL_REFERENCE_FIELDS,
    compile_model_reference_map,
    validate_reference_contract,
)


SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
REFERENCE_RE = re.compile(r"^\s*(\d+)\.\s+`([^`]+)`\s*$", re.MULTILINE)
REFERENCE_FIELD_RE = re.compile(
    r"^\s*[-*]\s+(?:(?:Reference Image\s+(\d+)\s+)?"
    r"(Model Role|Role|Represents|Spatial Map|Use|Ignore))\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
LEGACY_REFERENCE_ROLE_RE = re.compile(
    r"^\s*[-*]\s+Reference Image\s+(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE
)
ASPECT_RATIO_RE = re.compile(r"^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$")
REFERENCE_FIELD_ALIASES = {
    "model role": "model_role",
    "role": "model_role",
    "represents": "model_role",
    "spatial map": "spatial_map",
    "use": "use",
    "ignore": "ignore",
}


def safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return safe.strip("-") or "job"


def sections(markdown: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(markdown))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        result[match.group(1).strip()] = markdown[start:end].strip()
    return result


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    return text[end + 5 :] if end >= 0 else text


def strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


def prompt_from_file(path: Path, parsed_sections: dict[str, str]) -> str:
    if "Prompt" in parsed_sections:
        prompt = strip_fence(parsed_sections["Prompt"])
    elif parsed_sections:
        raise ValueError("Markdown cards with sections must include a ## Prompt section")
    else:
        prompt = strip_frontmatter(path.read_text(encoding="utf-8")).strip()
    if not prompt:
        raise ValueError("prompt is empty")
    return prompt


def resolve_reference(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _reference_semantics_from_section(reference_section: str) -> dict[int, dict[str, str]]:
    semantics: dict[int, dict[str, str]] = {}
    current_index: int | None = None
    for line in reference_section.splitlines():
        declared = REFERENCE_RE.fullmatch(line)
        if declared:
            current_index = int(declared.group(1))
            continue
        field = REFERENCE_FIELD_RE.fullmatch(line)
        if field:
            explicit_index, raw_field, value = field.groups()
            index = int(explicit_index) if explicit_index else current_index
            if index is None:
                raise ValueError(f"Reference metadata appears before a numbered image: {line.strip()}")
            field_name = REFERENCE_FIELD_ALIASES[raw_field.casefold()]
            entry = semantics.setdefault(index, {})
            if field_name in entry:
                raise ValueError(f"Reference Image {index} repeats field {field_name}")
            entry[field_name] = value.strip()
            continue
        legacy_role = LEGACY_REFERENCE_ROLE_RE.fullmatch(line)
        if legacy_role:
            index = int(legacy_role.group(1))
            entry = semantics.setdefault(index, {})
            if "model_role" in entry:
                raise ValueError(f"Reference Image {index} repeats field model_role")
            entry["model_role"] = legacy_role.group(2).strip()
    return semantics


def reference_entries(
    *,
    parsed_sections: dict[str, str],
    base_dir: Path,
    cli_references: list[str],
    cli_reference_roles: list[str],
    cli_reference_spatial_maps: list[str],
    cli_reference_uses: list[str],
    cli_reference_ignores: list[str],
) -> list[dict[str, Any]]:
    reference_section = parsed_sections.get("Required Reference Images", "")
    declared = [(int(index), value) for index, value in REFERENCE_RE.findall(reference_section)]
    semantics = _reference_semantics_from_section(reference_section)
    cli_semantics = {
        "model_role": cli_reference_roles,
        "spatial_map": cli_reference_spatial_maps,
        "use": cli_reference_uses,
        "ignore": cli_reference_ignores,
    }
    cli_semantic_options = {
        "model_role": "--reference-role",
        "spatial_map": "--reference-spatial-map",
        "use": "--reference-use",
        "ignore": "--reference-ignore",
    }
    if declared and cli_references:
        raise ValueError("Use either ## Required Reference Images or --reference, not both")
    if declared and any(cli_semantics.values()):
        raise ValueError("Keep reference semantics in the prompt card when paths are declared there")
    if cli_references:
        declared = list(enumerate(cli_references, start=1))
        for field, values in cli_semantics.items():
            if len(values) != len(cli_references):
                raise ValueError(
                    f"{cli_semantic_options[field]} must be repeated once per --reference "
                    f"in the same order; expected {len(cli_references)}, got {len(values)}"
                )
        semantics = {
            index: {field: values[index - 1].strip() for field, values in cli_semantics.items()}
            for index in range(1, len(cli_references) + 1)
        }
    elif any(cli_semantics.values()):
        raise ValueError("Reference semantic flags require at least one --reference")

    references: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for index, source_path in declared:
        if index in seen_indices:
            raise ValueError(f"reference image numbering contains duplicate index {index}")
        seen_indices.add(index)
        path = resolve_reference(base_dir, source_path)
        if not path.is_file():
            raise FileNotFoundError(f"reference image not found: {path}")
        references.append(
            {
                "index": index,
                "source_path": source_path,
                "path": str(path),
                **semantics.get(index, {}),
            }
        )
    expected = list(range(1, len(references) + 1))
    actual = [item["index"] for item in references]
    if actual != expected:
        raise ValueError(
            "reference image numbering must follow declaration/upload order and be consecutive "
            f"from 1; got {actual}"
        )
    undeclared_semantics = sorted(set(semantics) - set(actual))
    if undeclared_semantics:
        raise ValueError(
            "Reference Map contains entries without matching upload paths: "
            f"{undeclared_semantics}"
        )
    return references


def split_reference_contract(
    references: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], str]:
    uploads = [
        {
            "index": int(reference["index"]),
            "source_path": str(reference["source_path"]),
            "path": str(reference["path"]),
        }
        for reference in references
    ]
    model_map = [
        {
            "index": int(reference["index"]),
            **{field: str(reference.get(field) or "").strip() for field in MODEL_REFERENCE_FIELDS},
        }
        for reference in references
    ]
    ordered_upload_paths = [reference["path"] for reference in uploads]
    compiled_map = compile_model_reference_map(model_map) if references else ""
    validate_reference_contract(
        reference_images=uploads,
        model_reference_map=model_map,
        ordered_upload_paths=ordered_upload_paths,
        compiled_model_reference_map=compiled_map,
    )
    return uploads, model_map, ordered_upload_paths, compiled_map


def validate_aspect_ratio(value: str) -> str:
    normalized = value.strip()
    match = ASPECT_RATIO_RE.fullmatch(normalized)
    if not match:
        raise ValueError("aspect ratio must use WIDTH:HEIGHT with positive numbers, for example 16:9")
    width, height = (float(part) for part in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError("aspect ratio dimensions must be greater than zero")
    return normalized


def build_message(
    *,
    prompt: str,
    compiled_model_reference_map: str,
    count: int,
    aspect_ratio: str,
    variant_label: str = "",
) -> str:
    lines = [compiled_model_reference_map] if compiled_model_reference_map else []
    if compiled_model_reference_map:
        lines.append("")
    if count == 1:
        lines.append(f"Generate exactly one image with aspect ratio {aspect_ratio}.")
    else:
        lines.append(
            f"Generate exactly {count} separate image results with aspect ratio {aspect_ratio}."
        )
    if variant_label:
        lines.append(f"Variant direction: {variant_label}")
    lines.extend(["", "PROMPT:", prompt])
    return "\n".join(lines).strip() + "\n"


def build_job(
    *,
    prompt_path: Path,
    output_dir: Path,
    variant_count: int,
    request_mode: str,
    aspect_ratio: str,
    reference_base_dir: Path | None,
    cli_references: list[str],
    variant_notes: list[str],
    reuse_conversation_references: bool,
    cli_reference_roles: list[str] | None = None,
    cli_reference_spatial_maps: list[str] | None = None,
    cli_reference_uses: list[str] | None = None,
    cli_reference_ignores: list[str] | None = None,
) -> dict[str, Any]:
    if variant_count < 1:
        raise ValueError("variants must be at least 1")
    if request_mode == "single_batch" and variant_notes:
        raise ValueError("--variant-note requires --request-mode independent_variants")
    if len(variant_notes) > variant_count:
        raise ValueError("variant note count cannot exceed variants")
    if reuse_conversation_references and request_mode != "single_batch":
        raise ValueError("--reuse-conversation-references requires single_batch")
    aspect_ratio = validate_aspect_ratio(aspect_ratio)

    prompt_path = prompt_path.expanduser().resolve()
    markdown = prompt_path.read_text(encoding="utf-8")
    parsed_sections = sections(markdown)
    prompt = prompt_from_file(prompt_path, parsed_sections)
    base_dir = (reference_base_dir or prompt_path.parent).expanduser().resolve()
    references = reference_entries(
        parsed_sections=parsed_sections,
        base_dir=base_dir,
        cli_references=cli_references,
        cli_reference_roles=cli_reference_roles or [],
        cli_reference_spatial_maps=cli_reference_spatial_maps or [],
        cli_reference_uses=cli_reference_uses or [],
        cli_reference_ignores=cli_reference_ignores or [],
    )
    uploads, model_reference_map, ordered_upload_paths, compiled_model_reference_map = (
        split_reference_contract(references)
    )
    output_dir = output_dir.expanduser().resolve()
    messages = []
    for index in range(1, variant_count + 1):
        note = variant_notes[index - 1] if index <= len(variant_notes) else ""
        messages.append(
            {
                "variant_index": index,
                "message": build_message(
                    prompt=prompt,
                    compiled_model_reference_map=compiled_model_reference_map,
                    count=1,
                    aspect_ratio=aspect_ratio,
                    variant_label=note,
                ),
            }
        )
    batch_message = build_message(
        prompt=prompt,
        compiled_model_reference_map=compiled_model_reference_map,
        count=variant_count,
        aspect_ratio=aspect_ratio,
    )
    job_name = prompt_path.stem.removesuffix(".prompt")
    return {
        "schema_version": 2,
        "adapter": "chatgpt_web_agent_browser",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "job_name": job_name,
        "prompt_card": str(prompt_path),
        "reference_base_dir": str(base_dir),
        "download_dir": str(output_dir),
        "chatgpt_session_path": str(output_dir / "chatgpt_session.json"),
        "chatgpt_variant_session_path_pattern": str(
            output_dir / "variant_{NN}_chatgpt_session.json"
        ),
        "suggested_conversation_title": f"Image generation | {job_name}",
        "reference_images": uploads,
        "ordered_upload_paths": ordered_upload_paths,
        "model_reference_map": model_reference_map,
        "compiled_model_reference_map": compiled_model_reference_map,
        "reuse_conversation_references": reuse_conversation_references,
        "image_generation_mode": "create_image",
        "variant_count": variant_count,
        "request_mode": request_mode,
        "output_aspect_ratio": aspect_ratio,
        "prompt": prompt,
        "chatgpt_messages": messages,
        "chatgpt_batch_message": batch_message,
        "chatgpt_message": batch_message if request_mode == "single_batch" else messages[0]["message"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("prompt_file", type=Path, help="Plain text prompt or Markdown prompt card")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Exact ChatGPT image job root. Defaults to the Donald Skills Documents data directory.",
    )
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument(
        "--request-mode",
        choices=["single_batch", "independent_variants"],
        default="single_batch",
    )
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="Reference image path; repeat in upload order",
    )
    parser.add_argument(
        "--reference-role",
        action="append",
        default=[],
        help="What each reference represents; repeat once per --reference in the same order",
    )
    parser.add_argument(
        "--reference-spatial-map",
        action="append",
        default=[],
        help="Whole-image or positional subject/region mapping; repeat once per --reference",
    )
    parser.add_argument(
        "--reference-use",
        action="append",
        default=[],
        help="Allowed visual evidence; repeat once per --reference",
    )
    parser.add_argument(
        "--reference-ignore",
        action="append",
        default=[],
        help="Visual evidence the model must ignore; repeat once per --reference",
    )
    parser.add_argument("--reference-base-dir", type=Path)
    parser.add_argument("--variant-note", action="append", default=[])
    parser.add_argument("--reuse-conversation-references", action="store_true")
    args = parser.parse_args()

    prompt_path = args.prompt_file.expanduser().resolve()
    job_name = prompt_path.stem.removesuffix(".prompt")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    job_dir = (
        resolve_tool_output_root("chatgpt-images", args.output_root)
        / safe_path_part(job_name)
        / timestamp
    )
    job = build_job(
        prompt_path=prompt_path,
        output_dir=job_dir,
        variant_count=args.variants,
        request_mode=args.request_mode,
        aspect_ratio=args.aspect_ratio,
        reference_base_dir=args.reference_base_dir,
        cli_references=args.reference,
        variant_notes=args.variant_note,
        reuse_conversation_references=args.reuse_conversation_references,
        cli_reference_roles=args.reference_role,
        cli_reference_spatial_maps=args.reference_spatial_map,
        cli_reference_uses=args.reference_use,
        cli_reference_ignores=args.reference_ignore,
    )
    output = job_dir / "chatgpt-job.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    Path(job["download_dir"]).mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"job_manifest": str(output), **job}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
