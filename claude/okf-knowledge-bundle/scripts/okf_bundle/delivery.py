"""Seal the actual review attachment and a small, counted delivery message."""
import json
from pathlib import Path
import shutil

from .common import digest
from .summary import check_summary


def seal_review(summary, preview, destination, link_prefix):
    summary, preview, target = Path(summary), Path(preview), Path(destination)
    checked = check_summary(summary)
    if not checked["valid"]:
        raise ValueError("Review must pass review-summary before sealing")
    if not preview.is_file() or preview.suffix != ".html":
        raise ValueError("Use the native review-preview content HTML")
    if target.exists():
        raise ValueError("Delivery folder already exists; do not overwrite a sealed review")
    if not isinstance(link_prefix, str) or not link_prefix or any(c in link_prefix for c in "\r\n()<> "):
        raise ValueError("Use a host-supported attachment URL prefix without spaces or Markdown delimiters")
    prefix = link_prefix.rstrip("/")
    message = (f"The [short review]({prefix}/analyst-review.md) and "
               f"[proposed content]({prefix}/proposed-content.html) are ready. Everything remains unpublished.\n")
    # Count both files together so moving the summary to an attachment cannot
    # evade the whole-review limit. The content preview remains separate.
    from .summary import check_text
    combined = check_text(checked["text"] + "\n" + message)
    if not combined["valid"]:
        raise ValueError("Review plus delivery note exceeds 150 words; shorten the review before sealing")
    raw = summary.read_bytes()
    if digest(raw) != checked["sha256"]:
        raise ValueError("Review changed during sealing")
    target.mkdir(parents=True)
    (target / "analyst-review.md").write_bytes(raw)
    shutil.copyfile(preview, target / "proposed-content.html")
    (target / "delivery.md").write_text(message, encoding="utf-8", newline="\n")
    receipt = {"contract": "okf-review-delivery/v1", "word_count": combined["word_count"], "limit": 150,
               "files": {name: digest((target / name).read_bytes()) for name in
                         ("analyst-review.md", "proposed-content.html", "delivery.md")}}
    (target / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return {"valid": True, "word_count": combined["word_count"], "folder": str(target),
            "delivery_text": message, "instruction": "Attach the two sealed files unchanged. Send only delivery_text; do not rewrite or summarize the review in chat. This is not approval."}


def verify_delivery(folder, message_file=None):
    root = Path(folder)
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    expected = {"analyst-review.md", "proposed-content.html", "delivery.md"}
    if receipt.get("contract") != "okf-review-delivery/v1" or set(receipt.get("files", {})) != expected:
        raise ValueError("Malformed delivery receipt")
    unchanged = all(digest((root / name).read_bytes()) == receipt["files"][name] for name in expected)
    from .summary import check_text
    result = check_text((root / "analyst-review.md").read_text(encoding="utf-8-sig") + "\n" +
                        (root / "delivery.md").read_text(encoding="utf-8"))
    message_matches = message_file is None or Path(message_file).read_text(encoding="utf-8-sig").strip() == (root / "delivery.md").read_text(encoding="utf-8").strip()
    return {"valid": unchanged and result["valid"] and message_matches and receipt["word_count"] == result["word_count"],
            "files_unchanged": unchanged, "message_checked": message_file is not None,
            "message_matches": message_matches, "word_count": result["word_count"], "limit": 150}
