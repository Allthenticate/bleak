import logging
from typing import Any, Dict, List, Optional

# gattlib
from gattlib import adapter

from bleak import BleakError
from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.signals import MatchRules, add_match, remove_match
from bleak.backends.bluezdbus.utils import (
    assert_reply,
    unpack_variants,
    validate_mac_address,
)
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import BaseBleakScanner, AdvertisementData

logger = logging.getLogger(__name__)

# set of org.bluez.Device1 property names that come from advertising data
_ADVERTISING_DATA_PROPERTIES = {
    "AdvertisingData",
    "AdvertisingFlags",
    "ManufacturerData",
    "Name",
    "ServiceData",
    "UUIDs",
}


def _device_info(path, props):
    try:
        name = props.get("Alias", "Unknown")
        address = props.get("Address", None)
        if address is None:
            try:
                address = path[-17:].replace("_", ":")
                if not validate_mac_address(address):
                    address = None
            except Exception:
                address = None
        rssi = props.get("RSSI", "?")
        return name, address, rssi, path
    except Exception:
        return None, None, None, None


class BleakScannerBlueZGattlib(BaseBleakScanner):
    """The native Linux Bleak BLE Scanner.

    For possible values for `filters`, see the parameters to the
    ``SetDiscoveryFilter`` method in the `BlueZ docs
    <https://git.kernel.org/pub/scm/bluetooth/bluez.git/tree/doc/adapter-api.txt?h=5.48&id=0d1e3b9c5754022c779da129025d493a198d49cf>`_

    Keyword Args:
        adapter (str): Bluetooth adapter to use for discovery.
        filters (dict): A dict of filters to be applied on discovery.

    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # kwarg "device" is for backwards compatibility
        self._adapter_name = kwargs.get("adapter", kwargs.get("device", "hci0"))
        self._adapter = adapter.Adapter(name=self._adapter_name)

        # Discovery filters
        self._filters: Dict[str, Variant] = {}
        self.set_scanning_filter(**kwargs)

    async def start(self):
        """ Start scanning for devices and storing them all in a local cache """
        self._adapter.open()
        self._adapter.scan_enable(self._parse_msg, -1)

    async def stop(self):

        return self._adapter.scan_disable()

    def set_scanning_filter(self, **kwargs):
        """Sets OS level scanning filters for the BleakScanner.

        For possible values for `filters`, see the parameters to the
        ``SetDiscoveryFilter`` method in the `BlueZ docs
        <https://git.kernel.org/pub/scm/bluetooth/bluez.git/tree/doc/adapter-api.txt?h=5.48&id=0d1e3b9c5754022c779da129025d493a198d49cf>`_

        Keyword Args:
            filters (dict): A dict of filters to be applied on discovery.

        """
        pass
        # self._filters = {k: Variant(v) for k, v in kwargs.get("filters", {}).items()}
        # if "Transport" not in self._filters:
        #     self._filters["Transport"] = Variant("s", "le")

    async def get_discovered_devices(self) -> List[BLEDevice]:
        # Reduce output.
        discovered_devices = []
        for path, props in self._devices.items():
            if not props:
                logger.debug(
                    "Disregarding %s since no properties could be obtained." % path
                )
                continue
            name, address, _, path = _device_info(path, props)
            if address is None:
                continue
            uuids = props.get("UUIDs", [])
            manufacturer_data = props.get("ManufacturerData", {})
            discovered_devices.append(
                BLEDevice(
                    address,
                    name,
                    {"path": path, "props": props},
                    props.get("RSSI", 0),
                    uuids=uuids,
                    manufacturer_data=manufacturer_data,
                )
            )
        return discovered_devices

    # Helper methods

    # def _invoke_callback(self, path: str, message: Message) -> None:
    #     """Invokes the advertising data callback.
    #
    #     Args:
    #         message: The D-Bus message that triggered the callback.
    #     """
    #     if self._callback is None:
    #         return
    #
    #     props = self._devices[path]
    #
    #     # Get all the information wanted to pack in the advertisement data
    #     _local_name = props.get("Name")
    #     _manufacturer_data = {k: bytes(v) for k, v in props.get("ManufacturerData", {}).items()}
    #     _service_data = {k: bytes(v) for k, v in props.get("ServiceData", {}).items()}
    #     _service_uuids = props.get("UUIDs", [])
    #
    #     # Pack the advertisement data
    #     advertisement_data = AdvertisementData(
    #         local_name=_local_name,
    #         manufacturer_data=_manufacturer_data,
    #         service_data=_service_data,
    #         service_uuids=_service_uuids,
    #         platform_data=(props, message),
    #     )
    #
    #     device = BLEDevice(
    #         props["Address"],
    #         props["Alias"],
    #         {"path": path, "props": props},
    #         props.get("RSSI", 0),
    #     )
    #
    #     self._callback(device, advertisement_data)

    def _parse_msg(self, device, user_data):

        logger.debug("---------------------------------")
        logger.debug("Found BLE Device %s" % device.id)
        adv_data = device.get_advertisement_data()
        logger.debug("\t%s" % str(adv_data))
        uuids = device.get_uuids()
        logger.debug("\t%s" % uuids)
        print(device.id, uuids)

        # Only do advertising data callback if this is the first time the
        # device has been seen or if an advertising data property changed.
        # Otherwise we get a flood of callbacks from RSSI changing.
        # if first_time_seen or not _ADVERTISING_DATA_PROPERTIES.isdisjoint(changed.keys()):
        #     self._invoke_callback(message.path, message)
