from __future__ import annotations

import logging
import os
import random
from decimal import Decimal
from typing import Any, List, Optional, Tuple, Union

import ethereum
from django.conf import settings
from django.db import models
from django.db.models import Max, Q, Sum
from eth_utils import to_checksum_address
from eth_wallet import Wallet
from ethereum.abi import ContractTranslator
from ethtoken.abi import EIP20_ABI
from model_utils.managers import QueryManager
from web3 import Web3
from web3.contract import Contract

from hub20.apps.blockchain.fields import EthereumAddressField, HexField
from hub20.apps.blockchain.models import Chain, Transaction

from .app_settings import HD_WALLET_MNEMONIC, HD_WALLET_ROOT_KEY, TRANSFER_GAS_LIMIT
from .typing import EthereumAccount_T

logger = logging.getLogger(__name__)


def get_max_fee(chain: Chain) -> EthereumTokenAmount:
    w3 = chain.get_web3()
    ETH = EthereumToken.ETH(chain=chain)

    gas_price = w3.eth.generateGasPrice()
    return ETH.from_wei(TRANSFER_GAS_LIMIT * gas_price)


def encode_transfer_data(recipient_address, amount: EthereumTokenAmount):
    translator = ContractTranslator(EIP20_ABI)
    encoded_data = translator.encode_function_call("transfer", (recipient_address, amount.as_wei))
    return f"0x{encoded_data.hex()}"


class EthereumToken(models.Model):
    NULL_ADDRESS = "0x0000000000000000000000000000000000000000"
    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, related_name="tokens")
    code = models.CharField(max_length=8)
    name = models.CharField(max_length=500)
    decimals = models.PositiveIntegerField(default=18)
    address = EthereumAddressField(default=NULL_ADDRESS)

    objects = models.Manager()
    ERC20tokens = QueryManager(~Q(address=NULL_ADDRESS))
    ethereum = QueryManager(address=NULL_ADDRESS)

    @property
    def is_ERC20(self) -> bool:
        return self.address != self.NULL_ADDRESS

    def __str__(self) -> str:
        components = [self.code]
        if self.is_ERC20:
            components.append(self.address)

        components.append(str(self.chain_id))
        return " - ".join(components)

    def get_contract(self, w3: Web3) -> Contract:
        if not self.is_ERC20:
            raise ValueError("Not an ERC20 token")

        return w3.eth.contract(abi=EIP20_ABI, address=self.address)

    def build_transfer_transaction(self, w3: Web3, sender, recipient, amount: EthereumTokenAmount):

        chain_id = int(w3.net.version)
        message = f"Web3 client is on network {chain_id}, token {self.code} is on {self.chain_id}"
        assert self.chain_id == chain_id, message

        transaction_params = {
            "chainId": chain_id,
            "nonce": w3.eth.getTransactionCount(sender),
            "gasPrice": w3.eth.generateGasPrice(),
            "gas": TRANSFER_GAS_LIMIT,
            "from": sender,
        }

        if self.is_ERC20:
            transaction_params.update(
                {"to": self.address, "value": 0, "data": encode_transfer_data(recipient, amount)}
            )
        else:
            transaction_params.update({"to": recipient, "value": amount.as_wei})
        return transaction_params

    def _decode_transaction_data(self, tx_data, contract: Optional[Contract] = None) -> Tuple:
        if not self.is_ERC20:
            return tx_data.to, self.from_wei(tx_data.value)

        try:
            assert tx_data["to"] == self.address, f"Not a {self.code} transaction"
            assert contract is not None, f"{self.code} contract interface required to decode tx"

            fn, args = contract.decode_function_input(tx_data.input)

            # TODO: is this really the best way to identify the transaction as a value transfer?
            transfer_idenfifier = contract.functions.transfer.function_identifier
            assert transfer_idenfifier == fn.function_identifier, "No transfer transaction"

            return args["_to"], self.from_wei(args["_value"])
        except AssertionError as exc:
            logger.warning(exc)
            return None, None
        except Exception as exc:
            logger.warning(exc)
            return None, None

    def _decode_transaction(self, transaction: Transaction) -> Tuple:
        # A transfer transaction input is 'function,address,uint256'
        # i.e, 16 bytes + 20 bytes + 32 bytes = hex string of length 136
        try:
            # transaction input strings are '0x', so we they should be 138 chars long
            assert len(transaction.data) == 138, "Not a ERC20 transfer transaction"
            assert transaction.logs.count() == 1, "Transaction does not contain log changes"

            recipient_address = to_checksum_address(transaction.data[-104:-64])

            wei_transferred = int(transaction.data[-64:], 16)
            tx_log = transaction.logs.first()

            assert int(tx_log.data, 16) == wei_transferred, "Log data and tx amount do not match"

            return recipient_address, self.from_wei(wei_transferred)
        except AssertionError as exc:
            logger.info(f"Failed to get transfer data from transaction: {exc}")
            return None, None
        except ValueError:
            logger.info(f"Failed to extract transfer amounts from {transaction.hash.hex()}")
            return None, None
        except Exception as exc:
            logger.exception(exc)
            return None, None

    def from_wei(self, wei_amount: int) -> EthereumTokenAmount:
        value = Decimal(wei_amount) / (10 ** self.decimals)
        return EthereumTokenAmount(amount=value, currency=self)

    @staticmethod
    def ETH(chain: Chain):
        eth, _ = EthereumToken.objects.get_or_create(
            chain=chain, code="ETH", defaults={"name": "Ethereum"}
        )
        return eth

    @classmethod
    def make(cls, address: str, chain: Chain, **defaults):
        if address == EthereumToken.NULL_ADDRESS:
            return EthereumToken.ETH(chain)

        obj, _ = cls.objects.update_or_create(address=address, chain=chain, defaults=defaults)
        return obj

    class Meta:
        unique_together = (("chain", "address"),)


class EthereumTokenAmountField(models.DecimalField):
    def __init__(self, *args: Any, **kw: Any) -> None:
        kw.setdefault("decimal_places", 18)
        kw.setdefault("max_digits", 32)

        super().__init__(*args, **kw)


class EthereumTokenValueModel(models.Model):
    amount = EthereumTokenAmountField()
    currency = models.ForeignKey(EthereumToken, on_delete=models.PROTECT)

    @property
    def as_token_amount(self):
        return EthereumTokenAmount(amount=self.amount, currency=self.currency)

    @property
    def formatted_amount(self):
        return self.as_token_amount.formatted

    class Meta:
        abstract = True


class AbstractEthereumAccount(models.Model):
    address = EthereumAddressField(unique=True, db_index=True)

    def send(self, recipient_address, transfer_amount: EthereumTokenAmount, *args, **kw) -> str:
        chain = transfer_amount.currency.chain
        w3 = chain.get_web3()
        transaction_data = transfer_amount.currency.build_transfer_transaction(
            w3=w3, sender=self.address, recipient=recipient_address, amount=transfer_amount
        )
        signed_tx = self.sign_transaction(w3=w3, transaction_data=transaction_data)
        return w3.eth.sendRawTransaction(signed_tx.rawTransaction)

    def sign_transaction(self, w3: Web3, transaction_data, *args, **kw):
        if not hasattr(self, "private_key"):
            raise NotImplementedError("Can not sign transaction without the private key")
        return w3.eth.account.signTransaction(transaction_data, self.private_key)

    def get_balance(self, currency: EthereumToken) -> EthereumTokenAmount:
        return EthereumTokenAmount.aggregated(self.balance_entries.all(), currency=currency)

    def get_balances(self, chain: Chain) -> List[EthereumTokenAmount]:
        return [self.get_balance(token) for token in EthereumToken.objects.filter(chain=chain)]

    @classmethod
    def select_for_transfer(cls, amount: EthereumTokenAmount) -> Optional[EthereumAccount_T]:
        max_fee_amount: EthereumTokenAmount = get_max_fee(chain=amount.currency.chain)
        assert max_fee_amount.is_ETH

        ETH = max_fee_amount.currency

        eth_required = max_fee_amount
        token_required = EthereumTokenAmount(amount=amount.amount, currency=amount.currency)
        accounts = cls.objects.all()

        if amount.is_ETH:
            token_required += eth_required
            funded_accounts = [
                account for account in accounts if account.get_balance(ETH) >= token_required
            ]
        else:
            funded_accounts = [
                account
                for account in accounts
                if account.get_balance(token_required.currency) >= token_required
                and account.get_balance(ETH) >= eth_required
            ]

        try:
            return random.choice(funded_accounts)
        except IndexError:
            return None

    class Meta:
        abstract = True


class KeystoreAccount(AbstractEthereumAccount):
    private_key = HexField(max_length=64, unique=True)

    @classmethod
    def generate(cls):
        private_key = os.urandom(32)
        address = ethereum.utils.privtoaddr(private_key)
        checksum_address = ethereum.utils.checksum_encode(address.hex())
        return cls.objects.create(address=checksum_address, private_key=private_key.hex())


class HierarchicalDeterministicWallet(AbstractEthereumAccount):
    BASE_PATH_FORMAT = "m/44'/60'/0'/0/{index}"

    index = models.PositiveIntegerField(unique=True)

    @property
    def private_key(self):
        wallet = self.__class__.get_wallet(index=self.index)
        return wallet.private_key()

    @classmethod
    def get_wallet(cls, index: int) -> Wallet:
        wallet = Wallet()

        if HD_WALLET_MNEMONIC:
            wallet.from_mnemonic(mnemonic=HD_WALLET_MNEMONIC)
        elif HD_WALLET_ROOT_KEY:
            wallet.from_root_private_key(root_private_key=HD_WALLET_ROOT_KEY)
        else:
            raise ValueError("Can not generate new addresses for HD Wallets. No seed available")

        wallet.from_path(cls.BASE_PATH_FORMAT.format(index=index))
        return wallet

    @classmethod
    def generate(cls):

        index = cls.objects.aggregate(generation=Max("index")).get("generation") or 0
        wallet = HierarchicalDeterministicWallet.get_wallet(index)
        return cls.objects.create(index=index, address=wallet.address())


class AccountBalanceEntry(EthereumTokenValueModel):
    account = models.ForeignKey(
        settings.ETHEREUM_ACCOUNT_MODEL, on_delete=models.CASCADE, related_name="balance_entries"
    )
    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE)


class EthereumTokenAmount:
    def __init__(self, amount: Union[int, str, Decimal], currency: EthereumToken):
        self.amount: Decimal = Decimal(amount)
        self.currency: EthereumToken = currency

    @property
    def formatted(self):
        return f"{self.amount} {self.currency.code}"

    @property
    def as_wei(self) -> int:
        return int(self.amount * (10 ** self.currency.decimals))

    @property
    def as_hex(self) -> str:
        return hex(self.as_wei)

    @property
    def is_ETH(self) -> bool:
        return self.currency.address == EthereumToken.NULL_ADDRESS

    def _check_currency_type(self, other: EthereumTokenAmount):
        if not self.currency == other.currency:
            raise ValueError(f"Can not operate {self.currency} and {other.currency}")

    def __add__(self, other: EthereumTokenAmount) -> EthereumTokenAmount:
        self._check_currency_type(self)
        return self.__class__(self.amount + other.amount, self.currency)

    def __mul__(self, other: Union[int, Decimal]) -> EthereumTokenAmount:
        return EthereumTokenAmount(amount=Decimal(other * self.amount), currency=self.currency)

    def __rmul__(self, other: Union[int, Decimal]) -> EthereumTokenAmount:
        return self.__mul__(other)

    def __eq__(self, other: object) -> bool:
        message = f"Can not compare {self.currency} amount with {type(other)}"
        assert isinstance(other, EthereumTokenAmount), message

        return self.currency == other.currency and self.amount == other.amount

    def __lt__(self, other: EthereumTokenAmount):
        self._check_currency_type(other)
        return self.amount < other.amount

    def __le__(self, other: EthereumTokenAmount):
        self._check_currency_type(other)
        return self.amount <= other.amount

    def __gt__(self, other: EthereumTokenAmount):
        self._check_currency_type(other)
        return self.amount > other.amount

    def __ge__(self, other: EthereumTokenAmount):
        self._check_currency_type(other)
        return self.amount >= other.amount

    def __str__(self):
        return self.formatted

    def __repr__(self):
        return self.formatted

    @classmethod
    def aggregated(cls, queryset, currency: EthereumToken):
        entries = queryset.filter(currency=currency)
        amount = entries.aggregate(total=Sum("amount")).get("total") or Decimal(0)
        return cls(amount=amount, currency=currency)


__all__ = [
    "EthereumToken",
    "EthereumTokenAmount",
    "EthereumTokenValueModel",
    "KeystoreAccount",
    "HierarchicalDeterministicWallet",
    "AccountBalanceEntry",
    "get_max_fee",
    "encode_transfer_data",
]
