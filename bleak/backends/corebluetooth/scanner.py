import asyncio
import logging
import pathlib
from typing import Callable, Union, List

from bleak.backends.corebluetooth.CentralManagerDelegate import \
    CentralManagerDelegate
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import BaseBleakScanner
from bleak.exc import BleakError

logger = logging.getLogger(__name__)
_here = pathlib.Path(__file__).parent


class BleakScannerCoreBluetooth(BaseBleakScanner):
    """The native macOS Bleak BLE Scanner.

    Documentation:
    https://developer.apple.com/documentation/corebluetooth/cbcentralmanager

    CoreBluetooth doesn't explicitly use Bluetooth addresses to identify peripheral
    devices because private devices may obscure their Bluetooth addresses. To cope
    with this, CoreBluetooth utilizes UUIDs for each peripheral. Bleak uses
    this for the BLEDevice address on macOS.

    Keyword Args:
        timeout (double): The scanning timeout to be used, in case of missing
          ``stopScan_`` method.

    """

    def __init__(self, **kwargs):
        super(BleakScannerCoreBluetooth, self).__init__(**kwargs)
        self._callback = None
        self._identifiers = None
        self._manager = CentralManagerDelegate.alloc().init()
        self._timeout = kwargs.get("timeout", 5.0)

    async def start(self):
        try:
            await self._manager.wait_for_powered_on(0.1)
        except asyncio.TimeoutError:
            raise BleakError("Bluetooth device is turned off")

        self._identifiers = {}

        def safe_list_get(list: list, idx: int, default):
            """
            Returns an index from a list safely similar to .get() with dicts
            """
            try:
                return repr(list[idx]).lower()
            except IndexError:
                return default

        def callback_dict_breakdown(data) -> Union[None, dict]:
            """
            This function parses the __NSDictionaryM or __NSSingleEntryDictionry
            I object passed as data into a more user-friendly dictionary which
            is returned, if data is None, or if an exception is raised during
            parsing, None is returned
            """
            if data:
                try:
                    _service_dict_key = data.allKeys()[0]
                    _service_dict_data = data.objectForKey_(_service_dict_key)
                    return {str(_service_dict_key): str(_service_dict_data)}
                except Exception:
                    return None
            else:
                return None

        def callback(p, a, r):
            self._identifiers[p.identifier()] = a

            service_data_dict = a.get("kCBAdvDataServiceData", {})
            apple_manufacturer_dict = a.get("kCBAdvDataAppleMfgData", {})
            manufacturer_data_str = str(
                a.get("kCBAdvDataManufacturerData", ""))

            callback_data = {"uuid": p.identifier().UUIDString(),
                             "name": p.name(),
                             "rssi": r,
                             "advertisement_data":
                                 {
                                     "data_channel_connectable": a.get(
                                         "kCBAdvDataIsConnectable"),
                                     "data_channel": a.get(
                                         "kCBAdvDataChannel"),
                                     "manufacturer_data": manufacturer_data_str,
                                     "data_tx_power_level": a.get(
                                         "kCBAdvDataTxPowerLevel"),
                                     "apple_manufacturer_data": callback_dict_breakdown(
                                         apple_manufacturer_dict),
                                     "local_name": a.get(
                                         "kCBAdvDataLocalName"),
                                     "service_data": callback_dict_breakdown(
                                         service_data_dict),
                                     "service_uuid": safe_list_get(
                                         a.get("kCBAdvDataServiceUUIDs", []),
                                         0, None)
                                 }
                             }

            try:
                self._callback(p, a, r, callback_data)
            except:
                self._callback(p, a, r)

        self._manager.callbacks[id(self)] = callback
        self._manager.start_scan({})

    async def stop(self):
        del self._manager.callbacks[id(self)]
        try:
            await self._manager.stop_scan()
        except Exception as e:
            logger.warning("stopScan method could not be called: {0}".format(e))

    async def set_scanning_filter(self, **kwargs):
        """Set scanning filter for the scanner.

        .. note::

            This is not implemented for macOS yet.

        Raises:

           ``NotImplementedError``

        """
        raise NotImplementedError(
            "Need to evaluate which macOS versions to support first..."
        )

    async def get_discovered_devices(self) -> List[BLEDevice]:
        found = []
        peripherals = self._manager.central_manager.retrievePeripheralsWithIdentifiers_(
            self._identifiers.keys(),
        )

        for i, peripheral in enumerate(peripherals):
            address = peripheral.identifier().UUIDString()
            name = peripheral.name() or "Unknown"
            details = peripheral

            advertisementData = self._identifiers[peripheral.identifier()]
            manufacturer_binary_data = advertisementData.get(
                "kCBAdvDataManufacturerData"
            )
            manufacturer_data = {}
            if manufacturer_binary_data:
                manufacturer_id = int.from_bytes(
                    manufacturer_binary_data[0:2], byteorder="little"
                )
                manufacturer_value = bytes(manufacturer_binary_data[2:])
                manufacturer_data = {manufacturer_id: manufacturer_value}

            uuids = [
                # converting to lower case to match other platforms
                str(u).lower()
                for u in advertisementData.get("kCBAdvDataServiceUUIDs", [])
            ]

            found.append(
                BLEDevice(
                    address,
                    name,
                    details,
                    uuids=uuids,
                    manufacturer_data=manufacturer_data,
                )
            )

        return found

    def register_detection_callback(self, callback: Callable):
        """Set a function to act as callback on discovered devices or devices with changed properties.

        Args:
            callback: Function accepting three arguments:
             peripheral
             advertisementData
             rssi

        """
        self._callback = callback

    @classmethod
    async def find_device_by_address(
        cls, device_identifier: str, timeout: float = 10.0, **kwargs
    ) -> Union[BLEDevice, None]:
        """A convenience method for obtaining a ``BLEDevice`` object specified by macOS UUID address.

        Args:
            device_identifier (str): The Bluetooth address of the Bluetooth peripheral.
            timeout (float): Optional timeout to wait for detection of specified peripheral before giving up. Defaults to 10.0 seconds.

        Returns:
            The ``BLEDevice`` sought or ``None`` if not detected.

        """
        loop = asyncio.get_event_loop()
        stop_scanning_event = asyncio.Event()
        device_identifier = device_identifier.lower()
        scanner = cls(timeout=timeout)

        def stop_if_detected(peripheral, advertisement_data, rssi):
            if str(peripheral.identifier().UUIDString()).lower() == device_identifier:
                loop.call_soon_threadsafe(stop_scanning_event.set)

        return await scanner._find_device_by_address(
            device_identifier, stop_scanning_event, stop_if_detected, timeout
        )

    # macOS specific methods

    @property
    def is_scanning(self):
        # TODO: Evaluate if newer macOS than 10.11 has isScanning.
        try:
            return self._manager.isScanning_
        except:
            return None
