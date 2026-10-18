import csv
import io
from datetime import datetime, timezone
from flask import make_response, Response


def build_csv_response(items: list[dict], export_fields: list[str] | None = None, filename_prefix: str = "export") -> Response:
    """Builds a CSV download Response from serialized dictionaries."""
    if not items:
        fieldnames = export_fields or ["id"]
    else:
        fieldnames = export_fields or list(items[0].keys())

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\r\n"
    )
    writer.writeheader()
    writer.writerows(items)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}.csv"

    return make_response(buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"'
    })
