from decimal import Decimal
from typing import TYPE_CHECKING, Any, List, Union

from django.conf import settings
from django.core.exceptions import ValidationError
from django_countries.fields import Country
from django_prices_vatlayer.utils import (
    fetch_rate_types,
    fetch_rates,
    get_tax_rate_types,
)
from prices import Money, MoneyRange, TaxedMoney, TaxedMoneyRange

from ...checkout import calculations
from ...core.taxes import TaxType
from ...graphql.core.utils.error_codes import PluginErrorCode
from ..base_plugin import BasePlugin, ConfigurationTypeField
from . import (
    DEFAULT_TAX_RATE_NAME,
    TaxRateType,
    VatlayerConfiguration,
    apply_tax_to_price,
    get_taxed_shipping_price,
    get_taxes_for_country,
)

if TYPE_CHECKING:
    # flake8: noqa
    from ...checkout.models import Checkout, CheckoutLine
    from ...discount import DiscountInfo
    from ...product.models import Product, ProductType
    from ...account.models import Address
    from ...order.models import OrderLine, Order
    from ..models import PluginConfiguration


class VatlayerPlugin(BasePlugin):
    PLUGIN_ID = "mirumee.taxes.vatlayer"
    PLUGIN_NAME = "Vatlayer"
    META_CODE_KEY = "vatlayer.code"
    META_DESCRIPTION_KEY = "vatlayer.description"
    DEFAULT_CONFIGURATION = [{"name": "Access key", "value": None}]
    CONFIG_STRUCTURE = {
        "Access key": {
            "type": ConfigurationTypeField.PASSWORD,
            "help_text": "Required to authenticate to Vatlayer API.",
            "label": "Access key",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Convert to dict to easier take config elements
        configuration = {item["name"]: item["value"] for item in self.configuration}
        self.config = VatlayerConfiguration(access_key=configuration["Access key"])
        self._cached_taxes = {}

    def _skip_plugin(self, previous_value: Union[TaxedMoney, TaxedMoneyRange]) -> bool:
        if not self.active or not self.config.access_key:
            return True

        # The previous plugin already calculated taxes so we can skip our logic
        if isinstance(previous_value, TaxedMoneyRange):
            start = previous_value.start
            stop = previous_value.stop

            return start.net != start.gross and stop.net != stop.gross

        if isinstance(previous_value, TaxedMoney):
            return previous_value.net != previous_value.gross
        return False

    def calculate_checkout_total(
        self,
        checkout: "Checkout",
        lines: List["CheckoutLine"],
        discounts: List["DiscountInfo"],
        previous_value: TaxedMoney,
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        return (
            calculations.checkout_subtotal(
                checkout=checkout, lines=lines, discounts=discounts
            )
            + calculations.checkout_shipping_price(
                checkout=checkout, lines=lines, discounts=discounts
            )
            - checkout.discount
        )

    def _get_taxes_for_country(self, country: Country):
        """Try to fetch cached taxes on the plugin level.

        If the plugin doesn't have cached taxes for a given country it will fetch it
        from cache or db.
        """
        if not country:
            country = Country(settings.DEFAULT_COUNTRY)
        country_code = country.code
        if country_code in self._cached_taxes:
            return self._cached_taxes[country_code]
        taxes = get_taxes_for_country(country)
        self._cached_taxes[country_code] = taxes
        return taxes

    def calculate_checkout_shipping(
        self,
        checkout: "Checkout",
        lines: List["CheckoutLine"],
        discounts: List["DiscountInfo"],
        previous_value: TaxedMoney,
    ) -> TaxedMoney:
        """Calculate shipping gross for checkout."""
        if self._skip_plugin(previous_value):
            return previous_value

        address = checkout.shipping_address or checkout.billing_address
        taxes = None
        if address:
            taxes = self._get_taxes_for_country(address.country)
        if not checkout.shipping_method:
            return previous_value

        return get_taxed_shipping_price(checkout.shipping_method.price, taxes)

    def calculate_order_shipping(
        self, order: "Order", previous_value: TaxedMoney
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        address = order.shipping_address or order.billing_address
        taxes = None
        if address:
            taxes = self._get_taxes_for_country(address.country)
        if not order.shipping_method:
            return previous_value
        return get_taxed_shipping_price(order.shipping_method.price, taxes)

    def calculate_checkout_line_total(
        self,
        checkout_line: "CheckoutLine",
        discounts: List["DiscountInfo"],
        previous_value: TaxedMoney,
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        address = (
            checkout_line.checkout.shipping_address
            or checkout_line.checkout.billing_address
        )
        price = checkout_line.variant.get_price(discounts)
        country = address.country if address else None
        return (
            self.__apply_taxes_to_product(checkout_line.variant.product, price, country)
            * checkout_line.quantity
        )

    def calculate_order_line_unit(
        self, order_line: "OrderLine", previous_value: TaxedMoney
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        address = order_line.order.shipping_address or order_line.order.billing_address
        country = address.country if address else None
        variant = order_line.variant
        if not variant:
            return previous_value
        return self.__apply_taxes_to_product(
            variant.product, order_line.unit_price, country
        )

    def get_tax_rate_type_choices(
        self, previous_value: List["TaxType"]
    ) -> List["TaxType"]:
        if not self.active:
            return previous_value

        rate_types = get_tax_rate_types() + [DEFAULT_TAX_RATE_NAME]
        choices = [
            TaxType(code=rate_name, description=rate_name) for rate_name in rate_types
        ]
        # sort choices alphabetically by translations
        return sorted(choices, key=lambda x: x.code)

    def show_taxes_on_storefront(self, previous_value: bool) -> bool:
        if not self.active:
            return previous_value
        return True

    def apply_taxes_to_shipping_price_range(
        self, prices: MoneyRange, country: Country, previous_value: TaxedMoneyRange
    ) -> TaxedMoneyRange:
        if self._skip_plugin(previous_value):
            return previous_value

        taxes = self._get_taxes_for_country(country)
        return get_taxed_shipping_price(prices, taxes)

    def apply_taxes_to_shipping(
        self, price: Money, shipping_address: "Address", previous_value: TaxedMoney
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        taxes = self._get_taxes_for_country(shipping_address.country)
        return get_taxed_shipping_price(price, taxes)

    def apply_taxes_to_product(
        self,
        product: "Product",
        price: Money,
        country: Country,
        previous_value: TaxedMoney,
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value
        return self.__apply_taxes_to_product(product, price, country)

    def __apply_taxes_to_product(
        self, product: "Product", price: Money, country: Country
    ):
        taxes = None
        if country and product.charge_taxes:
            taxes = self._get_taxes_for_country(country)

        product_tax_rate = self.__get_tax_code_from_object_meta(product).code
        tax_rate = (
            product_tax_rate
            or self.__get_tax_code_from_object_meta(product.product_type).code
        )
        return apply_tax_to_price(taxes, tax_rate, price)

    def assign_tax_code_to_object_meta(
        self, obj: Union["Product", "ProductType"], tax_code: str, previous_value: Any
    ):
        if not self.active:
            return previous_value

        if tax_code not in dict(TaxRateType.CHOICES):
            return previous_value

        tax_item = {self.META_CODE_KEY: tax_code, self.META_DESCRIPTION_KEY: tax_code}
        obj.store_value_in_metadata(items=tax_item)
        obj.save()
        return previous_value

    def get_tax_code_from_object_meta(
        self, obj: Union["Product", "ProductType"], previous_value: "TaxType"
    ) -> "TaxType":
        if not self.active:
            return previous_value
        return self.__get_tax_code_from_object_meta(obj)

    def __get_tax_code_from_object_meta(
        self, obj: Union["Product", "ProductType"]
    ) -> "TaxType":
        tax_code = obj.get_value_from_metadata(self.META_CODE_KEY, "")
        tax_description = obj.get_value_from_metadata(self.META_DESCRIPTION_KEY, "")
        return TaxType(code=tax_code, description=tax_description,)

    def get_tax_rate_percentage_value(
        self, obj: Union["Product", "ProductType"], country: Country, previous_value
    ) -> Decimal:
        """Return tax rate percentage value for given tax rate type in the country."""
        if not self.active:
            return previous_value
        taxes = self._get_taxes_for_country(country)
        if not taxes:
            return Decimal(0)
        rate_name = self.__get_tax_code_from_object_meta(obj).code
        tax = taxes.get(rate_name) or taxes.get(DEFAULT_TAX_RATE_NAME)
        return Decimal(tax["value"])

    def fetch_taxes_data(self, previous_value: Any) -> Any:
        """Triggered when ShopFetchTaxRates mutation is called."""
        if not self.active:
            return previous_value
        fetch_rates(self.config.access_key)
        return True

    @classmethod
    def validate_plugin_configuration(cls, plugin_configuration: "PluginConfiguration"):
        """Validate if provided configuration is correct."""
        configuration = plugin_configuration.configuration
        configuration = {item["name"]: item["value"] for item in configuration}

        access_key = configuration.get("Access key")
        if plugin_configuration.active and not access_key:
            raise ValidationError(
                {
                    "Access key": ValidationError(
                        "Cannot be enabled without provided Access key",
                        code=PluginErrorCode.INVALID.value,
                    )
                }
            )
        if access_key and plugin_configuration.active:
            # let's check if access_key works
            fetched_data = fetch_rate_types(access_key=access_key)
            if not fetched_data["success"]:
                raise ValidationError(
                    {
                        "Access key": ValidationError(
                            "Cannot enable Vatlayer. Incorrect API key.",
                            code=PluginErrorCode.INVALID.value,
                        )
                    }
                )
