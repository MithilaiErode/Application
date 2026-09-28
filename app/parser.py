"""Turn uploaded Classic customisation exports into individual customisation records.

Classic export formats differ between Epicor versions and between export methods
(Customization Maintenance XML, Solution Workbench packages, SQL extracts of Ice.XXXDef),
so the parser is deliberately tolerant: it walks every XML node, decodes embedded XML /
base64 / gzip content, and picks out the C# script by content rather than by tag name.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import xml.etree.ElementTree as ET

CSHARP_MARKERS = (
    "public class Script",
    "InitializeCustomCode",
    "DestroyCustomCode",
    "EpiDataView",
    "oTrans",
    "using Ice.",
    "using Erp.",
)
NAME_TAGS = ("key1", "customizationname", "custname", "name")
FORM_TAGS = ("key2", "formname", "form")
META_TAGS = NAME_TAGS + FORM_TAGS + (
    "key3", "typecode", "productid", "description", "company", "layername", "parentname",
)
TEXT_EXTENSIONS = (".cs", ".txt")
MAX_UI_CONTEXT_CHARS = 60_000
_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


@dataclass
class Customisation:
    id: str
    name: str
    form: str
    source_file: str
    script: str = ""
    ui_context: str = ""
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _looks_like_csharp(text: str) -> bool:
    return sum(marker in text for marker in CSHARP_MARKERS) >= 2


def _decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _try_decode_blob(text: str) -> str | None:
    """Decode base64 (optionally gzip/zlib compressed) content into text."""
    stripped = text.strip()
    if len(stripped) < 200 or not _B64_RE.match(stripped):
        return None
    try:
        raw = base64.b64decode(stripped, validate=False)
    except (binascii.Error, ValueError):
        return None
    for decompress in (gzip.decompress, zlib.decompress, lambda b: b):
        try:
            out = decompress(raw)
        except Exception:
            continue
        decoded = _decode_bytes(out)
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in decoded[:2000])
        if decoded and printable / max(1, len(decoded[:2000])) > 0.95:
            return decoded
    return None


def _collect(text: str, scripts: list[str], xml_fragments: list[str], depth: int = 0) -> None:
    """Classify one text value: C# script, embedded XML, or encoded blob."""
    if not text or depth > 4:
        return
    stripped = text.lstrip()
    # Embedded XML first: a customisation XML wraps the script, so it also "looks like" C#.
    if stripped.startswith("<"):
        try:
            root = ET.fromstring(stripped)
        except ET.ParseError:
            root = None
        if root is not None:
            xml_fragments.append(stripped)
            _walk(root, scripts, xml_fragments, {}, depth + 1)
            return
    if _looks_like_csharp(text):
        scripts.append(text)
        return
    decoded = _try_decode_blob(text)
    if decoded:
        _collect(decoded, scripts, xml_fragments, depth + 1)


def _walk(elem: ET.Element, scripts: list[str], xml_fragments: list[str], meta: dict, depth: int) -> None:
    for node in elem.iter():
        tag = _local(node.tag)
        if node.text and node.text.strip():
            value = node.text.strip()
            if tag in META_TAGS and len(value) < 300 and tag not in meta:
                meta[tag] = value
            else:
                _collect(node.text, scripts, xml_fragments, depth)
        for attr, value in node.attrib.items():
            if _local(attr) in META_TAGS and len(value) < 300:
                meta.setdefault(_local(attr), value)
            elif len(value) > 200:
                _collect(value, scripts, xml_fragments, depth)


def _strip_scripts(xml_text: str, scripts: list[str]) -> str:
    for script in scripts:
        xml_text = xml_text.replace(script, "[C# SCRIPT EXTRACTED SEPARATELY]")
    return xml_text


def _make_record(source: str, index: int, meta: dict, scripts: list[str], ui_xml: str) -> Customisation:
    name = next((meta[t] for t in NAME_TAGS if meta.get(t)), None) or PurePosixPath(source).stem
    form = next((meta[t] for t in FORM_TAGS if meta.get(t)), "")
    script = max(scripts, key=len) if scripts else ""
    warnings = []
    if not scripts:
        warnings.append("No C# script found; analysis is based on the UI/property XML only.")
    elif len(scripts) > 1:
        extra = [s for s in scripts if s is not script]
        script += "".join(f"\n\n// ---- Additional script block {i + 2} ----\n{s}" for i, s in enumerate(extra))
    ui_context = _strip_scripts(ui_xml, scripts)
    if len(ui_context) > MAX_UI_CONTEXT_CHARS:
        warnings.append(
            f"UI/property XML is {len(ui_context):,} characters; only the first "
            f"{MAX_UI_CONTEXT_CHARS:,} were sent for analysis. Review UI changes manually."
        )
        ui_context = ui_context[:MAX_UI_CONTEXT_CHARS]
    digest = hashlib.sha1(f"{source}|{index}|{name}|{form}".encode()).hexdigest()[:10]
    return Customisation(
        id=digest, name=name, form=form, source_file=source, script=script,
        ui_context=ui_context, metadata=meta, warnings=warnings,
    )


def _parse_xml(source: str, text: str) -> list[Customisation]:
    try:
        root = ET.fromstring(text.lstrip("﻿").lstrip())
    except ET.ParseError as exc:
        if _looks_like_csharp(text):
            return [_make_record(source, 0, {}, [text], "")]
        raise ValueError(f"{source}: not valid XML ({exc})") from exc

    # Several customisations in one file (e.g. an XXXDef table extract): one record per row.
    rows = [el for el in root.iter() if any(_local(child.tag) == "key1" for child in el)]
    if len(rows) <= 1:
        rows = [root]

    records = []
    for i, row in enumerate(rows):
        scripts: list[str] = []
        fragments: list[str] = []
        meta: dict = {}
        _walk(row, scripts, fragments, meta, 0)
        typecode = meta.get("typecode", "").lower()
        if len(rows) > 1 and typecode and "custom" not in typecode and not scripts:
            continue  # skip personalisations / non-customisation rows in a table extract
        ui_xml = max(fragments, key=len) if fragments else ET.tostring(row, encoding="unicode")
        records.append(_make_record(source, i, meta, scripts, ui_xml))
    return records


def parse_upload(filename: str, data: bytes) -> list[Customisation]:
    """Parse one uploaded file (xml, cs, txt or zip) into customisation records."""
    lower = filename.lower()
    if lower.endswith(".zip") or data[:2] == b"PK":
        records: list[Customisation] = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename.startswith("__MACOSX"):
                    continue
                inner = f"{filename}/{info.filename}"
                if info.filename.lower().endswith((".xml", ".zip") + TEXT_EXTENSIONS):
                    records.extend(parse_upload(inner, zf.read(info)))
        return records

    text = _decode_bytes(data)
    if lower.endswith(TEXT_EXTENSIONS):
        return [_make_record(filename, 0, {}, [text], "")]
    return _parse_xml(filename, text)


def read_reference_sample(filename: str, data: bytes, limit: int = 25_000) -> tuple[str, str]:
    """Read a Kinetic sample export (layer / BPM / function) used as a format reference."""
    if filename.lower().endswith(".zip") or data[:2] == b"PK":
        parts = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    parts.append(f"--- {info.filename} ---\n{_decode_bytes(zf.read(info))}")
        text = "\n".join(parts)
    else:
        text = _decode_bytes(data)
    if len(text) > limit:
        text = text[:limit] + f"\n[... sample truncated at {limit:,} characters ...]"
    return filename, text
