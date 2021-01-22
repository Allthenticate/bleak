# -*- coding: utf-8 -*-
"""
BLE Client for BlueZ on Linux
"""
import logging
import asyncio
import os
import re
import subprocess
import uuid
import warnings
from ctypes import c_int, c_ubyte, cast, c_void_p, c_char, c_uint16, c_byte
from typing import Any, Callable, Dict, List, Optional, Union
from uuid import UUID

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, ErrorType, MessageType
from dbus_next.message import Message
from dbus_next.signature import Variant

from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.characteristic import BleakGATTCharacteristicBlueZDBus
from bleak.backends.bluezdbus.descriptor import BleakGATTDescriptorBlueZDBus
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
from bleak.backends.bluezdbus.service import BleakGATTServiceBlueZDBus
from bleak.backends.bluezdbus.signals import MatchRules, add_match, remove_match
from bleak.backends.bluezdbus.utils import assert_reply, unpack_variants
from bleak.backends.bluezgattlib import (
    gattlib_connect,
    gattlib_disconnect,
    POINTER,
    c_char_p,
    c_size_t,
    gattlib_get_uuids_from_mac,
    byref,
    gattlib_get_uuids,
    GattlibPrimaryService,
    gattlib_discover_primary,
    gattlib_discover_char,
    GattlibCharacteristic,
    gattlib_read_char_by_uuid,
    gattlib_write_char_by_uuid,
    gattlib_write_without_response_char_by_uuid,
    GattlibAdvertisementData,
    gattlib_get_advertisement_data_from_mac,
    gattlib_get_advertisement_data,
    gattlib_discover_char_range,
    GattlibUuid,
    GattlibUuidTypes,
    GattlibUuidValue,
    gattlib_string_to_uuid,
)
from bleak.backends.bluezgattlib.characteristic import (
    BleakGATTCharacteristicBlueZGattlib,
)
from bleak.backends.bluezgattlib.exception import handle_return
from bleak.backends.bluezgattlib.gatt import GattCharacteristic, GattService
from bleak.backends.bluezgattlib.service import BleakGATTServiceBlueZGattlib
from bleak.backends.bluezgattlib.uuid import gattlib_uuid_to_int
from bleak.backends.client import BaseBleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError, BleakError


logger = logging.getLogger(__name__)

# BLE constants
CONNECTION_OPTIONS_LEGACY_BDADDR_LE_PUBLIC = 1 << 0
CONNECTION_OPTIONS_LEGACY_BDADDR_LE_RANDOM = 1 << 1
CONNECTION_OPTIONS_LEGACY_BT_SEC_LOW = 1 << 2
CONNECTION_OPTIONS_LEGACY_BT_SEC_MEDIUM = 1 << 3
CONNECTION_OPTIONS_LEGACY_BT_SEC_HIGH = 1 << 4
CONNECTION_OPTIONS_LEGACY_DEFAULT = (
    CONNECTION_OPTIONS_LEGACY_BDADDR_LE_PUBLIC
    | CONNECTION_OPTIONS_LEGACY_BDADDR_LE_RANDOM
    | CONNECTION_OPTIONS_LEGACY_BT_SEC_LOW
)


class BleakClientBlueZGattlib(BaseBleakClient):
    """A native Linux Bleak Client

    Implemented by using the `gattlib API <https://github.com/Allthenticate/gattlib`_.

    Args:
        address_or_ble_device (`BLEDevice` or str): The Bluetooth address of the BLE peripheral to connect to or the `BLEDevice` object representing it.

    Keyword Args:
        timeout (float): Timeout for required ``BleakScanner.find_device_by_address`` call. Defaults to 10.0.
        disconnected_callback (callable): Callback that will be scheduled in the
            event loop when the client is disconnected. The callable must take one
            argument, which will be this client object.
        adapter (str): Bluetooth adapter to use for discovery.
    """

    def __init__(self, address_or_ble_device: Union[BLEDevice, str], **kwargs):
        super(BleakClientBlueZGattlib, self).__init__(address_or_ble_device, **kwargs)
        # kwarg "device" is for backwards compatibility
        self._adapter_name = kwargs.get("adapter", kwargs.get("device", "hci0"))
        self._adapter = None

        # Extract address from BLEDevice
        if isinstance(address_or_ble_device, BLEDevice):
            self._address = address_or_ble_device.address
            self._adapter = address_or_ble_device.metadata["adapter"]
        # Encode our string to make sure we can send it to the ctypes properly
        elif type(address_or_ble_device) == str:
            self._address = address_or_ble_device
        else:
            raise BleakError(
                f"Argument 'address_or_ble_device' must be BLEDevice or str, got {type(address_or_ble_device)}."
            )

        self._address_encoded = self._address.encode("utf-8")

        # Connection variables
        self._connection = None

    # Connectivity methods

    async def connect(
        self, options=CONNECTION_OPTIONS_LEGACY_DEFAULT, **kwargs
    ) -> bool:
        """Connect to the specified GATT server."""
        logger.debug(f"Connecting to device @ {self.address} with {self._adapter_name}")

        # Must encode as utf-8 to be a char * in C
        self._connection = gattlib_connect(
            self._adapter, self._address_encoded, options
        )
        if self._connection is None:
            raise BleakError("Connection failed.")

        logger.debug(f"Connected to {self.address}")

        return True

    async def _remove_signal_handlers(self) -> None:
        """
        Remove all pending notifications of the client. This method is used to
        free the DBus matches that have been established.
        """
        logger.debug(f"_remove_signal_handlers({self._address})")
        raise NotImplementedError

    async def disconnect(self) -> bool:
        """Disconnect from the specified GATT server."""
        logger.debug(f"Disconnecting ({self._address})")

        if self._connection is not None:
            ret = gattlib_disconnect(self._connection)
            handle_return(ret)
            return ret == 0
        return False

    async def pair(self, *args, **kwargs) -> bool:
        """Pair with the peripheral."""
        raise NotImplementedError

    async def unpair(self) -> bool:
        """Unpair with the peripheral.

        Returns:
            Boolean regarding success of unpairing.

        """
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """Check connection status between this client and the server.

        Returns:
            Boolean representing connection status.

        """
        return self._connection == None

    # GATT services methods

    async def get_services(self) -> BleakGATTServiceCollection:
        """Get all services registered for this GATT server.

        Returns:
           A :py:class:`bleak.backends.service.BleakGATTServiceCollection` with this device's services tree.

        TODO: Convert everything to Bleak objects
        """
        if self._services_resolved:
            return self.services

        # Get all of our services
        _services = POINTER(GattlibPrimaryService)()
        _services_count = c_int(0)
        ret = gattlib_discover_primary(
            self._connection, byref(_services), byref(_services_count)
        )
        handle_return(ret)
        if ret != 0:
            logger.error("Failed to retrieve service UUIDs")

        # Add our services to our list
        for i in range(0, _services_count.value):
            # service = GattService(self, _services[i])
            service = BleakGATTServiceBlueZGattlib(_services[i])
            self.services.add_service(service)
            logging.debug(
                "Service UUID: %s (%d,%d)"
                % (
                    service.uuid,
                    _services[i].attr_handle_start,
                    _services[i].attr_handle_end,
                )
            )

        # Get all of our characteristics
        _characteristics = POINTER(GattlibCharacteristic)()
        _characteristics_count = c_int(0)
        ret = gattlib_discover_char(
            self._connection,
            byref(_characteristics),
            byref(_characteristics_count),
        )
        handle_return(ret)
        if ret != 0:
            logger.error("Failed to retrieve characteristics")

        # Figure out which characteristics go with which services
        for i in range(0, _characteristics_count.value):
            characteristic = BleakGATTCharacteristicBlueZGattlib(_characteristics[i])

            logging.debug(
                "Characteristic UUID: %s (%d)"
                % (characteristic.uuid, characteristic.handle)
            )
            added = False
            for s in self.services:
                if s.add_characteristic(characteristic):
                    characteristic.add_service_uuid(s.uuid)
                    added = True
            if not added:
                logger.warning(
                    "Characteristic was not associated with any service. (%s,%d)"
                    % (characteristic.uuid, characteristic.handle)
                )
        self._services_resolved = True
        return self.services

    # IO methods

    async def read_gatt_char(
        self,
        char_specifier: Union[BleakGATTCharacteristicBlueZGattlib, int, str, UUID],
        timeout: int = 1,
        **kwargs,
    ) -> bytearray:
        """Perform read operation on the specified GATT characteristic.

        Args:
            char_specifier (BleakGATTCharacteristicBlueZDBus, int, str or UUID): The characteristic to read from,
                specified by either integer handle, UUID or directly by the
                BleakGATTCharacteristicBlueZDBus object representing it.
            timeout (int): The number of seconds to wait before timing out.

        Returns:
            (bytearray) The read data.

        """
        _buffer = c_void_p(None)
        _buffer_len = c_size_t(0)

        char_uuid = None
        if isinstance(char_specifier, BleakGATTCharacteristicBlueZGattlib):
            char_uuid = char_specifier._gattlib_characteristic.uuid
        elif isinstance(char_specifier, UUID):
            gattlib_uuid = GattlibUuid()

            uuid_ascii = str(char_specifier).encode("utf-8")
            ret = gattlib_string_to_uuid(
                uuid_ascii, len(uuid_ascii), byref(gattlib_uuid)
            )
            handle_return(ret)
            char_uuid = gattlib_uuid
            # char_uuid = GattlibUuid()
            # char_uuid.type = GattlibUuidTypes.BT_UUID128
            # char_uuid_value = GattlibUuidValue
            # char_uuid_value.uuid128 = char_specifier.bytes
            # char_uuid.value.uuid128 = char_specifier.bytes
        else:
            raise BleakError(
                f"{type(char_specifier)} not currently supported for char_specifier"
            )
        ret = gattlib_read_char_by_uuid(
            self._connection, char_uuid, byref(_buffer), byref(_buffer_len)
        )

        pointer_type = POINTER(c_ubyte * _buffer_len.value)
        c_bytearray = cast(_buffer, pointer_type)

        value = bytearray(_buffer_len.value)
        for i in range(_buffer_len.value):
            value[i] = c_bytearray.contents[i]

        return bytes(value)

    async def read_gatt_descriptor(self, handle: int, **kwargs) -> bytearray:
        """Perform read operation on the specified GATT descriptor.

        Args:
            handle (int): The handle of the descriptor to read from.

        Returns:
            (bytearray) The read data.

        """
        raise NotImplementedError

    async def write_gatt_char(
        self,
        char_specifier: Union[BleakGATTCharacteristicBlueZDBus, int, str, UUID],
        data: bytearray,
        response: bool = False,
    ) -> None:
        """Perform a write operation on the specified GATT characteristic.

        .. note::

            The version check below is for the "type" option to the
            "Characteristic.WriteValue" method that was added to `Bluez in 5.51
            <https://git.kernel.org/pub/scm/bluetooth/bluez.git/commit?id=fa9473bcc48417d69cc9ef81d41a72b18e34a55a>`_
            Before that commit, ``Characteristic.WriteValue`` was only "Write with
            response". ``Characteristic.AcquireWrite`` was `added in Bluez 5.46
            <https://git.kernel.org/pub/scm/bluetooth/bluez.git/commit/doc/gatt-api.txt?id=f59f3dedb2c79a75e51a3a0d27e2ae06fefc603e>`_
            which can be used to "Write without response", but for older versions
            of Bluez, it is not possible to "Write without response".

        Args:
            char_specifier (BleakGATTCharacteristicBlueZDBus, int, str or UUID): The characteristic to write
                to, specified by either integer handle, UUID or directly by the
                BleakGATTCharacteristicBlueZDBus object representing it.
            data (bytes or bytearray): The data to send.
            response (bool): If write-with-response operation should be done. Defaults to `False`.

        """
        # if not isinstance(char_specifier, BleakGATTCharacteristicBlueZDBus):
        #     characteristic = self.services.get_characteristic(char_specifier)
        # else:
        #     characteristic = char_specifier

        if not isinstance(data, bytes) and not isinstance(data, bytearray):
            raise TypeError("Data must be of bytes type to know its size.")

        buffer_type = c_char * len(data)
        buffer = data
        buffer_len = len(data)

        if response:
            ret = gattlib_write_char_by_uuid(
                self._connection,
                char_specifier,
                buffer_type.from_buffer_copy(buffer),
                buffer_len,
            )
        else:
            ret = gattlib_write_without_response_char_by_uuid(
                self._connection,
                char_specifier,
                buffer_type.from_buffer_copy(buffer),
                buffer_len,
            )
        handle_return(ret)

    async def write_gatt_descriptor(self, handle: int, data: bytearray) -> None:
        """Perform a write operation on the specified GATT descriptor.

        Args:
            handle (int): The handle of the descriptor to read from.
            data (bytes or bytearray): The data to send.

        """
        raise NotImplementedError

    async def start_notify(
        self,
        char_specifier: Union[BleakGATTCharacteristicBlueZDBus, int, str, UUID],
        callback: Callable[[int, bytearray], None],
        **kwargs,
    ) -> None:
        """Activate notifications/indications on a characteristic.

        Callbacks must accept two inputs. The first will be a integer handle of the characteristic generating the
        data and the second will be a ``bytearray`` containing the data sent from the connected server.

        .. code-block:: python

            def callback(sender: int, data: bytearray):
                print(f"{sender}: {data}")
            client.start_notify(char_uuid, callback)

        Args:
            char_specifier (BleakGATTCharacteristicBlueZDBus, int, str or UUID): The characteristic to activate
                notifications/indications on a characteristic, specified by either integer handle,
                UUID or directly by the BleakGATTCharacteristicBlueZDBus object representing it.
            callback (function): The function to be called on notification.
        """
        if not isinstance(char_specifier, BleakGATTCharacteristicBlueZDBus):
            characteristic = self.services.get_characteristic(char_specifier)
        else:
            characteristic = char_specifier

        return True
        # raise NotImplementedError

    async def stop_notify(
        self,
        char_specifier: Union[BleakGATTCharacteristicBlueZDBus, int, str, UUID],
    ) -> None:
        """Deactivate notification/indication on a specified characteristic.

        Args:
            char_specifier (BleakGATTCharacteristicBlueZDBus, int, str or UUID): The characteristic to deactivate
                notification/indication on, specified by either integer handle, UUID or
                directly by the BleakGATTCharacteristicBlueZDBus object representing it.

        """
        if not isinstance(char_specifier, BleakGATTCharacteristicBlueZDBus):
            characteristic = self.services.get_characteristic(char_specifier)
        else:
            characteristic = char_specifier
        if not characteristic:
            raise BleakError("Characteristic {} not found!".format(char_specifier))

        raise NotImplementedError

    # Internal Callbacks

    def _parse_msg(self, message: Message):
        if message.message_type != MessageType.SIGNAL:
            return

        logger.debug(
            "received D-Bus signal: {0}.{1} ({2}): {3}".format(
                message.interface, message.member, message.path, message.body
            )
        )

        if message.member == "InterfacesAdded":
            path, interfaces = message.body

            if defs.GATT_SERVICE_INTERFACE in interfaces:
                obj = unpack_variants(interfaces[defs.GATT_SERVICE_INTERFACE])
                # if this assert fails, it means our match rules are probably wrong
                assert obj["Device"] == self._device_path
                self.services.add_service(BleakGATTServiceBlueZDBus(obj, path))

            if defs.GATT_CHARACTERISTIC_INTERFACE in interfaces:
                obj = unpack_variants(interfaces[defs.GATT_CHARACTERISTIC_INTERFACE])
                service = next(
                    x
                    for x in self.services.services.values()
                    if x.path == obj["Service"]
                )
                self.services.add_characteristic(
                    BleakGATTCharacteristicBlueZDBus(obj, path, service.uuid)
                )

            if defs.GATT_DESCRIPTOR_INTERFACE in interfaces:
                obj = unpack_variants(interfaces[defs.GATT_DESCRIPTOR_INTERFACE])
                handle = int(obj["Characteristic"][-4:], 16)
                characteristic = self.services.characteristics[handle]
                self.services.add_descriptor(
                    BleakGATTDescriptorBlueZDBus(obj, path, characteristic.uuid, handle)
                )
        elif message.member == "InterfacesRemoved":
            path, interfaces = message.body

        elif message.member == "PropertiesChanged":
            interface, changed, _ = message.body
            changed = unpack_variants(changed)

            if interface == defs.GATT_CHARACTERISTIC_INTERFACE:
                if message.path in self._notification_callbacks and "Value" in changed:
                    handle = int(message.path[-4:], 16)
                    self._notification_callbacks[message.path](handle, changed["Value"])
            elif interface == defs.DEVICE_INTERFACE:
                self._properties.update(changed)

                if "ServicesResolved" in changed:
                    if changed["ServicesResolved"]:
                        if self._services_resolved_event:
                            self._services_resolved_event.set()
                    else:
                        self._services_resolved = False

                if "Connected" in changed and not changed["Connected"]:
                    logger.debug(f"Device disconnected ({self._device_path})")

                    if self._disconnect_monitor_event:
                        self._disconnect_monitor_event.set()
                        self._disconnect_monitor_event = None

                    task = asyncio.get_event_loop().create_task(self._cleanup_all())
                    if self._disconnected_callback is not None:
                        task.add_done_callback(
                            lambda _: self._disconnected_callback(self)
                        )
                    if self._disconnecting_event:
                        task.add_done_callback(
                            lambda _: self._disconnecting_event.set()
                        )
