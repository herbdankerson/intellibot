import json
import logging
import os
import time
import uuid
from typing import Dict, Tuple

import requests
from slugify import slugify

from src.config import (
    DOCLING_BASE_URL,
    DOCLING_POLL_INTERVAL,
    DOCLING_TIMEOUT,
)


logger = logging.getLogger(__name__)


class DoclingConversionError(RuntimeError):
    """Raised when Docling cannot convert a document."""


class DoclingTimeoutError(DoclingConversionError):
    """Raised when Docling conversion exceeds the configured timeout."""


def _extract_result(payload: Dict) -> Tuple[str, Dict]:
    document = payload.get("document") or {}
    markdown = document.get("md_content")
    status = payload.get("status")
    errors = payload.get("errors") or []

    if status not in {"success", "partial_success"}:
        raise DoclingConversionError(
            f"Docling conversion returned status '{status}' with errors {errors}"
        )

    if not markdown:
        raise DoclingConversionError("Docling response did not contain markdown content")

    if errors:
        logger.warning("Docling conversion completed with errors: %s", errors)

    return markdown, {
        "docling_status": status,
        "docling_filename": document.get("filename"),
        "docling_errors": errors,
    }


def _convert_sync(pdf_path: str) -> Tuple[str, Dict]:
    url = f"{DOCLING_BASE_URL.rstrip('/')}/v1/convert/file"
    headers = {"Accept": "application/json"}

    try:
        with open(pdf_path, "rb") as file_handle:
            files = [("files", (os.path.basename(pdf_path), file_handle, "application/pdf"))]
            data = [("to_formats", "md")]
            response = requests.post(
                url,
                files=files,
                data=data,
                headers=headers,
                timeout=DOCLING_TIMEOUT,
            )
    except requests.RequestException as exc:
        raise DoclingConversionError(f"Docling request failed: {exc}") from exc

    if response.status_code == 504:
        raise DoclingTimeoutError(
            "Docling synchronous conversion exceeded the maximum wait time"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise DoclingConversionError(
            f"Docling request failed with status {response.status_code}: {response.text}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise DoclingConversionError("Docling response was not valid JSON") from exc

    return _extract_result(payload)


def _poll_async_task(task_id: str) -> Dict:
    status_url = f"{DOCLING_BASE_URL.rstrip('/')}/v1/status/poll/{task_id}"
    result_url = f"{DOCLING_BASE_URL.rstrip('/')}/v1/result/{task_id}"
    deadline = time.monotonic() + DOCLING_TIMEOUT

    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise DoclingTimeoutError("Docling async conversion timed out")

        wait = min(DOCLING_POLL_INTERVAL, remaining)

        try:
            status_response = requests.get(
                status_url,
                params={"wait": wait},
                timeout=wait + 10,
            )
        except requests.RequestException as exc:
            raise DoclingConversionError(f"Docling status polling failed: {exc}") from exc

        try:
            status_response.raise_for_status()
        except requests.HTTPError as exc:
            raise DoclingConversionError(
                f"Docling status polling failed with {status_response.status_code}: {status_response.text}"
            ) from exc

        try:
            status_payload = status_response.json()
        except ValueError as exc:
            raise DoclingConversionError("Docling status response was not valid JSON") from exc

        task_status = status_payload.get("task_status")
        if task_status in {"success", "partial_success"}:
            break
        if task_status in {"failure", "skipped"}:
            raise DoclingConversionError(
                f"Docling async conversion failed with status '{task_status}'"
            )

        # Loop again to check progress until timeout

    try:
        result_response = requests.get(result_url, timeout=DOCLING_TIMEOUT)
        result_response.raise_for_status()
    except requests.RequestException as exc:
        raise DoclingConversionError(f"Docling result fetch failed: {exc}") from exc
    except requests.HTTPError as exc:
        raise DoclingConversionError(
            f"Docling result fetch failed with {result_response.status_code}: {result_response.text}"
        ) from exc

    try:
        return result_response.json()
    except ValueError as exc:
        raise DoclingConversionError("Docling result response was not valid JSON") from exc


def _convert_async(pdf_path: str) -> Tuple[str, Dict]:
    url = f"{DOCLING_BASE_URL.rstrip('/')}/v1/convert/file/async"

    try:
        with open(pdf_path, "rb") as file_handle:
            files = [("files", (os.path.basename(pdf_path), file_handle, "application/pdf"))]
            data = [("to_formats", "md")]
            response = requests.post(
                url,
                files=files,
                data=data,
                timeout=DOCLING_TIMEOUT,
            )
    except requests.RequestException as exc:
        raise DoclingConversionError(f"Docling async request failed: {exc}") from exc

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise DoclingConversionError(
            f"Docling async request failed with status {response.status_code}: {response.text}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise DoclingConversionError("Docling async submission response was not valid JSON") from exc

    task_id = payload.get("task_id")
    if not task_id:
        raise DoclingConversionError("Docling async response did not include a task_id")

    result_payload = _poll_async_task(task_id)
    return _extract_result(result_payload)


def parse_pdf(pdf_path: str, parsed_dir: str) -> Tuple[str, str, Dict]:
    """Convert a PDF to markdown + manifest using Docling."""
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    slug = slugify(base) or str(uuid.uuid4())
    out_md = os.path.join(parsed_dir, f"{slug}.md")
    out_meta = os.path.join(parsed_dir, f"{slug}.json")
    os.makedirs(parsed_dir, exist_ok=True)

    try:
        markdown, docling_meta = _convert_sync(pdf_path)
    except DoclingTimeoutError:
        logger.info("Docling synchronous conversion timed out, falling back to async")
        markdown, docling_meta = _convert_async(pdf_path)
    except DoclingConversionError as exc:
        raise RuntimeError(str(exc)) from exc

    with open(out_md, "w", encoding="utf-8") as handle:
        handle.write(markdown)

    meta = {
        "slug": slug,
        "source_file": os.path.basename(pdf_path),
        **docling_meta,
    }
    with open(out_meta, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)

    return slug, markdown, meta
