from django.db.models import Q

from ...account import models
from ..utils import filter_by_query_param

USER_SEARCH_FIELDS = (
    'email', 'default_shipping_address__first_name',
    'default_shipping_address__last_name', 'default_shipping_address__city',
    'default_shipping_address__country')


def resolve_customers(info, query):
    qs = models.User.objects.filter(
        Q(is_staff=False) | (Q(is_staff=True) & Q(orders__isnull=False))
    ).prefetch_related('addresses')
    return filter_by_query_param(
        queryset=qs, query=query, search_fields=USER_SEARCH_FIELDS)


def resolve_staff_users(info, query):
    qs = models.User.objects.filter(is_staff=True)
    return filter_by_query_param(
        queryset=qs, query=query, search_fields=USER_SEARCH_FIELDS)
