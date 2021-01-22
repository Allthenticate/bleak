import asyncio
import logging
import sys
import threading
import uuid
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
from bleak.backends.bluezgattlib.exception import handle_return
from bleak.backends.bluezgattlib.uuid import gattlib_uuid_to_int
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

        # TODO(Chad): Implement this cache?
        self._cached_devices: Dict[str, str] = {}
        self._devices: Dict[str, Dict[str, Any]] = {}

    def _get_advertisement_data(self, address):
        """

        :param address:
        :return:
        """
        _advertisement_data = POINTER(GattlibAdvertisementData)()
        _advertisement_data_count = c_size_t(0)
        _manufacturer_id = c_uint16(0)
        _manufacturer_data = c_void_p(None)
        _manufacturer_data_len = c_size_t(0)

        if address is None:
            logger.error("_get_advertisement_data called with None address")
            return {}, None, []

        ret = gattlib_get_advertisement_data_from_mac(
            self._adapter,
            address,
            byref(_advertisement_data),
            byref(_advertisement_data_count),
            byref(_manufacturer_id),
            byref(_manufacturer_data),
            byref(_manufacturer_data_len),
        )

        handle_return(ret)

        advertisement_data = {}
        manufacturer_data = None

        for i in range(0, _advertisement_data_count.value):
            service_data = _advertisement_data[i]
            uuid_adv = gattlib_uuid_to_int(service_data.uuid)

            pointer_type = POINTER(c_byte * service_data.data_length)
            c_bytearray = cast(service_data.data, pointer_type)

            data = bytearray(service_data.data_length)
            for i in range(service_data.data_length):
                data[i] = c_bytearray.contents[i] & 0xFF

            advertisement_data[uuid_adv] = data

        if _manufacturer_data_len.value > 0:
            pointer_type = POINTER(c_byte * _manufacturer_data_len.value)
            c_bytearray = cast(_manufacturer_data, pointer_type)

            manufacturer_data = bytearray(_manufacturer_data_len.value)
            for i in range(_manufacturer_data_len.value):
                manufacturer_data[i] = c_bytearray.contents[i] & 0xFF

        return advertisement_data, _manufacturer_id.value, manufacturer_data

    def _get_uuids(self, address):
        """
        List all of the service UUIDs for this device

        Returns:
            List of UUIDs [uuid.UUID, ...]

        C function definitions
        /**
         * @brief Function to retrieve advertised UUIDs of this device
         *
         * @param connection Active GATT connection
         * @param services is an array of UUIDs
         * @param services_count is the number of UUIDs in services
         *
         * @return GATTLIB_SUCCESS on success or GATTLIB_* error code
         */
        int gattlib_get_uuids(gatt_connection_t* connection,
                              const char** services,
                              size_t* services_count);

        /**
         * @brief Function to retrieve advertised UUIDs of this device
         *
         * @param adapter is the adapter the new device has been seen
         * @param mac_address is the MAC address of the device to get UUIDs from
         * @param services is an array of UUIDs
         * @param services_count is the number of UUIDs in services
         *
         * @return GATTLIB_SUCCESS on success or GATTLIB_* error code
         */
        int gattlib_get_uuids_from_mac(void* adapter, const char* mac_address,
                                       const char ** services,
                                       size_t* services_count);
        """

        _services = POINTER(c_char_p)()
        _services_count = c_size_t(0)
        ret = gattlib_get_uuids_from_mac(
            self._adapter, address, byref(_services), byref(_services_count)
        )
        handle_return(ret)
        if ret != 0:
            logger.error(f"Failed to get UUIDs for {address}")
            return []

        services = []
        for i in range(0, _services_count.value):
            services.append(_services[i].decode().lower())

        return services

    async def start(self):
        """ Start scanning for devices and storing them all in a local cache """
        # Must encode as utf-8 to be a char * in C++
        ret = gattlib_adapter_open(
            self._adapter_name.encode("utf-8"), byref(self._adapter)
        )
        if ret != 0:
            raise BleakError("Failed to open adapter (%s)" % self._adapter_name)

        # Clear our caches
        self._devices.clear()
        self._cached_devices.clear()

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

            if address is None:
                return
            # Get all the information wanted to pack in the advertisement data
            (
                advertisement_data,
                manufacturer_id,
                manufacturer_data,
            ) = self._get_advertisement_data(address)

            if isinstance(manufacturer_data, bytearray):
                manufacturer_data = bytes(manufacturer_data)
            manufacturer_data_dict = {manufacturer_id: manufacturer_data}
            service_data = advertisement_data
            service_uuids = self._get_uuids(address)

            # Make these strings
            name = name.decode() if name else name
            address = address.decode() if address else address

            # Pack the advertisement data
            advertisement_data = AdvertisementData(
                local_name=name,
                manufacturer_data=manufacturer_data_dict,
                service_data=service_data,
                service_uuids=service_uuids,
            )

            # Create our BLEDevice to return
            device = BLEDevice(
                address,
                name,
                None,
                None,
            )

            properties = self._devices.get(address, {})
            properties["AdvertisingData"] = advertisement_data
            properties["ManufacturerData"] = manufacturer_data_dict
            properties["Name"] = name
            properties["ServiceData"] = service_data
            properties["UUIDs"] = service_uuids

            self._devices[address] = properties

            self._callback(device, advertisement_data)

        # Be sure to keep a pointer to the callback so that the garbage collector doesn't clean it up
        self._c_callback = gattlib_discovered_device_type(_on_discovered_device)

        # Start scanning asynchronously
        gattlib_adapter_scan_enable_async(self._adapter, self._c_callback, None)

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
        for address, props in self._devices.items():
            if not props:
                logger.debug(
                    "Disregarding %s since no properties could be obtained." % address
                )
                continue
            if address is None:
                continue
            name = props["Name"]
            uuids = props.get("UUIDs", [])
            manufacturer_data = props.get("ManufacturerData", {})
            discovered_devices.append(
                BLEDevice(
                    address,
                    name,
                    {"props": props},
                    props.get("RSSI", 0),
                    uuids=uuids,
                    manufacturer_data=manufacturer_data,
                    adapter=self._adapter,
                )
            )
        return discovered_devices
