# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt
import json
import frappe
from frappe import _, throw
from frappe.model.document import Document
from frappe.integrations.utils import make_post_request

from frappe_whatsapp.utils import format_number


class WhatsAppMessage(Document):
    def on_update(self):
        self.update_profile_name()

    def update_profile_name(self):
        number = self.get("from")
        if not number:
            return
        from_number = format_number(number)

        if (
            self.has_value_changed("profile_name")
            and self.profile_name
            and from_number
            and frappe.db.exists("WhatsApp Profiles", {"number": from_number})
        ):
            profile_id = frappe.get_value("WhatsApp Profiles", {"number": from_number}, "name")
            frappe.db.set_value("WhatsApp Profiles", profile_id, "profile_name", self.profile_name)

    def create_whatsapp_profile(self):
        number = format_number(self.get("from") or self.to)
        if not frappe.db.exists("WhatsApp Profiles", {"number": number}):
            frappe.get_doc({
                "doctype": "WhatsApp Profiles",
                "profile_name": self.profile_name,
                "number": number,
                "whatsapp_account": self.whatsapp_account
            }).insert(ignore_permissions=True)

    """Send whats app messages."""
    def before_insert(self):
        """Send message."""

        # --- Handle Outgoing Messages ---
        if self.type == "Outgoing" and not self.message_id:

            # Case 1: Template Message (Doctype or custom JSON)
            if self.message_type == "Template":
                self.send_template()

            # Case 2: Manual Message (Text or Media, including complex JSON payload from template_json)
            else:  # self.message_type != "Template" (i.e., "Manual" or fallback)

                # Use custom JSON payload if provided (for interactive/complex messages)
                if self.template_json:
                    try:
                        data = json.loads(self.template_json)
                    except json.JSONDecodeError:
                        frappe.throw(_("Invalid JSON provided in template_json field."))

                    # Standard required fields
                    if 'messaging_product' not in data:
                        data['messaging_product'] = 'whatsapp'
                    if 'to' not in data:
                        data['to'] = format_number(self.to)

                    # The message 'type' (e.g., 'text', 'interactive', 'location') should be defined in template_json

                # Standard Text/Media Message Construction
                else:
                    if self.attach and not self.attach.startswith("http"):
                        link = frappe.utils.get_url() + "/" + self.attach
                    else:
                        link = self.attach

                    data = {
                        "messaging_product": "whatsapp",
                        "to": format_number(self.to),
                        "type": self.content_type,
                    }
                    if self.is_reply and self.reply_to_message_id:
                        data["context"] = {"message_id": self.reply_to_message_id}

                    # Handle media content types
                    if self.content_type in ["document", "image", "video"]:
                        data[self.content_type.lower()] = {
                            "link": link,
                            "caption": self.message,
                        }
                    elif self.content_type == "reaction":
                        data["reaction"] = {
                            "message_id": self.reply_to_message_id,
                            "emoji": self.message,
                        }
                    elif self.content_type == "text":
                        data["text"] = {"preview_url": True, "body": self.message}

                    elif self.content_type == "audio":
                        data["audio"] = {"link": link}

                # Send the final payload (from Manual setup or template_json)
                try:
                    self.notify(data)
                    self.status = "Success"
                except Exception as e:
                    self.status = "Failed"
                    frappe.throw(f"Failed to send message {str(e)}")

        self.create_whatsapp_profile()

    def send_template(self):
        """Send template from doctype or a custom json structure."""
        data = {
            "messaging_product": "whatsapp",
            "to": format_number(self.to),
        }

        # Case 1: Template name provided (uses existing WhatsApp Templates doctype)
        if self.template:
            template = frappe.get_doc("WhatsApp Templates", self.template)
            data["type"] = "template"
            data["template"] = {
                "name": template.actual_name or template.template_name,
                "language": {"code": template.language_code},
                "components": [],
            }

            # Handle template parameters from reference document (Body Components)
            if template.sample_values:
                field_names = template.field_names.split(",") if template.field_names else template.sample_values.split(
                    ",")
                parameters = []
                template_parameters = []

                if self.flags.custom_ref_doc:
                    custom_values = self.flags.custom_ref_doc
                    for field_name in field_names:
                        value = custom_values.get(field_name.strip())
                        parameters.append({"type": "text", "text": value})
                        template_parameters.append(value)

                else:
                    ref_doc = frappe.get_doc(self.reference_doctype, self.reference_name)
                    for field_name in field_names:
                        # Use .get_formatted to ensure values are correct (e.g., date formats)
                        value = ref_doc.get_formatted(field_name.strip())
                        parameters.append({"type": "text", "text": value})
                        template_parameters.append(value)

                self.template_parameters = json.dumps(template_parameters)

                data["template"]["components"].append(
                    {
                        "type": "body",
                        "parameters": parameters,
                    }
                )

            # Handle header image component
            if template.header_type and template.sample:
                if template.header_type == 'IMAGE':
                    if template.sample.startswith("http"):
                        url = f'{template.sample}'
                    else:
                        url = f'{frappe.utils.get_url()}{template.sample}'
                    data['template']['components'].append({
                        "type": "header",
                        "parameters": [{
                            "type": "image",
                            "image": {
                                "link": url
                            }
                        }]
                    })

        # Case 2: Custom template JSON provided (for interactive messages, location, etc. which are NOT 'template' API calls)
        elif self.template_json:
            try:
                # Load the JSON and use it as the payload.
                # This handles 'interactive', 'location', 'text', 'contacts' etc.
                template_data = json.loads(self.template_json)
                data.update(template_data)

            except json.JSONDecodeError:
                frappe.throw(_("Invalid JSON provided in template_json field."))

            # If the JSON is for a simple message type (text, location),
            # we change the type to "Manual" so it gets handled in before_insert
            # and avoids being sent twice or improperly structured.
            # NOTE: This is mainly a fallback. Ideally, 'interactive' and 'template'
            # should be sent from here. 'text' and 'media' are best handled by 'Manual'.
            if data.get("type") in ["text", "location", "contacts"]:
                self.message_type = "Manual"
                self.content_type = data.get("type")
                # Exit and let before_insert continue with the 'Manual' logic
                return
        else:
            frappe.throw(_("Either a 'Template' name or valid 'template_json' must be provided."))

        # Ensure 'type' is set for notification if it wasn't a template from doctype
        if not data.get("type"):
            # If it's a template from doctype, 'type' is already 'template'.
            # If it's custom JSON, it must define its type (e.g., 'interactive').
            # Default to template if no type is found, though this is risky.
            data["type"] = "template"

        self.notify(data)

    def notify(self, data):
        """Notify."""
        whatsapp_account = frappe.get_doc(
            "WhatsApp Account",
            self.whatsapp_account,
        )
        token = whatsapp_account.get_password("token")

        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        try:
            response = make_post_request(
                f"{whatsapp_account.url}/{whatsapp_account.version}/{whatsapp_account.phone_id}/messages",
                headers=headers,
                data=json.dumps(data),
            )
            self.message_id = response["messages"][0]["id"]

        except Exception as e:
            # Safely handle the exception, checking for 'integration_request' existence
            if frappe.flags.get("integration_request"):
                # Assuming standard Frappe integration request structure for error
                res = frappe.flags.integration_request.json().get("error", {})
                error_message = res.get("message", "Unknown API Error")

                # Log the error
                frappe.get_doc(
                    {
                        "doctype": "WhatsApp Notification Log",
                        "template": "Text Message",
                        "meta_data": frappe.flags.integration_request.json(),
                    }
                ).insert(ignore_permissions=True)

                frappe.throw(msg=error_message, title=res.get("error_user_title", "WhatsApp API Error"))
            else:
                # Fallback error handling if integration_request is not available
                frappe.throw(msg=f"WhatsApp API Error: {str(e)}", title="API Request Failed")


def on_doctype_update():
    frappe.db.add_index("WhatsApp Message", ["reference_doctype", "reference_name"])


@frappe.whitelist()
def send_message(to, message, content_type, account, reference_doctype=None, reference_name=None, attach=None):
    """Sends a manual (text or media) message."""
    try:
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "to": to,
            "message": message,
            "type": "Outgoing",
            "message_type": "Manual",
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "content_type": content_type,
            "whatsapp_account": account,
            "attach": attach,  # Added attach support
        })

        doc.save()
    except Exception as e:
        raise e


@frappe.whitelist()
def send_template(to, account, reference_doctype=None, reference_name=None, template=None, template_json=None):
    """
    Sends a template message (Doctype) or a custom message (via template_json).
    The template_json is used for all complex message types (interactive, location, contacts).
    """

    if not (template or template_json):
        frappe.throw(_("Either 'template' name or 'template_json' must be provided."))

    if template and template_json:
        frappe.throw(_("Cannot provide both 'template' name and 'template_json'. Use one or the other."))

    try:
        message_doc = {
            "doctype": "WhatsApp Message",
            "to": to,
            "type": "Outgoing",
            "message_type": "Template",  # Default to Template to trigger self.send_template
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "content_type": "text",  # Default, type will be determined by template or JSON
            "whatsapp_account": account,
            "template": template,
            "template_json": template_json,
        }

        doc = frappe.get_doc(message_doc)
        doc.save()
    except Exception as e:
        raise e
