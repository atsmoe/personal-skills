"""Pure payload adapters for the currently exposed Tencent connector schemas."""
import datetime as dt
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

FORBIDDEN = {"待确认", "未知", "可能是", "大概", "猜测值", "待核实"}


def number(value, money=False):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a purchase number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid purchase number") from error
    if not result.is_finite() or result < 0 or result > Decimal("1000000000000"):
        raise ValueError("Purchase number must be finite and nonnegative")
    if money and result != result.quantize(Decimal("0.01")):
        raise ValueError("Settlement amount has more than two decimal places")
    return format(result.quantize(Decimal("0.01")), "f") if money else format(result.normalize(), "f")


def text(value):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Invalid purchase text")
    value = value.strip()
    if any(word in value for word in FORBIDDEN):
        raise ValueError("Uncertain fields must be empty, not placeholders")
    return value


class TencentAdapter:
    @staticmethod
    def write_cell(sheet_type, value, field_type, timezone="Asia/Shanghai"):
        """Return a value payload; None/empty means omit, never silently clear."""
        if sheet_type not in {"sheet", "smartsheet"}:
            raise ValueError("Unsupported document type")
        if value is None or value == "":
            return None
        if field_type in {"money", "currency", "number"}:
            normalized = number(value, field_type in {"money", "currency"})
            numeric = float(normalized)
            if Decimal(str(numeric)) != Decimal(normalized):
                raise ValueError("Number loses precision in the connector payload")
            payload = {"number_value": numeric}
            return {"value_type": "NUMBER", **payload} if sheet_type == "sheet" else payload
        if field_type == "text":
            content = text(value)
            if not content:
                return None
            if sheet_type == "sheet":
                return {"value_type": "STRING", "string_value": content}
            return {"text_value": {"items": [{"text": content, "type": "text"}]}}
        if field_type in {"select", "singleSelect"} and sheet_type == "smartsheet":
            content = text(value)
            return {"option_value": {"items": [{"text": content}]}} if content else None
        if field_type == "dateTime" and sheet_type == "smartsheet":
            day = dt.date.fromisoformat(value)
            midnight = dt.datetime.combine(day, dt.time(), ZoneInfo(timezone))
            return {"string_value": str(int(midnight.timestamp()) * 1000)}
        if field_type == "image" and sheet_type == "smartsheet":
            if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
                raise ValueError("Actual image IDs required")
            return {"image_value": {"items": [{"image_id": x} for x in dict.fromkeys(value)]}}
        raise ValueError("Unsupported field type; SHEET images use insert_image separately")

    @staticmethod
    def locators(sheet_type, values, count):
        if not isinstance(values, list) or len(values) != count:
            raise ValueError("One actual locator required for each purchase item")
        valid = (all(isinstance(v, str) and v for v in values) if sheet_type == "smartsheet"
                 else sheet_type == "sheet" and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values))
        if not valid or len(set(values)) != len(values):
            raise ValueError("Use unique record IDs for SmartSheet or zero-based rows for SHEET")
        return values

    @staticmethod
    def merge_range(sheet_type, rows, column, allowed_range, current_merges, cells_blank):
        """Plan one SHEET photo merge; the executor still journals and reads it back."""
        if sheet_type != "sheet":
            raise ValueError("SmartSheet has no cell merge semantics")
        TencentAdapter.locators(sheet_type, rows, len(rows))
        if len(rows) < 2 or sorted(rows) != list(range(min(rows), max(rows) + 1)):
            raise ValueError("Merge must contain contiguous rows of one purchase group")
        keys = {"start_row", "end_row", "start_col", "end_col"}
        if set(allowed_range) != keys or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in allowed_range.values()):
            raise ValueError("Confirmed daily bounds required")
        area = {"start_row": min(rows), "end_row": max(rows), "start_col": column, "end_col": column}
        if not isinstance(column, int) or isinstance(column, bool) or not (
                allowed_range["start_row"] <= area["start_row"] <= area["end_row"] <= allowed_range["end_row"] and
                allowed_range["start_col"] <= column <= allowed_range["end_col"]):
            raise ValueError("Merge crosses the authorized daily bounds")
        if not isinstance(current_merges, list):
            raise ValueError("Current merged-cell readback required")
        for existing in current_merges:
            if set(existing) != keys or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in existing.values()):
                raise ValueError("Normalize actual merged-cell ranges first")
            if existing["start_row"] > existing["end_row"] or existing["start_col"] > existing["end_col"]:
                raise ValueError("Invalid merged-cell range")
            overlaps = not (existing["end_row"] < area["start_row"] or existing["start_row"] > area["end_row"] or
                            existing["end_col"] < column or existing["start_col"] > column)
            if overlaps:
                if existing == area:
                    return None  # Already merged: never issue a duplicate mutation.
                raise ValueError("Existing merged cells intersect the requested photo area")
        if cells_blank is not True:
            raise ValueError("Confirm all photo cells are blank before merging")
        return {**area, "merge_type": "all"}
