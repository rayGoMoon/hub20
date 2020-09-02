from rest_framework import serializers

from hub20.apps.blockchain.client import get_web3
from hub20.apps.blockchain.serializers import HexadecimalField
from hub20.apps.ethereum_money.models import EthereumToken
from hub20.apps.ethereum_money.serializers import CurrencyRelatedField, TokenValueField

from .client.blockchain import get_service_token
from .models import Raiden, ServiceDeposit


class RaidenSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="raiden:raiden-detail", lookup_field="address"
    )

    class Meta:
        model = Raiden
        fields = ("address", "url")
        read_only_fields = ("address", "url")


class DepositSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="raiden:deposit-detail")
    raiden = serializers.HyperlinkedRelatedField(
        view_name="raiden:raiden-detail", queryset=Raiden.objects.all(), lookup_field="address",
    )
    amount = TokenValueField()
    currency = CurrencyRelatedField(queryset=None, read_only=True)
    transaction = HexadecimalField(source="transfer.transaction.hash", read_only=True)

    def create(self, validated_data):
        w3 = get_web3()
        service_token: EthereumToken = get_service_token(w3=w3)
        return self.Meta.model.objects.create(currency=service_token, **validated_data)

    class Meta:
        model = ServiceDeposit
        fields = ("url", "created", "raiden", "amount", "currency", "transaction")
        read_only_fields = ("url", "created", "raiden", "amount", "currency", "transaction")
