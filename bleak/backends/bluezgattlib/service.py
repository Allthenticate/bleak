from typing import List

from bleak.backends.bluezgattlib.uuid import gattlib_uuid_to_uuid
from bleak.backends.service import BleakGATTService
from bleak.backends.bluezdbus.characteristic import BleakGATTCharacteristicBlueZDBus


class BleakGATTServiceBlueZGattlib(BleakGATTService):
    """GATT Service implementation for the BlueZ DBus backend"""

    def __init__(self, obj):
        super().__init__(obj)
        self._primary_service = obj
        self.__characteristics = []

    @property
    def uuid(self) -> str:
        """The UUID to this service"""
        return str(gattlib_uuid_to_uuid(self._primary_service.uuid))

    @property
    def characteristics(self) -> List[BleakGATTCharacteristicBlueZDBus]:
        """List of characteristics for this service"""
        return self.__characteristics

    def add_characteristic(
        self, characteristic: BleakGATTCharacteristicBlueZDBus
    ) -> bool:
        """Add a :py:class:`~BleakGATTCharacteristicBlueZDBus` to the service.

        Should not be used by end user, but rather by `bleak` itself.
        """
        if (
            self._primary_service.attr_handle_start
            <= characteristic.handle
            <= self._primary_service.attr_handle_end
        ):
            self.__characteristics.append(characteristic)
            return True
        else:
            return False
