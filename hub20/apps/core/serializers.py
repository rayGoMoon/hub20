import copy
from typing import Dict, Type, Union

from django.contrib.auth import get_user_model
from django.db import transaction
from ipware import get_client_ip
from rest_framework import serializers
from rest_framework.reverse import reverse

from hub20.apps.blockchain.serializers import EthereumAddressField, HexadecimalField
from hub20.apps.ethereum_money.serializers import (
    CurrencyRelatedField,
    EthereumTokenSerializer,
    TokenValueField,
)

from . import models

User = get_user_model()


class UserRelatedField(serializers.SlugRelatedField):
    queryset = User.objects.filter(is_active=True)

    def __init__(self, *args, **kw):
        kw.setdefault("slug_field", "username")
        super().__init__(*args, **kw)


class TokenBalanceSerializer(serializers.Serializer):
    url = serializers.SerializerMethodField()
    amount = TokenValueField(read_only=True)
    currency = EthereumTokenSerializer(read_only=True)
    balance = serializers.CharField(source="formatted", read_only=True)

    def get_url(self, obj):
        return reverse(
            "hub20:balance-detail",
            kwargs={"code": obj.currency.code},
            request=self.context.get("request"),
        )


class TransferSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="hub20:transfer-detail")
    address = EthereumAddressField(write_only=True, required=False)
    recipient = UserRelatedField(write_only=True, required=False, allow_null=True)
    token = CurrencyRelatedField()
    target = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)

    def validate_recipient(self, value):
        request = self.context["request"]
        if value == request.user:
            raise serializers.ValidationError("You can not make a transfer to yourself")
        return value

    def validate(self, data):
        address = data.get("address")
        recipient = data.get("recipient")
        request = self.context["request"]

        if not address and not recipient:
            raise serializers.ValidationError(
                "Either one of recipient or address must be provided"
            )
        if address and recipient:
            raise serializers.ValidationError(
                "Choose recipient by address or username, but not both at the same time"
            )

        transfer_data = copy.copy(data)
        del transfer_data["address"]
        del transfer_data["recipient"]

        if recipient is not None:
            transfer_class = models.InternalTransfer
            transfer_data["receiver"] = recipient
        else:
            transfer_class = models.ExternalTransfer
            transfer_data["recipient_address"] = address

        transfer = transfer_class(sender=request.user, **transfer_data)

        try:
            transfer.verify_conditions()
        except models.TransferError as exc:
            raise serializers.ValidationError(str(exc))

        return transfer_data

    def create(self, validated_data):
        transfer_class: Union[Type[models.InternalTransfer], Type[models.ExternalTransfer]] = (
            models.InternalTransfer if "recipient" in validated_data else models.ExternalTransfer
        )
        request = self.context["request"]

        return transfer_class.objects.create(sender=request.user, **validated_data)

    class Meta:
        model = models.Transfer
        fields = (
            "url",
            "address",
            "recipient",
            "amount",
            "token",
            "memo",
            "identifier",
            "status",
            "target",
        )
        read_only_fields = ("recipient", "status", "target")


class PaymentRouteSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source="name", read_only=True)


class InternalPaymentRouteSerializer(PaymentRouteSerializer):
    class Meta:
        model = models.InternalPaymentRoute
        fields = read_only_fields = ("recipient", "type")


class BlockchainPaymentRouteSerializer(PaymentRouteSerializer):
    address = EthereumAddressField(source="account.address", read_only=True)
    network_id = serializers.IntegerField(source="order.chain_id", read_only=True)
    start_block = serializers.IntegerField(source="start_block_number", read_only=True)
    expiration_block = serializers.IntegerField(source="expiration_block_number", read_only=True)

    class Meta:
        model = models.BlockchainPaymentRoute
        fields = read_only_fields = (
            "address",
            "network_id",
            "start_block",
            "expiration_block",
            "type",
        )


class RaidenPaymentRouteSerializer(PaymentRouteSerializer):
    address = EthereumAddressField(source="raiden.address", read_only=True)

    class Meta:
        model = models.RaidenPaymentRoute
        fields = read_only_fields = ("address", "expiration_time", "identifier", "type")


class PaymentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField()
    url = serializers.HyperlinkedIdentityField(view_name="hub20:payments-detail")
    currency = EthereumTokenSerializer()
    identifier = serializers.CharField()
    confirmed = serializers.BooleanField(source="is_confirmed", read_only=True)

    class Meta:
        model = models.Payment
        fields = read_only_fields = (
            "id",
            "url",
            "created",
            "currency",
            "amount",
            "identifier",
            "confirmed",
        )


class InternalPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.InternalPayment
        fields = PaymentSerializer.Meta.fields + ("user", "memo")
        read_only_fields = PaymentSerializer.Meta.read_only_fields + ("user", "memo")


class BlockchainPaymentSerializer(PaymentSerializer):
    transaction = HexadecimalField(source="transaction.hash", read_only=True)
    block = serializers.IntegerField(source="transaction.block.number", read_only=True)

    class Meta:
        model = models.BlockchainPayment
        fields = PaymentSerializer.Meta.fields + ("transaction", "block")
        read_only_fields = PaymentSerializer.Meta.read_only_fields + ("transaction", "block")


class RaidenPaymentSerializer(PaymentSerializer):
    raiden = serializers.CharField(source="payment.channel.raiden.address")

    class Meta:
        model = models.RaidenPayment
        fields = PaymentSerializer.Meta.fields + ("raiden",)
        read_only_fields = PaymentSerializer.Meta.read_only_fields + ("raiden",)


class PaymentOrderSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="hub20:payment-order-detail")
    amount = TokenValueField()
    token = CurrencyRelatedField(source="currency")
    routes = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()

    def create(self, validated_data: Dict) -> models.PaymentOrder:
        request = self.context["request"]
        with transaction.atomic():
            return models.PaymentOrder.objects.create(user=request.user, **validated_data)

    def get_routes(self, obj):
        def get_route_serializer(route):
            return {
                models.InternalPaymentRoute: InternalPaymentRouteSerializer,
                models.BlockchainPaymentRoute: BlockchainPaymentRouteSerializer,
                models.RaidenPaymentRoute: RaidenPaymentRouteSerializer,
            }.get(type(route), PaymentRouteSerializer)(route, context=self.context)

        return [get_route_serializer(route).data for route in obj.routes.select_subclasses()]

    def get_payments(self, obj):
        def get_payment_serializer(payment):
            return {
                models.InternalPayment: InternalPaymentSerializer,
                models.BlockchainPayment: BlockchainPaymentSerializer,
                models.RaidenPayment: RaidenPaymentSerializer,
            }.get(type(payment), PaymentSerializer)(payment, context=self.context)

        return [get_payment_serializer(payment).data for payment in obj.payments]

    class Meta:
        model = models.PaymentOrder
        fields = ("url", "id", "amount", "token", "created", "routes", "payments", "status")
        read_only_fields = ("id", "created", "status")


class PaymentOrderReadSerializer(PaymentOrderSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="hub20:payment-order-detail")
    currency = EthereumTokenSerializer(read_only=True)


class CheckoutSerializer(PaymentOrderSerializer):
    store = serializers.PrimaryKeyRelatedField(queryset=models.Store.objects.all())
    voucher = serializers.SerializerMethodField()

    def validate(self, data):
        currency = data["currency"]
        store = data["store"]
        if currency not in store.accepted_currencies.all():
            raise serializers.ValidationError(f"{currency.code} is not accepted at {store.name}")

        return data

    def create(self, validated_data):
        request = self.context.get("request")
        client_ip, _ = get_client_ip(request)
        store = validated_data.pop("store")
        currency = validated_data["currency"]

        with transaction.atomic():
            return models.Checkout.objects.create(
                store=store,
                user=store.owner,
                requester_ip=client_ip,
                chain=currency.chain,
                **validated_data,
            )

    def get_voucher(self, obj):
        return obj.issue_voucher()

    class Meta:
        model = models.Checkout
        fields = PaymentOrderSerializer.Meta.fields + ("store", "external_identifier", "voucher",)
        read_only_fields = PaymentOrderSerializer.Meta.read_only_fields + ("voucher",)


class HttpCheckoutSerializer(CheckoutSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="hub20:checkout-detail")

    class Meta:
        model = models.Checkout
        fields = ("url",) + CheckoutSerializer.Meta.fields
        read_only_fields = CheckoutSerializer.Meta.read_only_fields


class StoreSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="hub20:store-detail")
    site_url = serializers.URLField(source="url")
    public_key = serializers.CharField(source="rsa.public_key_pem", read_only=True)
    accepted_currencies = CurrencyRelatedField(many=True)

    def create(self, validated_data):
        request = self.context.get("request")
        currencies = validated_data.pop("accepted_currencies", [])
        store = models.Store.objects.create(owner=request.user, **validated_data)
        store.accepted_currencies.set(currencies)
        return store

    class Meta:
        model = models.Store
        fields = ("id", "url", "name", "site_url", "public_key", "accepted_currencies")
        read_only_fields = ("id", "public_key")
