"""Polling coordinator: BB inventory + assignments + live AMS layout -> SpoolModel map.

Also owns the two orchestration primitives that are more than a raw REST call:
  - resolve_tag_fresh: client-side tag->spool (no /spoolbuddy broadcast), refresh-once
    fallback so a just-bound tag resolves.
  - relocate_assign: clear the spool's PRIOR slot(s) before assigning (BB's unique key is
    (printer,ams,tray) only, so BB won't do it) => a spool can't sit in two slots.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bambuddy.rest_client import BambuddyApiError, BambuddyRestClient
from .const import DOMAIN
from .brain.inventory import (
    SpoolModel,
    TrayState,
    canonical_tag,
    derive_slots,
    map_inventory,
    parse_ams,
    resolve_tag,
)

_LOGGER = logging.getLogger(__name__)


def _pid(printer: dict) -> int | None:
    try:
        return int(printer.get("id"))
    except (TypeError, ValueError):
        return None


class SpoolTapCoordinator(DataUpdateCoordinator[dict[int, SpoolModel]]):
    """Polls the Bambuddy inventory read-path on an interval."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        rest: BambuddyRestClient,
        *,
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="spooltap inventory",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.rest = rest
        self.printers: list[dict] = []
        # the ACTIVE printer: everything slot-shaped (layout, registry, pickers, assign
        # default) is scoped to it. Persisted with the registry; falls back to BB's
        # first printer. Single-printer installs never notice any of this.
        self.printer_id: int = 1
        self._active_printer: int | None = None
        self.tray_states: dict[tuple[int, int], TrayState] = {}
        self.slots: list[dict] = []  # auto-derived AMS slot layout from BB (persists last-known)
        # slot->tag registry (SoA): the one thing BB can't store. Persisted here (ships
        # EMPTY; the user binds slot tags in the Bind area). One registry per printer,
        # keyed by str(printer_id) then slot key '<ams>_<tray>'. `slot_tags` is the
        # ACTIVE printer's view of it.
        self._registry: dict[str, dict[str, str]] = {}
        # a pre-0.4.0 flat registry (no printer dimension) waits here until BB tells us
        # which printer it belonged to (its first-listed one); then it is filed under it
        self._legacy_flat: dict[str, str] | None = None
        self._store: Store = Store(hass, 1, f"{DOMAIN}_slot_tags_{entry.entry_id}")
        self._assign_lock = asyncio.Lock()  # serialize relocate_assign (list->clear->assign)

    # ------------------------------------------------------------- registry
    @property
    def slot_tags(self) -> dict[str, str]:
        """The active printer's slot->tag registry (live dict — mutate + save)."""
        key = str(self.printer_id)
        if self._legacy_flat is not None and not self.printers:
            return self._legacy_flat  # BB not seen yet: keep serving the old flat map
        self._adopt_legacy(key)
        return self._registry.setdefault(key, {})

    @slot_tags.setter
    def slot_tags(self, value: dict[str, str]) -> None:
        if self._legacy_flat is not None and not self.printers:
            self._legacy_flat = dict(value)
            return
        self._registry[str(self.printer_id)] = dict(value)

    def _adopt_legacy(self, key: str) -> None:
        if self._legacy_flat is None:
            return
        # first printer BB listed == the printer the flat registry always meant
        self._registry.setdefault(key, {}).update(
            {k: v for k, v in self._legacy_flat.items() if k not in self._registry[key]}
        )
        self._legacy_flat = None

    async def async_load_slot_tags(self) -> None:
        loaded = (await self._store.async_load()) or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("printers"), dict):
            self._registry = {
                str(pid): {k: canonical_tag(v) for k, v in (tags or {}).items() if v}
                for pid, tags in loaded["printers"].items()
            }
            ap = loaded.get("active_printer")
            self._active_printer = int(ap) if ap is not None else None
            if self._active_printer is not None:
                self.printer_id = self._active_printer
            return
        # pre-0.4.0 flat shape {"<ams>_<tray>": "<uid>"}; also canonicalizes the very
        # old raw-tag_id bindings (binds and the dispatch compare canonical)
        self._legacy_flat = {
            k: canonical_tag(v) for k, v in loaded.items() if v and isinstance(v, str)
        }

    async def async_save_slot_tags(self) -> None:
        if self._legacy_flat is not None and not self.printers:
            await self._store.async_save(self._legacy_flat)  # BB unseen: keep old shape
        else:
            self._adopt_legacy(str(self.printer_id))
            await self._store.async_save(
                {
                    "version": 2,
                    "active_printer": self._active_printer,
                    "printers": {k: v for k, v in self._registry.items() if v},
                }
            )
        self.async_update_listeners()  # re-render the slots sensor

    # ------------------------------------------------------------- printers
    def _printer_label_map(self) -> dict[int, str]:
        """{printer_id: "Name (Model)"}; identical name+model pairs get " #<id>" so
        every printer stays selectable (a fleet of same-model printers is the #3 case)."""
        base: dict[int, str] = {}
        for p in self.printers:
            pid = _pid(p)
            if pid is None:
                continue
            name = str(p.get("name") or f"Printer {pid}")
            model = p.get("model")
            base[pid] = f"{name} ({model})" if model else name
        counts: dict[str, int] = {}
        for label in base.values():
            counts[label] = counts.get(label, 0) + 1
        return {
            pid: (f"{label} #{pid}" if counts[label] > 1 else label)
            for pid, label in base.items()
        }

    def printer_labels(self) -> list[str]:
        return list(self._printer_label_map().values())

    def active_printer_label(self) -> str | None:
        return self._printer_label_map().get(self.printer_id)

    def printer_id_for_label(self, label: str) -> int | None:
        return next(
            (pid for pid, lab in self._printer_label_map().items() if lab == label), None
        )

    def _resolve_active(self) -> None:
        """printer_id := the persisted choice if BB still lists it, else BB's first."""
        ids = [_pid(p) for p in self.printers if _pid(p) is not None]
        if not ids:
            return
        if self._active_printer in ids:
            self.printer_id = self._active_printer
        else:
            self.printer_id = ids[0]

    async def async_set_active_printer(self, printer_id: int) -> None:
        """Switch the active printer (persisted), then re-poll its layout."""
        if printer_id == self.printer_id and self._active_printer == printer_id:
            return
        self._active_printer = printer_id
        self.printer_id = printer_id
        self.slots = []  # never show the previous printer's layout for the new one
        self.tray_states = {}
        await self.async_save_slot_tags()
        await self.async_refresh()

    async def _async_update_data(self) -> dict[int, SpoolModel]:
        try:
            spools = await self.rest.list_spools()
            try:
                assignments = await self.rest.list_assignments()
            except Exception as err:  # noqa: BLE001 - slot join is best-effort
                _LOGGER.debug("assignments poll failed (slots omitted): %s", err)
                assignments = []
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"inventory poll failed: {err}") from err
        # best-effort live AMS layout (offline -> keep last-good, used for slot picker +
        # material-mismatch + mid-print guard). Never fails the inventory poll.
        try:
            printers = await self.rest.get_printers()
            if printers:
                self.printers = printers
                self._resolve_active()
                self._adopt_legacy(str(self.printer_id))
                status = await self.rest.get_printer_status(self.printer_id)
                if status is not None:
                    self.tray_states = parse_ams(status)
                    labels = await self.rest.get_ams_labels(self.printer_id)
                    derived = derive_slots(status, labels)
                    if derived:  # only overwrite when BB actually returned a layout
                        self.slots = derived
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("AMS layout poll failed: %s", err)
        return map_inventory(spools, assignments)

    # ------------------------------------------------------------- orchestration
    def resolve_tag_local(self, tag_uid: str) -> int | None:
        return resolve_tag(self.data or {}, tag_uid)

    async def resolve_tag_fresh(self, tag_uid: str) -> int | None:
        """Client-side resolve; if unseen, poll once (a tag bound seconds ago)."""
        spool_id = self.resolve_tag_local(tag_uid)
        if spool_id is None:
            await self.async_refresh()
            spool_id = self.resolve_tag_local(tag_uid)
        return spool_id

    async def relocate_assign(
        self, spool_id: int, printer_id: int, ams_id: int, tray_id: int
    ) -> dict:
        """Clear the spool's prior slot(s), then assign. BB's assign auto-configures
        the physical slot (profile+K+color) or defers via pending_config.

        The clear must SUCCEED before we assign — proceeding after a failed clear
        would leave the spool occupying two slots, the exact thing this exists to
        prevent — so a clear failure aborts. A 404 on the delete is fine (BB
        auto-removes assignments on tray fingerprint mismatch)."""
        target = (printer_id, ams_id, tray_id)
        async with self._assign_lock:
            try:
                for a in await self.rest.list_assignments():
                    if a.get("spool_id") == spool_id and (
                        a.get("printer_id"),
                        a.get("ams_id"),
                        a.get("tray_id"),
                    ) != target:
                        try:
                            await self.rest.unassign(
                                a["printer_id"], a["ams_id"], a["tray_id"]
                            )
                        except BambuddyApiError as err:
                            if err.status != 404:  # already gone == cleared
                                raise
            except HomeAssistantError:
                raise
            except Exception as err:  # noqa: BLE001
                raise HomeAssistantError(
                    f"Assign aborted: could not clear spool {spool_id}'s prior "
                    f"slot ({err}); the spool would occupy two slots."
                ) from err
            result = await self.rest.assign_slot(spool_id, printer_id, ams_id, tray_id)
        await self.async_refresh()
        return result
