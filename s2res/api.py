import frappe
from frappe import _
from datetime import date

@frappe.whitelist(allow_guest=False)
def create_sales_invoice_and_payment(contract_name):
    """
    Creates a Sales Invoice and corresponding Payment Entries from the given contract.
    This API can be accessed via a server-side call or REST API request.
    """
    try:
        # Fetch Contract Details
        contract = frappe.get_doc("Contracts", contract_name)
        
        if not contract.tenant or not contract.rent:
            return {"status": "error", "message": _("Tenant or Rent amount is missing in the contract.")}

        # Fetch default settings
        default_currency = frappe.db.get_single_value("Global Defaults", "default_currency") or "AED"
        
        # Create Sales Invoice
        sales_invoice = frappe.new_doc("Sales Invoice")
        sales_invoice.customer = contract.tenant
        sales_invoice.currency = default_currency

        # Define Sales Invoice Items
        items = [
            {"item_code": "Rent", "qty": 1, "rate": contract.rent},
            {"item_code": "Commission", "qty": 1, "rate": contract.commission},
            {"item_code": "Deposit", "qty": 1, "rate": contract.refundable_deposit},
        ]

        taxes = {}  # Dictionary to store tax details

        # Append predefined items (Rent, Commission, Deposit)
        for item in items:
            if item["rate"]:
                item_doc = frappe.get_doc("Item", item["item_code"])
                income_account = item_doc.item_defaults[0].income_account if item_doc.item_defaults else None
                deferred_account = item_doc.item_defaults[0].deferred_revenue_account if item_doc.enable_deferred_revenue else None

                # Apply VAT 5% if contract is Commercial or Office
                real_estate = frappe.get_single("Real Estate")
                item_tax_template = real_estate.item_tax_template if contract.used_for in ["Commercial", "Office"] and item["item_code"] in ["Rent"] else None

                sales_invoice.append("items", {
                    "item_code": item["item_code"],
                    "qty": item["qty"],
                    "rate": item["rate"],
                    "amount": item["qty"] * item["rate"],
                    "enable_deferred_revenue": 1,
                    "income_account": income_account,
                    "deferred_revenue_account": deferred_account,
                    "service_start_date": contract.contract_from,
                    "service_end_date": contract.contract_to,
                    "item_tax_template": item_tax_template  # Add tax template if applicable
                })

                # Fetch tax details and calculate tax amounts
                if item_tax_template:
                    tax_details = frappe.get_doc("Item Tax Template", item_tax_template)
                    for tax in tax_details.get("taxes"):
                        tax_account = tax.tax_type  
                        tax_rate = tax.tax_rate  

                        tax_amount = (item["rate"] * tax_rate / 100)  # Calculate tax amount
                        if tax_account in taxes:
                            taxes[tax_account]["amount"] += tax_amount
                        else:
                            taxes[tax_account] = {
                                "rate": tax_rate,
                                "amount": tax_amount,
                                "description": f"{tax_account} ({tax_rate}%)"
                            }

        # Fetch Other Charges Details (Ejari, Parking, etc.)
        for charge in contract.get("other_charges_details"):
            item_code = None
            real_estate = frappe.get_single("Real Estate")
            if real_estate.ejari_account in charge.charges_account:
                item_code = "Ejari"
            elif real_estate.parking_account in charge.charges_account:
                item_code = "Parking"

            if item_code:
                item_doc = frappe.get_doc("Item", item_code)
                expense_account = item_doc.item_defaults[0].expense_account if item_doc.item_defaults else None
                real_estate = frappe.get_single("Real Estate")
                sales_invoice.append("items", {
                    "item_code": item_code,
                    "qty": 1,
                    "rate": charge.amount,
                    "amount": charge.amount,
                    "income_account": expense_account,  
                    "item_tax_template": real_estate.item_tax_template if charge.tax_type == "Taxable" else None
                })

                # Fetch tax details and calculate tax amounts
                if charge.tax_type == "Taxable":
                    real_estate = frappe.get_single("Real Estate")

                    # Fetch the Item Tax Template field from Real Estate doctype
                    item_tax_template = real_estate.item_tax_template  # Ensure this is the correct fieldname

                    tax_details = frappe.get_doc("Item Tax Template", item_tax_template)
                    for tax in tax_details.get("taxes"):
                        tax_account = tax.tax_type  
                        tax_rate = tax.tax_rate  

                        tax_amount = (charge.amount * tax_rate / 100)
                        if tax_account in taxes:
                            taxes[tax_account]["amount"] += tax_amount
                        else:
                            taxes[tax_account] = {
                                "rate": tax_rate,
                                "amount": tax_amount,
                                "description": f"{tax_account} ({tax_rate}%)"
                            }

        # Append tax entries to Sales Taxes and Charges table
        for tax_account, tax_data in taxes.items():
            sales_invoice.append("taxes", {
                "charge_type": "On Net Total",
                "account_head": tax_account,
                "rate": 0,
                "tax_amount": tax_data["amount"],
                "description": tax_data["description"]
            })

        # Save and submit Sales Invoice
        sales_invoice.insert(ignore_permissions=True)
        sales_invoice.submit()
        frappe.db.commit()
        
        # Create Payment Entries
        for receipt in contract.get("receipt_details"):
            payment_entry = frappe.new_doc("Payment Entry")
            payment_entry.payment_type = "Receive"
            payment_entry.party_type = "Customer"
            payment_entry.party = contract.tenant
            payment_entry.paid_amount = receipt.amount
            payment_entry.received_amount = receipt.amount
            payment_entry.currency = default_currency
            payment_entry.target_exchange_rate = 1

            real_estate = frappe.get_single("Real Estate")

            # Ensure the correct field names exist in the Real Estate doctype
            pdc_account = real_estate.pdc_account  # Example field for PDC
            cash_account = real_estate.cash_account  # Example field for Cash
            bank_account = real_estate.bank_account  # Example field for Bank

            # Set payment mode and determine the correct paid_to account dynamically
            if receipt.payment_mode == "Cheque":
                payment_entry.mode_of_payment = "PDC"
                payment_entry.paid_to = pdc_account if pdc_account else None
            elif receipt.payment_mode == "Cash":
                payment_entry.mode_of_payment = "Cash"
                payment_entry.paid_to = cash_account if cash_account else None
            else:
                payment_entry.mode_of_payment = receipt.payment_mode
                payment_entry.paid_to = bank_receivable_account if bank_receivable_account else None

            payment_entry.paid_to_account_currency = default_currency

            # Add reference details for non-cash payments
            if receipt.payment_mode in ["Bank Transfer", "Cheque"]:
                if not receipt.cheque_no or not receipt.cheque_date:
                    frappe.throw(f"Reference No and Reference Date are required for {receipt.payment_mode} transactions.")
                payment_entry.reference_no = receipt.cheque_no
                payment_entry.reference_date = receipt.cheque_date
            else:
                payment_entry.reference_no = "1"
                payment_entry.reference_date = date.today()

            # Link Payment Entry to Sales Invoice
            payment_entry.append("references", {
                "reference_doctype": "Sales Invoice",
                "reference_name": sales_invoice.name,
                "total_amount": sales_invoice.grand_total,
                "allocated_amount": receipt.amount
            })

            # Save and submit Payment Entry
            payment_entry.insert(ignore_permissions=True)
            payment_entry.submit()
            frappe.db.commit()
        
        frappe.msgprint(f"Sales Invoice {sales_invoice.name} and Payment Entries created successfully.", alert=True)
        return sales_invoice.name

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Error in create_sales_invoice_and_payment"))
        return {"status": "error", "message": str(e)}
