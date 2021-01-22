"""
Connect by BLEDevice
"""

import asyncio
import platform
import sys

from bleak import BleakClient, BleakScanner


async def print_services(mac_addr: str):
    device = await BleakScanner.find_device_by_address(mac_addr)
    if device:
        print(f"Connecting to {device}...")
        async with BleakClient(device) as client:
            print("Connected successfully.")
            svcs = await client.get_services()
            print("Services:")
            for s in svcs:
                print("\t%s" % s)
                for c in s.characteristics:
                    print("\t- %s" % c)
    else:
        print(f"Could not find {mac_addr}.")


# Commandline or hardcoded?
if len(sys.argv) > 1:
    mac_addr = sys.argv[1]
    print(f"Connecting to {mac_addr} (command line)")
else:
    mac_addr = (
        "24:71:89:cc:09:05"
        if platform.system() != "Darwin"
        else "B9EA5233-37EF-4DD6-87A8-2A875E821C46"
    )
    print(f"Connecting to {mac_addr} (hard coded)")

loop = asyncio.get_event_loop()
loop.run_until_complete(print_services(mac_addr))
