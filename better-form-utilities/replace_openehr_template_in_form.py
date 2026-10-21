#!/usr/bin/env python3
"""Update templateId and form name in a Better EHR Studio form package zip, then re-pack.

Better exports a nested package:

  outer.zip
  ├── package-manifest.json
  ├── <form>.zip          # flat form files (manifest.json, templates, …)
  ├── <template>_TEMPLATE.opt
  └── proxy_VARIABLE.json  (optional)

This script reads that outer zip, swaps in a new OPT file, rewrites template IDs /
form names inside the form, rebuilds the nested form zip (flat entries, no parent
folder), refreshes SHA-256 hashes in package-manifest.json, and writes a new
outer package zip.

Example:

  python3 ./replace_openehr_template_in_form.py \\
    TEST_update_templateID_MDK_Lungcancer_Oscar_2_2_27_FORM.zip \\
    --new_opt Diagnostic_MDT_Lung_cancer_TEMPLATE.opt \\
    --new_template_id Diagnostic_MDT_Lung_cancer.v2 \\
    --new_form_name TEST_update_templateID_MDK_Lungcancer_Oscar_2_2_27_FORM_UPDATED \\
    --new_form_display_name "TEST update templateID MDK Lungcancer Oscar UPDATED"
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


FORM_DESCRIPTION_PREFIX = "form-description"
TEMPLATE_ID_JSON = re.compile(r'"templateId"\s*:\s*"(?P<value>[^"]*)"')
# Whitespace (including newlines) allowed between tags and around the value text.
TEMPLATE_ID_OPT = re.compile(
    r"(<template_id>\s*<value>\s*)(?P<value>[^<]*?)(\s*</value>\s*</template_id>)"
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_package_manifest(outer: zipfile.ZipFile) -> dict:
    try:
        raw = outer.read("package-manifest.json")
    except KeyError as exc:
        raise SystemExit(
            "Input is not a Better package zip: missing package-manifest.json at the zip root."
        ) from exc
    return json.loads(raw)


def item_by_type(manifest: dict, item_type: str) -> dict:
    for item in manifest.get("items", []):
        if item.get("type") == item_type:
            return item
    raise SystemExit(f"package-manifest.json has no item with type={item_type!r}")


def replace_template_id_json(text: str, old_id: str, new_id: str) -> tuple[str, int]:
    """Replace only "templateId":"<old>" values (exact match)."""

    def _sub(match: re.Match[str]) -> str:
        if match.group("value") == old_id:
            return f'"templateId":"{new_id}"'
        return match.group(0)

    new_text, count = TEMPLATE_ID_JSON.subn(_sub, text)
    return new_text, count


def replace_template_id_opt(opt_text: str, old_id: str, new_id: str) -> tuple[str, int]:
    """Replace the value inside <template_id>…<value>…</value>…</template_id>.

    Accepts whitespace/newlines between the tags and around the value text; only the
    value text is changed. Surrounding markup and whitespace are left as-is.

    If the OPT already uses new_id, leave it unchanged and return the existing count.
    """

    values = [m.group("value") for m in TEMPLATE_ID_OPT.finditer(opt_text)]
    already = sum(1 for v in values if v == new_id)
    old_count = sum(1 for v in values if v == old_id)
    if already and old_id != new_id and old_count == 0:
        return opt_text, already
    if old_count == 0:
        raise SystemExit(
            f"OPT has no <template_id>…<value>{old_id}</value>…</template_id> "
            f"and no <template_id>…<value>{new_id}</value>…</template_id>"
        )

    def _sub(match: re.Match[str]) -> str:
        if match.group("value") == old_id:
            return f"{match.group(1)}{new_id}{match.group(3)}"
        return match.group(0)

    new_text, _ = TEMPLATE_ID_OPT.subn(_sub, opt_text)
    return new_text, old_count


def load_opt_bytes(opt_path: Path) -> tuple[str, bytes]:
    """Return (packaged filename, raw bytes) for a replacement OPT file."""
    if not opt_path.is_file():
        raise SystemExit(f"OPT file not found: {opt_path}")
    name = opt_path.name
    if not name.lower().endswith(".opt"):
        raise SystemExit(f"OPT path must be a .opt file, got: {name}")
    return name, opt_path.read_bytes()


def rewrite_form_members(
    members: dict[str, bytes],
    *,
    old_template_id: str,
    new_template_id: str,
    new_display_name: str | None,
) -> dict[str, bytes]:
    updated: dict[str, bytes] = {}
    template_id_hits = 0

    for name, data in members.items():
        # Binary-ish empty stubs are still utf-8 text in these packages.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            updated[name] = data
            continue

        changed = False

        if name == "manifest.json" or name == "templates" or name.startswith(
            FORM_DESCRIPTION_PREFIX
        ):
            text, n = replace_template_id_json(text, old_template_id, new_template_id)
            template_id_hits += n
            if n:
                changed = True

        if name == "manifest.json" and new_display_name is not None:
            manifest = json.loads(text)
            form = manifest.setdefault("form", {})
            if form.get("name") != new_display_name:
                form["name"] = new_display_name
                text = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
                changed = True

        updated[name] = text.encode("utf-8") if changed else data

    if template_id_hits == 0:
        raise SystemExit(
            f"No \"templateId\":\"{old_template_id}\" found in form files "
            f"(manifest.json / templates / form-description*)."
        )
    return updated


def preferred_form_order(names: Iterable[str]) -> list[str]:
    preferred = [
        "manifest.json",
        "templates",
        "summary-pages",
        "app-pages",
        "widget-configuration",
        "form-metadata",
        "form-environment",
        "form-description",
        "form-layout",
    ]
    name_set = set(names)
    ordered: list[str] = []
    for name in preferred:
        if name in name_set:
            ordered.append(name)
    # Keep page-specific resources together after the shared ones.
    rest = sorted(n for n in name_set if n not in ordered)
    return ordered + rest


def build_flat_zip(members: dict[str, bytes], order: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    names = order or preferred_form_order(members)
    now = datetime.now().timetuple()[:6]
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            if name not in members:
                continue
            if "/" in name or name.endswith("/"):
                raise SystemExit(f"Refusing to pack nested path into flat zip: {name!r}")
            info = zipfile.ZipInfo(filename=name, date_time=now)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, members[name])
    return buf.getvalue()


def load_inner_form_members(outer: zipfile.ZipFile, form_zip_name: str) -> dict[str, bytes]:
    try:
        inner_bytes = outer.read(form_zip_name)
    except KeyError as exc:
        raise SystemExit(
            f"package-manifest entryFile/fileName {form_zip_name!r} is missing from the package zip."
        ) from exc

    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
        members: dict[str, bytes] = {}
        for info in inner.infolist():
            if info.is_dir():
                continue
            # Normalise accidental "dirname/file" exports to basename.
            name = info.filename.split("/")[-1]
            if not name:
                continue
            members[name] = inner.read(info)
        if "manifest.json" not in members:
            raise SystemExit("Inner form zip has no manifest.json")
        return members


def detect_old_template_id(form_members: dict[str, bytes], template_item: dict) -> str:
    if template_item.get("id"):
        return template_item["id"]
    manifest = json.loads(form_members["manifest.json"])
    tid = manifest.get("form", {}).get("templateId")
    if not tid:
        raise SystemExit("Could not detect current templateId from form manifest.json")
    return tid


def update_package(
    input_zip: Path,
    *,
    new_opt_path: Path,
    new_template_id: str,
    new_form_name: str,
    new_display_name: str,
    output_zip: Path,
    old_template_id: str | None = None,
) -> None:
    if new_form_name.endswith(".zip"):
        new_form_name = new_form_name[: -len(".zip")]
    new_form_zip_name = f"{new_form_name}.zip"
    new_opt_name, new_opt_raw = load_opt_bytes(new_opt_path.resolve())

    with zipfile.ZipFile(input_zip) as outer:
        package_manifest = read_package_manifest(outer)
        form_item = item_by_type(package_manifest, "FORM")
        template_item = item_by_type(package_manifest, "TEMPLATE")

        old_form_zip_name = form_item.get("fileName") or package_manifest.get("entryFile")
        if not old_form_zip_name:
            raise SystemExit("FORM item has no fileName and package has no entryFile")

        old_opt_name = template_item.get("fileName")
        if not old_opt_name:
            raise SystemExit("TEMPLATE item has no fileName in package-manifest.json")

        form_members = load_inner_form_members(outer, old_form_zip_name)
        resolved_old_template_id = old_template_id or detect_old_template_id(
            form_members, template_item
        )

        form_members = rewrite_form_members(
            form_members,
            old_template_id=resolved_old_template_id,
            new_template_id=new_template_id,
            new_display_name=new_display_name,
        )
        # Preserve original inner entry order when possible.
        with zipfile.ZipFile(io.BytesIO(outer.read(old_form_zip_name))) as inner:
            original_order = [
                info.filename.split("/")[-1]
                for info in inner.infolist()
                if not info.is_dir() and info.filename.split("/")[-1]
            ]
        # Append any new/unexpected files.
        for name in preferred_form_order(form_members):
            if name not in original_order:
                original_order.append(name)
        form_zip_bytes = build_flat_zip(form_members, order=original_order)

        # Replacement OPT (from --new_opt), with template_id aligned to --new_template_id.
        try:
            opt_text = new_opt_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemExit(f"OPT file is not valid UTF-8 text: {new_opt_path}") from exc
        opt_text, opt_hits = replace_template_id_opt(
            opt_text, resolved_old_template_id, new_template_id
        )
        opt_bytes = opt_text.encode("utf-8")

        # Copy through any other root members (e.g. proxy_VARIABLE.json) unchanged,
        # except the old form zip / old opt / package-manifest which we rewrite.
        other_root: dict[str, bytes] = {}
        skip = {"package-manifest.json", old_form_zip_name, old_opt_name}
        for info in outer.infolist():
            if info.is_dir() or info.filename in skip or "/" in info.filename.rstrip("/"):
                continue
            other_root[info.filename] = outer.read(info)

    # Update package-manifest
    form_item["name"] = new_display_name
    form_item["fileName"] = new_form_zip_name
    form_item["hash"] = sha256_bytes(form_zip_bytes)

    template_item["name"] = new_template_id
    template_item["id"] = new_template_id
    template_item["fileName"] = new_opt_name
    template_item["hash"] = sha256_bytes(opt_bytes)

    for name, data in other_root.items():
        # Refresh hash on VARIABLE (and any other typed items) if listed.
        for item in package_manifest.get("items", []):
            if item.get("fileName") == name and "hash" in item:
                item["hash"] = sha256_bytes(data)

    package_manifest["name"] = new_form_zip_name
    package_manifest["entryFile"] = new_form_zip_name
    package_manifest["timestamp"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )

    pm_bytes = json.dumps(package_manifest, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )

    outer_members = {
        "package-manifest.json": pm_bytes,
        new_form_zip_name: form_zip_bytes,
        new_opt_name: opt_bytes,
        **other_root,
    }
    # Stable outer order: manifest, form zip, opt, then the rest alphabetically.
    outer_order = ["package-manifest.json", new_form_zip_name, new_opt_name] + sorted(
        n for n in other_root if n not in {new_form_zip_name, new_opt_name}
    )

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    output_zip.write_bytes(build_flat_zip(outer_members, order=outer_order))

    print(f"Wrote {output_zip}")
    print(f"  old templateId : {resolved_old_template_id}")
    print(f"  new templateId : {new_template_id}")
    print(f"  form zip name  : {new_form_zip_name}")
    print(f"  display name   : {new_display_name}")
    print(f"  OPT file       : {new_opt_path} -> {new_opt_name}")
    if old_opt_name != new_opt_name:
        print(f"  OPT renamed    : {old_opt_name} -> {new_opt_name}")
    print(f"  OPT template_id occurrences aligned: {opt_hits}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update templateId and form name in a Better EHR Studio form package zip "
            "and write a correctly nested re-packaged zip."
        )
    )
    parser.add_argument(
        "input_zip",
        type=Path,
        help="Original Better package zip (contains package-manifest.json + nested form zip)",
    )
    parser.add_argument(
        "--new_opt",
        required=True,
        type=Path,
        help="Path to the replacement OPT file to include in the package",
    )
    parser.add_argument(
        "--new_template_id",
        required=True,
        help="New template ID (e.g. Diagnostic_MDT_Lung_cancer.v2)",
    )
    parser.add_argument(
        "--new_form_name",
        required=True,
        help="New form package file stem (without .zip), used for nested + outer zip names",
    )
    parser.add_argument(
        "--new_form_display_name",
        required=True,
        help='New human-readable form name (e.g. "TEST update … UPDATED")',
    )
    parser.add_argument(
        "--old-template-id",
        default=None,
        help="Current template ID to replace (default: detect from package-manifest / form manifest)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output package zip path (default: <form-name>.zip next to the input)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_zip = args.input_zip.resolve()
    if not input_zip.is_file():
        raise SystemExit(f"Input zip not found: {input_zip}")

    form_stem = (
        args.new_form_name[:-4]
        if args.new_form_name.endswith(".zip")
        else args.new_form_name
    )
    output_zip = (
        args.output.resolve()
        if args.output
        else input_zip.with_name(f"{form_stem}.zip")
    )
    if output_zip.resolve() == input_zip.resolve():
        raise SystemExit(
            "Output path would overwrite the input zip; pass --output with a different path."
        )

    update_package(
        input_zip,
        new_opt_path=args.new_opt,
        new_template_id=args.new_template_id,
        new_form_name=form_stem,
        new_display_name=args.new_form_display_name,
        output_zip=output_zip,
        old_template_id=args.old_template_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
