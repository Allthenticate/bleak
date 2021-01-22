"""
Bleak Scanner
-------------



Updated on 2020-08-12 by hbldh <henrik.blidh@nedomkull.com>
Updated on 2021-01-19 by cspensky <chad@allthenticate.net>

"""

import asyncio
import platform
import sys

from bleak import BleakScanner


# Commandline or hardcoded?
if len(sys.argv) > 1:
    address = sys.argv[1]
    print(f"Scanning for {sys.argv[1]} (command line)")
else:
    address = (
        "24:71:89:cc:09:05"  # <--- Change to your device's address here if you are using Windows or Linux
        if platform.system() != "Darwin"
        else "B9EA5233-37EF-4DD6-87A8-2A875E821C46"  # <--- Change to your device's address here if you are using macOS
    )
    print(f"Scanning for {sys.argv[1]} (hard coded)")


async def run():
    device = await BleakScanner.find_device_by_address(address)
    print(device)


loop = asyncio.get_event_loop()
loop.run_until_complete(run())
