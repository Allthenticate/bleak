import asyncio
import logging
import sys
import threading
from typing import Any, Dict, List, Optional

# gattlib
# TODO(Chad): Only import the proper things PyCharm isn't being nice right now...
from bleak.backends.bluezgattlib import *

from bleak import BleakError
from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.signals import MatchRules, add_match, remove_match
from bleak.backends.bluezdbus.utils import (
    assert_reply,
    unpack_variants,
    validate_mac_address,
)
from bleak.backends.bluezgattlib.device import Device
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
        """
        Keyword Args:
            adapter (str): Name of Bluetooth adapter to use (e.g., hci0)
        """
        super().__init__(**kwargs)
        # kwarg "device" is for backwards compatibility
        self._adapter_name = kwargs.get("adapter", kwargs.get("device", "hci0"))
        self._adapter = c_void_p(None)
        self._c_callback = None

    async def start(self):
        """ Start scanning for devices and storing them all in a local cache """
        # Must encode as utf-8 to be a char * in C++
        ret = gattlib_adapter_open(self._adapter_name.encode("utf-8"), byref(self._adapter))
        if ret != 0:
            raise BleakError("Failed to open adapter (%s)" % self._adapter_name)

        # Implement this here so that we don't have to pass `self` to C and back
        def _on_discovered_device(adapter, address, name, user_data):
            """
                Callback when a device is discovered

            :param adapter: The low-level adapter object
            :param address: BLE address (e.g., AA:BB:CC:DD:EE:FF)
            :param name: The resolved name (e.g., bobs-iphone)
            :param user_data: User data that was passed into the scanner to keep track of which scanner returned this
            :return:
            """
            device = Device(c_void_p(None), address, name)

            # Make these strings
            name = name.decode() if name else name
            address = address.decode() if address else address

            # Get all the information wanted to pack in the advertisement data
            advertisement_data, manufacturer_id, manufacturer_data = device.get_advertisement_data()
            _manufacturer_data = {manufacturer_id: manufacturer_data}
            _service_data = advertisement_data
            _service_uuids = device.get_uuids()

            # Pack the advertisement data
            advertisement_data = AdvertisementData(
                local_name=name,
                manufacturer_data=_manufacturer_data,
                service_data=_service_data,
                service_uuids=_service_uuids,
            )

            # Create our BLEDevice to return
            device = BLEDevice(
                address,
                name,
                None,
                None,
            )

            self._callback(device, advertisement_data)

        # Be sure to keep a pointer to the callback so that the garbage collector doesn't clean it up
        self._c_callback = gattlib_discovered_device_type(_on_discovered_device)

        # Start scanning asynchronously
        gattlib_adapter_scan_enable_async(
            self._adapter,
            self._c_callback,
            None
        )

        return True

    async def stop(self):
        return gattlib_adapter_scan_disable_async(self._adapter)

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
                logger.debug("Disregarding %s since no properties could be obtained." % path)
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

