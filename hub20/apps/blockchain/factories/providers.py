import decimal
import os
import random

import ethereum
from faker.providers import BaseProvider
from hexbytes import HexBytes


class EthereumProvider(BaseProvider):
    def ethereum_address(self):
        private_key = os.urandom(32)
        address = ethereum.utils.privtoaddr(private_key)
        return ethereum.utils.checksum_encode(address)

    def hex64(self):
        return HexBytes(f"0x{os.urandom(32).hex()}")

    def uint256(self):
        return decimal.Decimal(random.randint(1, 2 ** 256))
