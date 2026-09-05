"""Inventory model + mapper: BB spool JSON -> SpoolModel, joined with slot assignments.

Pure module — no HA, no aiohttp imports — so it unit-tests fast. Adapted from thegrove's
proven `brain/inventory.py` (own copy). `remaining_grams` is DERIVED (BB has no such field).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_HEX = re.compile(r"[^0-9a-fA-F]")


@dataclass(frozen=True, slots=True)
class SpoolModel:
    """An HA-friendly projection of a Bambuddy native spool + its slot assignment."""

    spool_id: int
    material: str
    color_name: str | None
    rgba: str | None
    brand: str | None
    category: str | None
    label_weight: float
    weight_used: float
    core_weight: float
    remaining_grams: float
    tag_uid: str | None
    tag_type: str | None
    data_origin: str | None
    slicer_filament: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    archived_at: str | None
    assigned_slot: str | None = None
    assigned_printer_id: int | None = None
    assigned_ams_id: int | None = None
    assigned_tray_id: int | None = None

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.brand, self.color_name, self.material) if p]
        return " ".join(parts) or f"Spool {self.spool_id}"


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _s(data: dict, key: str) -> str | None:
    value = data.get(key)
    return str(value) if value not in (None, "") else None


def map_spool(data: dict, assignment: dict | None) -> SpoolModel:
    label = _f(data.get("label_weight"), 0.0)
    used = _f(data.get("weight_used"), 0.0)
    slot = printer_id = ams_id = tray_id = None
    if assignment:
        printer_id = assignment.get("printer_id")
        ams_id = assignment.get("ams_id")
        tray_id = assignment.get("tray_id")
        if None not in (printer_id, ams_id, tray_id):
            slot = f"P{printer_id}/AMS{ams_id}/Tray{tray_id}"
    return SpoolModel(
        spool_id=int(data["id"]),
        material=str(data.get("material") or "?"),
        color_name=_s(data, "color_name"),
        rgba=_s(data, "rgba"),
        brand=_s(data, "brand"),
        category=_s(data, "category"),
        label_weight=label,
        weight_used=used,
        core_weight=_f(data.get("core_weight"), 250.0),
        remaining_grams=round(label - used, 2),
        tag_uid=_s(data, "tag_uid"),
        tag_type=_s(data, "tag_type"),
        data_origin=_s(data, "data_origin"),
        slicer_filament=_s(data, "slicer_filament"),
        nozzle_temp_min=_i(data.get("nozzle_temp_min")),
        nozzle_temp_max=_i(data.get("nozzle_temp_max")),
        archived_at=_s(data, "archived_at"),
        assigned_slot=slot,
        assigned_printer_id=_i(printer_id) if printer_id is not None else None,
        assigned_ams_id=_i(ams_id) if ams_id is not None else None,
        assigned_tray_id=_i(tray_id) if tray_id is not None else None,
    )


def _assignments_by_spool(assignments) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in assignments if isinstance(assignments, list) else []:
        if isinstance(row, dict) and row.get("spool_id") is not None:
            out[int(row["spool_id"])] = row
    return out


def map_inventory(spools, assignments) -> dict[int, SpoolModel]:
    by_spool = _assignments_by_spool(assignments)
    out: dict[int, SpoolModel] = {}
    for row in spools if isinstance(spools, list) else []:
        if isinstance(row, dict) and row.get("id") is not None:
            model = map_spool(row, by_spool.get(int(row["id"])))
            out[model.spool_id] = model
    return out


# --------------------------------------------------------------- tag resolution
def canonical_tag(tag_uid: str | None) -> str:
    """The form BB stores/matches, mirroring BB's normalize_tag_uid: strip every
    non-hex char FIRST, then keep the last 16, uppercased. The strip matters for
    separator-bearing HA tag ids (companion-app UUIDs, dashed ESPHome ids) — BB
    strips server-side on link, so we must strip too or those tags never resolve."""
    return _NON_HEX.sub("", tag_uid or "")[-16:].upper()


def resolve_tag(inventory: dict[int, SpoolModel], tag_uid: str) -> int | None:
    """Client-side resolve: match a scanned tag against the polled inventory.

    Avoids POST /spoolbuddy/nfc/tag-scanned, which (a) flips to Spoolman when that
    integration is on and (b) broadcasts a WS event that refetches inventory on every
    open Bambuddy session + pops "New Tag Detected" on any SpoolBuddy kiosk. BB stores
    tag_uid canonicalized, so an exact canonical match is authoritative (no fuzzy path).
    """
    want = canonical_tag(tag_uid)
    if not want:
        return None
    for spool_id, model in inventory.items():
        if model.archived_at is None and canonical_tag(model.tag_uid) == want:
            return spool_id
    return None


# ------------------------------------------------------------------- AMS layout
@dataclass(frozen=True, slots=True)
class TrayState:
    """Live per-tray view from the printer status (for slot picker + guards)."""

    ams_id: int
    tray_id: int
    material: str | None
    tray_type: str | None
    active: bool  # this tray is feeding the current print


def parse_ams(status: dict | None) -> dict[tuple[int, int], TrayState]:
    """Parse BB `GET /printers/{id}/status` -> {(ams_id, tray_id): TrayState}.

    Defensive: BB/firmware shapes vary. Recognizes `ams` (list of units, each with
    `id`/`trays`) + `vt_tray`/`external` for ams 255. Returns {} when offline/empty.
    """
    out: dict[tuple[int, int], TrayState] = {}
    if not isinstance(status, dict):
        return out
    active_ext = status.get("active_extruder")
    tray_now = status.get("tray_now")

    def _add(ams_id: int, tray: dict, *, tid: int | None = None, gid: int | None = None) -> None:
        if tid is None:
            tid = _i(tray.get("id") if tray.get("id") is not None else tray.get("tray_id"))
        if tid is None:
            return
        if gid is None:
            gid = ams_id if ams_id >= 128 else ams_id * 4 + tid
        active = str(tray_now) == str(gid) if tray_now is not None else False
        out[(ams_id, tid)] = TrayState(
            ams_id=ams_id,
            tray_id=tid,
            material=_s(tray, "tray_type") or _s(tray, "material"),
            tray_type=_s(tray, "tray_type"),
            active=active,
        )

    for unit in status.get("ams", []) if isinstance(status.get("ams"), list) else []:
        if not isinstance(unit, dict):
            continue
        ams_id = _i(unit.get("id"))
        if ams_id is None:
            continue
        for tray in unit.get("tray", []) or unit.get("trays", []) or []:
            if isinstance(tray, dict):
                _add(ams_id, tray)
    # external feed(s): BB's global tray id is 254 (Ext-L / the only external) or 255
    # (Ext-R on dual-nozzle printers); the assignment contract is ams 255 + tray id-254
    for tray_id, _label, raw in external_feeds(status):
        _add(255, raw, tid=tray_id, gid=254 + tray_id)
    _ = active_ext  # reserved for dual-nozzle active-tray refinement
    return out


def external_feeds(status: dict | None) -> list[tuple[int, str, dict]]:
    """The external spool holder(s) BB reports -> [(tray_id, label, raw_entry)].

    BB emits `vt_tray` as a LIST: id 254 = Ext-L (or the single external holder),
    id 255 = Ext-R (dual-nozzle H2 series). Assignments address them as
    `ams_id=255, tray_id=id-254` (BB inventory route), so that is the tray id here.
    Entries without an id fall back to their position. One entry -> "External";
    two -> "Ext-L" / "Ext-R".
    """
    if not isinstance(status, dict):
        return []
    raw_list = status.get("vt_tray")
    if isinstance(raw_list, dict):  # very old single-object shape
        raw_list = [raw_list]
    if not isinstance(raw_list, list):
        return []
    feeds: list[tuple[int, dict]] = []
    for idx, vt in enumerate(raw_list):
        if not isinstance(vt, dict):
            continue
        raw_id = _i(vt.get("id"))
        tray_id = raw_id - 254 if raw_id is not None and raw_id >= 254 else idx
        if tray_id in (0, 1) and all(t != tray_id for t, _ in feeds):
            feeds.append((tray_id, vt))
    feeds.sort(key=lambda f: f[0])
    if len(feeds) == 1:
        return [(feeds[0][0], "External", feeds[0][1])]
    names = {0: "Ext-L", 1: "Ext-R"}
    return [(t, names[t], vt) for t, vt in feeds]


def derive_slots(status: dict | None, labels: dict | None) -> list[dict]:
    """Auto-derive the AMS slot layout from BB (labels included) — no hardcoding.

    Returns [{key:'<ams>_<tray>', ams_id, tray_id, label}] for every tray BB reports,
    plus the external feed(s) (ams 255; Ext-L/Ext-R on dual-nozzle). BB caches the layout,
    so this works offline.
    """
    slots: list[dict] = []
    if not isinstance(status, dict):
        return slots
    labels = labels or {}
    for unit in status.get("ams", []) if isinstance(status.get("ams"), list) else []:
        if not isinstance(unit, dict):
            continue
        ams_id = _i(unit.get("id"))
        if ams_id is None:
            continue
        label = labels.get(str(ams_id)) or f"AMS {ams_id}"
        for tray in unit.get("tray", []) or unit.get("trays", []) or []:
            tid = _i(tray.get("id") if isinstance(tray, dict) else None)
            if tid is None:
                continue
            slots.append({"key": f"{ams_id}_{tid}", "ams_id": ams_id, "tray_id": tid,
                          "label": f"{label} · Tray {tid + 1}"})
    for tray_id, label, _raw in external_feeds(status):
        slots.append({"key": f"255_{tray_id}", "ams_id": 255, "tray_id": tray_id, "label": label})
    return slots
