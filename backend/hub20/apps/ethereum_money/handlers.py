import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from eth_utils import from_wei

from hub20.apps.blockchain.models import Transaction, TransactionLog
from hub20.apps.ethereum_money.models import (
    BaseEthereumAccount,
    EthereumToken,
    EthereumTokenAmount,
)
from hub20.apps.ethereum_money.signals import account_deposit_received

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Transaction)
def on_transaction_mined_check_for_deposit(sender, **kw):
    tx = kw["instance"]
    if kw["created"]:

        accounts = BaseEthereumAccount.objects.select_subclasses()
        accounts_by_address = {account.address: account for account in accounts}

        if tx.to_address in accounts_by_address.keys():
            ETH = EthereumToken.ETH(tx.block.chain)
            eth_amount = EthereumTokenAmount(amount=from_wei(tx.value, "ether"), currency=ETH)
            account_deposit_received.send(
                sender=Transaction,
                account=accounts_by_address[tx.to_address],
                transaction=tx,
                amount=eth_amount,
            )


@receiver(post_save, sender=TransactionLog)
def on_transaction_event_check_for_token_transfer(sender, **kw):
    tx_log = kw["instance"]

    logger.info(f"Event log for {tx_log.transaction} created")
    if kw["created"]:
        tx = tx_log.transaction
        token = EthereumToken.ERC20tokens.filter(address=tx.to_address).first()

        if token is not None:
            recipient_address, transfer_amount = token._decode_transaction(tx)
            is_token_transfer = recipient_address is not None and transfer_amount is not None

            account = (
                BaseEthereumAccount.objects.select_subclasses()
                .filter(address=recipient_address)
                .first()
            )
            if is_token_transfer and account:
                account_deposit_received.send(
                    sender=Transaction, account=account, transaction=tx, amount=transfer_amount
                )


__all__ = [
    "on_transaction_mined_check_for_deposit",
    "on_transaction_event_check_for_token_transfer",
]
