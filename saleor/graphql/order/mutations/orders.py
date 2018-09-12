import graphene
from django.utils.translation import pgettext_lazy
from graphql_jwt.decorators import permission_required
from payments import PaymentError, PaymentStatus

from ....order import CustomPaymentChoices, OrderEvents, models
from ....order.utils import cancel_order
from ...account.types import AddressInput
from ...core.mutations import BaseMutation
from ...core.types.common import Decimal, Error
from ...order.mutations.draft_orders import DraftOrderUpdate
from ...order.types import Order, OrderEvent
from ...shipping.types import ShippingMethod
from ....shipping.models import ShippingMethod as ShippingMethodModel
from ....core.utils.taxes import ZERO_TAXED_MONEY


def try_payment_action(action, money, errors):
    try:
        action(money)
    except (PaymentError, ValueError) as e:
        errors.append(Error(field='payment', message=str(e)))


def clean_release_payment(payment, errors):
    """Check for payment errors."""
    if payment.status != PaymentStatus.PREAUTH:
        errors.append(
            Error(field='payment',
                  message='Only pre-authorized payments can be released'))
    try:
        payment.release()
    except (PaymentError, ValueError) as e:
        errors.append(Error(field='payment', message=str(e)))
    return errors


def clean_refund_payment(payment, amount, errors):
    if payment.variant == CustomPaymentChoices.MANUAL:
        errors.append(
            Error(field='payment',
                  message='Manual payments can not be refunded.'))
    try_payment_action(payment.refund, amount, errors)
    return errors


class OrderUpdateInput(graphene.InputObjectType):
    billing_address = AddressInput(
        description='Billing address of the customer.')
    user_email = graphene.String(description='Email address of the customer.')
    shipping_address = AddressInput(
        description='Shipping address of the customer.')


class OrderUpdate(DraftOrderUpdate):
    class Arguments:
        id = graphene.ID(
            required=True, description='ID of an order to update.')
        input = OrderUpdateInput(
            required=True,
            description='Fields required to update an order.')

    class Meta:
        description = 'Updates an order.'
        model = models.Order


class OrderUpdateShippingInput(graphene.InputObjectType):
    shipping_method = graphene.ID(
        description='ID of the selected shipping method.',
        name='shippingMethod')


class OrderUpdateShipping(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True, name='order',
            description='ID of the order to update a shipping method.')
        input = OrderUpdateShippingInput(
            description='Fields required to change '
                        'shipping method of the order.')

    class Meta:
        description = 'Updates a shipping method of the order.'

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id, input):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)

        if not input['shipping_method']:
            if order.is_shipping_required():
                cls.add_error(
                    errors, 'shippingMethod',
                    'Shipping method is required for this order.')
                return OrderUpdateShipping(errors=errors)
            order.shipping_method = None
            order.shipping_price = ZERO_TAXED_MONEY
            order.shipping_method_name = None
            order.save()
            return OrderUpdateShipping(order=order, shipping_method=None)

        method = cls.get_node_or_error(
            info, input['shipping_method'], errors,
            'shipping_method', ShippingMethod)
        if errors:
            return OrderUpdateShipping(errors=errors)

        if not order.shipping_address:
            cls.add_error(
                errors, 'order',
                'Cannot choose a shipping method for an '
                'order without the shipping address.')
            return OrderUpdateShipping(errors=errors)

        valid_methods = (
            ShippingMethodModel.objects.applicable_shipping_methods(
                price=order.get_subtotal().gross.amount,
                weight=order.get_total_weight(),
                country_code=order.shipping_address.country.code))
        valid_methods = valid_methods.values_list('id', flat=True)
        if method.pk not in valid_methods:
            cls.add_error(
                errors, 'shippingMethod',
                'Shipping method cannot be used with this order.')
            return OrderUpdateShipping(errors=errors)

        order.shipping_method = method
        order.shipping_price = method.get_total_price(info.context.taxes)
        order.shipping_method_name = method.name
        order.save()
        return OrderUpdateShipping(order=order, shipping_method=method)

    order = graphene.Field(
        Order, description='Order with updated shipping method.')
    shipping_method = graphene.Field(
        ShippingMethod, description='Shipping method assigned to the order.')


class OrderAddNoteInput(graphene.InputObjectType):
    message = graphene.String(description='Note message.', name='message')


class OrderAddNote(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True,
            description='ID of the order to add a note for.', name='order')
        input = OrderAddNoteInput(
            required=True,
            description='Fields required to create a note for the order.')

    class Meta:
        description = 'Adds note to the order.'

    order = graphene.Field(
        Order, description='Order with the note added.')
    event = graphene.Field(
        OrderEvent, description='Order note created.')

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id, input):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)
        if errors:
            return OrderAddNote(errors=errors)

        event = order.events.create(
            type=OrderEvents.NOTE_ADDED.value,
            user=info.context.user,
            parameters={
                'message': input['message']})
        return OrderAddNote(order=order, event=event)


class OrderCancel(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True, description='ID of the order to cancel.')
        restock = graphene.Boolean(
            required=True,
            description='Determine if lines will be restocked or not.')

    class Meta:
        description = 'Cancel an order.'

    order = graphene.Field(
        Order, description='Canceled order.')

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id, restock):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)
        if errors:
            return OrderCancel(errors=errors)

        cancel_order(order=order, restock=restock)
        if restock:
            order.events.create(
                type=OrderEvents.FULFILLMENT_RESTOCKED_ITEMS.value,
                user=info.context.user,
                parameters={'quantity': order.get_total_quantity()})
        else:
            order.events.create(
                type=OrderEvents.ORDER_CANCELED.value,
                user=info.context.user)
        return OrderCancel(order=order)


class OrderMarkAsPaid(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True, description='ID of the order to mark paid.')

    class Meta:
        description = 'Mark order as manually paid.'

    order = graphene.Field(
        Order, description='Order marked as paid.')

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)
        if order:
            if order.payments.exists():
                cls.add_error(
                    errors, 'payment',
                    'Orders with payments can not be manually marked as paid.')

        if errors:
            return OrderMarkAsPaid(errors=errors)

        defaults = {
            'total': order.total.gross.amount,
            'tax': order.total.tax.amount,
            'currency': order.total.currency,
            'delivery': order.shipping_price.net.amount,
            'description': pgettext_lazy(
                'Payment description', 'Order %(order)s') % {'order': order},
            'captured_amount': order.total.gross.amount}
        models.Payment.objects.get_or_create(
            variant=CustomPaymentChoices.MANUAL,
            status=PaymentStatus.CONFIRMED, order=order,
            defaults=defaults)

        order.events.create(
            type=OrderEvents.ORDER_MARKED_AS_PAID.value,
            user=info.context.user)
        return OrderMarkAsPaid(order=order)


class OrderCapture(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True, description='ID of the order to capture.')
        amount = Decimal(
            required=True, description='Amount of money to capture.')

    class Meta:
        description = 'Capture an order.'

    order = graphene.Field(
        Order, description='Captured order.')

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id, amount):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)
        if order:
            payment = order.get_last_payment()
            try_payment_action(payment.capture, amount, errors)

        if errors:
            return OrderCapture(errors=errors)

        order.events.create(
            parameters={'amount': amount},
            type=OrderEvents.PAYMENT_CAPTURED.value,
            user=info.context.user)
        return OrderCapture(order=order)


class OrderRelease(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True, description='ID of the order to release.')

    class Meta:
        description = 'Release an order.'

    order = graphene.Field(
        Order, description='A released order.')

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)
        if order:
            payment = order.get_last_payment()
            clean_release_payment(payment, errors)

        if errors:
            return OrderRelease(errors=errors)

        order.events.create(
            type=OrderEvents.PAYMENT_RELEASED.value,
            user=info.context.user)
        return OrderRelease(order=order)


class OrderRefund(BaseMutation):
    class Arguments:
        id = graphene.ID(
            required=True, description='ID of the order to refund.')
        amount = Decimal(
            required=True, description='Amount of money to refund.')

    class Meta:
        description = 'Refund an order.'

    order = graphene.Field(
        Order, description='A refunded order.')

    @classmethod
    @permission_required('order.manage_orders')
    def mutate(cls, root, info, id, amount):
        errors = []
        order = cls.get_node_or_error(info, id, errors, 'id', Order)
        if order:
            payment = order.get_last_payment()
            clean_refund_payment(payment, amount, errors)

        if errors:
            return OrderRefund(errors=errors)

        order.events.create(
            type=OrderEvents.PAYMENT_REFUNDED.value,
            user=info.context.user,
            parameters={'amount': amount})
        return OrderRefund(order=order)
