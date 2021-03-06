import logging
from random import random
import time
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT
from odoo.addons.base.models.res_partner import ADDRESS_FIELDS
from .avalara_api import AvaTaxService, BaseAddress
from .avatax_rest_api import AvaTaxRESTService


_LOGGER = logging.getLogger(__name__)


class ResPartner(models.Model):
    """
    Update partner information by adding new fields
    according to avalara partner configuration
    """

    _inherit = "res.partner"

    @api.model
    def _migrate_exemption_data(self):
        """
        Migrate values from old exemption fields
        into the new per company property fields
        """
        companies = self.env['res.company'].search([])
        for company in companies:
            Partner = self.env['res.partner'].with_context(force_company=company.id)
            pending_exempt_partners = Partner.search([
                ('exemption_code_id', '!=', False),
                ('property_exemption_code_id', '=', False),
            ])
            if pending_exempt_partners:
                _LOGGER.info(
                    'Migrating exemption data on %d partners for company %s',
                    len(pending_exempt_partners),
                    company.display_name
                )
            for partner in pending_exempt_partners:
                partner.write({
                    'property_tax_exempt': partner.tax_exempt,
                    'property_exemption_code_id': partner.exemption_code_id.id,
                    'property_exemption_number': partner.exemption_number,
                })

    @api.onchange("property_exemption_country_wide")
    def _onchange_property_exemption_contry_wide(self):
        if self.property_exemption_country_wide:
            message = _(
                "Enabling the exemption status for all states"
                " may have tax compliance risks,"
                " and should be carefully considered.\n\n"
                " Please ensure that your tax advisor was consulted and the"
                " necessary tax exemption documentation was obtained"
                " for every state this Partner may have transactions."
            ),
            return {
                "warning": {
                    "title": _("Tax Compliance Risk"),
                    "message": message,
                }
            }

    date_validation = fields.Date(
        "Last Validation Date",
        readonly=True,
        copy=False,
        help="The date the address was last validated by AvaTax and accepted",
    )
    validation_method = fields.Selection(
        [("avatax", "AVALARA"), ("usps", "USPS"), ("other", "Other")],
        "Address Validation Method",
        readonly=True,
        copy=False,
        help="It gets populated when the address is validated by the method",
    )
    validated_on_save = fields.Boolean(
        "Validated On Save",
        help="Indicates if the address is already validated "
        " on save before calling the wizard",
    )
    customer_code = fields.Char("Customer Code", copy=False)
    tax_exempt = fields.Boolean(
        "Is Tax Exempt (Deprecated))",
        deprecated=True,
    )
    exemption_number = fields.Char(
        "Exemption Number (Deprecated)",
        deprecated=True,
    )
    exemption_code_id = fields.Many2one(
        "exemption.code",
        "Exemption Code (Deprecated)",
        deprecated=True,
    )
    property_tax_exempt = fields.Boolean(
        "Is Tax Exempt",
        company_dependent=True,
        help="This company or address can claim for tax exemption",
    )
    property_exemption_number = fields.Char(
        "Exemption Number",
        company_dependent=True,
        help="The State identification number relevant fot the exemption"
    )
    property_exemption_code_id = fields.Many2one(
        "exemption.code",
        "Exemption Code",
        company_dependent=True,
        help="The type of exemption granted",
    )
    property_exemption_country_wide = fields.Boolean(
        "Exemption Applies Country Wide",
        help="When enabled, the delivery address State is irrelevant"
        " when looking up the exemption status, meaning that the exemption"
        " is considered applicable for all states",
    )
    vat_id = fields.Char(
        "VAT ID",
        help="Customers VAT number (Buyer VAT). "
        "Identifies the customer as a “Registered Business” "
        "and the tax engine will utilize that information "
        "in the tax decision process.",
    )

    _sql_constraints = [
        ("name_uniq", "unique(customer_code)", "Customer Code must be unique!"),
    ]

    @api.multi
    def generate_cust_code(self):
        "Auto populate customer code"
        for partner in self:
            partner.customer_code = (
                str(int(time.time()))
                + "-"
                + str(int(random() * 10))
                + "-"
                + "Cust-"
                + str(partner.id)
            )
        return True

    def check_avatax_support(self, avatax_config, country_id):
        """ Checks if address validation pre-condition meets. """
        if avatax_config.address_validation:
            raise UserError(
                _(
                    "The AvaTax Address Validation Service is disabled by the administrator. Please make sure it's enabled for the address validation"
                )
            )
        if (
            country_id
            and country_id not in [x.id for x in avatax_config.country_ids]
            or not country_id
        ):
            raise UserError(
                _(
                    "The AvaTax Address Validation Service does not support this country in the configuration, please continue with your normal process."
                )
            )
        return True

    @api.onchange("property_tax_exempt")
    def onchange_tax_exemption(self):
        if not self.property_tax_exempt:
            self.property_exemption_number = ""
            self.property_exemption_code_id = None

    def get_state_id(self, code, c_code):
        """ Returns the id of the state from the code. """
        c_id = self.env["res.country"].search([("code", "=", c_code)])[0]
        s_id = self.env["res.country.state"].search(
            [("code", "=", code), ("country_id", "=", c_id.id)]
        )
        if s_id:
            return s_id[0].id
        return False

    def get_country_id(self, code):
        """ Returns the id of the country from the code. """
        country = self.env["res.country"].search([("code", "=", code)])
        if country:
            return country[0].id
        return False

    def get_state_code(self, state_id):
        """ Returns the code from the id of the state. """

        state_obj = self.env["res.country.state"]
        return state_id and state_obj.browse(state_id).code

    def get_country_code(self, country_id):
        """ Returns the code from the id of the country. """

        country_obj = self.env["res.country"]
        return country_id and country_obj.browse(country_id).code

    @api.multi
    def multi_address_validation(self):
        for partner in self:
            vals = partner.read(
                ["street", "street2", "city", "state_id", "zip", "country_id"]
            )[0]
            vals["state_id"] = vals.get("state_id") and vals["state_id"][0]
            vals["country_id"] = vals.get("country_id") and vals["country_id"][0]

            avatax_config = self.env.user.company_id.get_avatax_config_company()
            if avatax_config:
                try:
                    valid_address = self._validate_address(vals, avatax_config)
                    vals.update(
                        {
                            "street": valid_address.Line1,
                            "street2": valid_address.Line2,
                            "city": valid_address.City,
                            "state_id": self.get_state_id(
                                valid_address.Region, valid_address.Country
                            ),
                            "zip": valid_address.PostalCode,
                            "country_id": self.get_country_id(valid_address.Country),
                            "partner_latitude": valid_address.Latitude,
                            "partner_longitude": valid_address.Longitude,
                            "date_validation": time.strftime(
                                DEFAULT_SERVER_DATE_FORMAT
                            ),
                            "validation_method": "avatax",
                            "validated_on_save": True,
                        }
                    )
                    partner.write(vals)
                except UserError as error:
                    _LOGGER.warning(
                        "couldn't validate address for partner %s: %s"
                        % (partner.display_name, error)
                    )

        return True

    @api.multi
    def button_avatax_validate_address(self):
        """Method is used to verify of state and country """
        view_ref = self.env.ref(
            "avatax_connector.view_avalara_salestax_address_validate", False
        )
        address = self.read(
            ["street", "street2", "city", "state_id", "zip", "country_id"]
        )[0]
        address["state_id"] = address.get("state_id") and address["state_id"][0]
        address["country_id"] = address.get("country_id") and address["country_id"][0]

        # Get the valid result from the AvaTax Address Validation Service
        self._validate_address(address)
        ctx = self._context.copy()
        ctx.update({"active_ids": self.ids, "active_id": self.id})

        return {
            "type": "ir.actions.act_window",
            "name": "Address Validation",
            "view_type": "form",
            "view_mode": "form",
            "view_id": view_ref.id,
            "res_model": "avalara.salestax.address.validate",
            "nodestroy": True,
            "res_id": False,
            "target": "new",
            "context": ctx,
        }

    def _validate_address(self, address, avatax_config=False):
        """ Returns the valid address from the AvaTax Address Validation Service. """
        if not avatax_config:
            avatax_config = self.env.user.company_id.get_avatax_config_company()
        if not avatax_config:
            raise UserError(
                _(
                    "This module has not yet been setup.  "
                    "Please refer to the Avatax module documentation."
                )
            )

        # Obtain the state code & country code and create a BaseAddress Object
        state_code = address.get("state_id") and self.get_state_code(
            address["state_id"]
        )
        country_code = address.get("country_id") and self.get_country_code(
            address["country_id"]
        )

        if "rest" in avatax_config.service_url:
            avatax_restpoint = AvaTaxRESTService(
                avatax_config.account_number,
                avatax_config.license_key,
                avatax_config.service_url,
                avatax_config.request_timeout,
                avatax_config.logging,
            )
            valid_address = avatax_restpoint.validate_rest_address(
                address, state_code, country_code
            )
        else:
            avapoint = AvaTaxService(
                avatax_config.account_number,
                avatax_config.license_key,
                avatax_config.service_url,
                avatax_config.request_timeout,
                avatax_config.logging,
            )
            addSvc = avapoint.create_address_service().addressSvc

            baseaddress = BaseAddress(
                addSvc,
                address.get("street") or None,
                address.get("street2") or None,
                address.get("city"),
                address.get("zip"),
                state_code,
                country_code,
                0,
            ).data
            result = avapoint.validate_address(
                baseaddress, avatax_config.result_in_uppercase and "Upper" or "Default"
            )
            valid_address = result.ValidAddresses[0][0]
        return valid_address

    def update_addresses(self, vals, from_write=False):
        """ Updates the vals dictionary with the valid address as returned from the Avalara Address Validation. """
        address = vals
        if vals:
            if (
                vals.get("street")
                or vals.get("street2")
                or vals.get("zip")
                or vals.get("city")
                or vals.get("country_id")
                or vals.get("state_id")
            ):
                avatax_config = self.env.user.company_id.get_avatax_config_company()
                if avatax_config and avatax_config.validation_on_save:
                    brw_address = self.read(
                        ["street", "street2", "city", "state_id", "zip", "country_id"]
                    )[0]
                    address["country_id"] = (
                        "country_id" in vals
                        and vals["country_id"]
                        or brw_address.get("country_id")
                        and brw_address["country_id"][0]
                    )
                    if self.check_avatax_support(avatax_config, address["country_id"]):
                        if from_write:
                            address["street"] = (
                                "street" in vals
                                and vals["street"]
                                or brw_address.get("street")
                                or ""
                            )
                            address["street2"] = (
                                "street2" in vals
                                and vals["street2"]
                                or brw_address.get("street2")
                                or ""
                            )
                            address["city"] = (
                                "city" in vals
                                and vals["city"]
                                or brw_address.get("city")
                                or ""
                            )
                            address["zip"] = (
                                "zip" in vals
                                and vals["zip"]
                                or brw_address.get("zip")
                                or ""
                            )
                            address["state_id"] = (
                                "state_id" in vals
                                and vals["state_id"]
                                or brw_address.get("state_id")
                                and brw_address["state_id"][0]
                                or False
                            )

                        valid_address = self._validate_address(address, avatax_config)
                        vals.update(
                            {
                                "street": valid_address.Line1,
                                "street2": valid_address.Line2,
                                "city": valid_address.City,
                                "state_id": self.get_state_id(
                                    valid_address.Region, valid_address.Country
                                ),
                                "zip": valid_address.PostalCode,
                                "country_id": self.get_country_id(
                                    valid_address.Country
                                ),
                                "partner_latitude": valid_address.Latitude,
                                "partner_longitude": valid_address.Longitude,
                                "date_validation": time.strftime(
                                    DEFAULT_SERVER_DATE_FORMAT
                                ),
                                "validation_method": "avatax",
                                "validated_on_save": True,
                            }
                        )
        return vals

    @api.model
    def create(self, vals):
        if vals.get("parent_id") and vals.get("use_parent_address"):
            domain_siblings = [
                ("parent_id", "=", vals["parent_id"]),
                ("use_parent_address", "=", True),
            ]
            update_ids = [vals["parent_id"]] + self.search(domain_siblings)
            vals = self.update_addresses(update_ids, vals)
        else:
            address = vals
            if (
                vals.get("street")
                or vals.get("street2")
                or vals.get("zip")
                or vals.get("city")
                or vals.get("country_id")
                or vals.get("state_id")
            ):
                # It will work when user want to validate address at customer creation,
                # check option in avalara api form
                avatax_config = self.env.user.company_id.get_avatax_config_company()
                if avatax_config and avatax_config.validation_on_save:

                    if self.check_avatax_support(
                        avatax_config, address.get("country_id")
                    ):
                        valid_address = self._validate_address(address, avatax_config)
                        vals.update(
                            {
                                "street": valid_address.Line1,
                                "street2": valid_address.Line2,
                                "city": valid_address.City,
                                "state_id": self.get_state_id(
                                    valid_address.Region, valid_address.Country
                                ),
                                "zip": valid_address.PostalCode,
                                "country_id": self.get_country_id(
                                    valid_address.Country
                                ),
                                "partner_latitude": valid_address.Latitude,
                                "partner_longitude": valid_address.Longitude,
                                "date_validation": time.strftime(
                                    DEFAULT_SERVER_DATE_FORMAT
                                ),
                                "validation_method": "avatax",
                                "validated_on_save": True,
                            }
                        )

        # execute the create
        cust_id = super(ResPartner, self).create(vals)

        # Generate a detailed customer code based on timestamp, a random number, and it's  ID
        customer_code = (
            str(int(time.time()))
            + "-"
            + str(int(random() * 10))
            + "-"
            + "Cust-"
            + str(cust_id.id)
        )
        # Auto populate customer code
        cust_id.write({"customer_code": customer_code})
        return cust_id

    @api.multi
    def write(self, vals):
        if any(
            address_field in vals for address_field in ADDRESS_FIELDS
        ) and not vals.get("date_validation"):
            vals.update(
                {
                    "partner_latitude": "",
                    "partner_longitude": "",
                    "date_validation": False,
                    "validation_method": "",
                    "validated_on_save": False,
                }
            )
        # Follow the normal write process if it's a write operation from the wizard
        if self.env.context.get("from_validate_button", False):
            return super(ResPartner, self).write(vals)
        vals1 = self.update_addresses(vals, True)
        return super(ResPartner, self).write(vals1)
