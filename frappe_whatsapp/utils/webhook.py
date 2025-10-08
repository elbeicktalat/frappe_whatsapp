"""Webhook."""
import frappe
import json
import requests
import time
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()
	return post()


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	verify_token = frappe.form_dict.get("hub.verify_token")
	webhook_verify_token = frappe.db.get_value('WhatsApp Account', verify_token, 'webhook_verify_token')

	if not webhook_verify_token:
		frappe.throw("No matching WhatsApp account")

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def post():
    # Helper function to get data safely
    data = frappe.form_dict

    if frappe.request.method != "POST":
        if frappe.form_dict.get("hub.mode") == "subscribe" and frappe.form_dict.get(
                "hub.verify_token") == frappe.db.get_single_value("WhatsApp Settings", "webhook_verify_token"):
            return frappe.form_dict.get("hub.challenge")

        return "OK"

    # Standard webhook processing to extract messages
    try:
        messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
        phone_id = data["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
        contact = data["entry"][0]["changes"][0]["value"]["contacts"][0]
        sender_wa_id = contact["wa_id"]
        sender_profile_name = contact["profile"].get("name", sender_wa_id)
    except Exception:
        frappe.logger("whatsapp").exception("Error extracting data from webhook payload")
        return "OK"

    if not messages:
        return "OK"

    try:
        whatsapp_account = frappe.get_doc("WhatsApp Account", {"phone_number_id": phone_id})
    except frappe.DoesNotExistError:
        frappe.logger("whatsapp").error(f"WhatsApp Account not found for phone_number_id: {phone_id}")
        return "OK"

    for message in messages:
        message_type = message.get("type")
        reply_to_message_id = message.get("context", {}).get("id")

        # Determine if it's a message reply (including interactive replies)
        is_reply = bool(reply_to_message_id)

        # Base structure for the WhatsApp Message document
        whatsapp_message_doc = frappe.new_doc("WhatsApp Message")
        whatsapp_message_doc.sender_id = sender_wa_id
        whatsapp_message_doc.from_number = sender_wa_id
        whatsapp_message_doc.to = whatsapp_account.phone_number
        whatsapp_message_doc.sender_name = sender_profile_name
        whatsapp_message_doc.whatsapp_account = whatsapp_account.name
        whatsapp_message_doc.message_timestamp = message.get("timestamp")
        whatsapp_message_doc.message_id = message.get("id")
        whatsapp_message_doc.type = message_type

        message_content = {}

        # --- FIX APPLIED HERE: Handle different interactive types ---
        if message_type == "interactive":
            interactive_data = message.get("interactive", {})
            interactive_type = interactive_data.get("type")

            if interactive_type == "button_reply":
                # This is the type you received. It contains 'button_reply'
                button_reply = interactive_data.get("button_reply", {})

                message_content = {
                    "message_type": "button_reply",
                    "message": f"Button Clicked: {button_reply.get('title')} (ID: {button_reply.get('id')})",
                    "reply_to_message_id": reply_to_message_id,
                    "reply_id": button_reply.get('id'),  # Store the button ID separately
                    "content_type": "text"
                }

            elif interactive_type == "nfm_reply":
                # Handle Native Flow Messages (Original logic)
                nfm_reply = interactive_data.get("nfm_reply", {})

                message_content = {
                    "message_type": "interactive",
                    "message": nfm_reply.get("response_json"),
                    "reply_to_message_id": reply_to_message_id,
                    "content_type": "text"
                }

            elif interactive_type == "list_reply":
                # Handle List Messages (If applicable)
                list_reply = interactive_data.get("list_reply", {})

                message_content = {
                    "message_type": "list_reply",
                    "message": f"List Item Selected: {list_reply.get('title')} (ID: {list_reply.get('id')})",
                    "reply_to_message_id": reply_to_message_id,
                    "reply_id": list_reply.get('id'),
                    "content_type": "text"
                }

            else:
                frappe.logger("whatsapp").error(f"Unsupported interactive message type: {interactive_type}")
                continue  # Skip processing this message

        # --- Handle other standard message types (kept for completeness) ---
        elif message_type == "text":
            message_content = {
                "message": message["text"]["body"],
                "reply_to_message_id": reply_to_message_id,
                "content_type": "text"
            }

        elif message_type == "image":
            image_data = message["image"]
            media_id = image_data.get("id")
            caption = image_data.get("caption", "")

            message_content = {
                "message": caption,
                "reply_to_message_id": reply_to_message_id,
                "media_id": media_id,
                "content_type": "image"
            }

        # Add logic for document, video, location, contacts, etc. here...

        else:
            # Handle unrecognized or unhandled message types
            frappe.logger("whatsapp").info(f"Received unhandled message type: {message_type}")
            continue

        whatsapp_message_doc.update(message_content)

        try:
            whatsapp_message_doc.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception:
            frappe.logger("whatsapp").exception("Error saving WhatsApp Message document")

    return "OK"


def update_status(data):
	"""Update status hook."""
	if data.get("field") == "message_template_status_update":
		update_template_status(data['value'])

	elif data.get("field") == "messages":
		update_message_status(data['value'])

def update_template_status(data):
	"""Update template status."""
	frappe.db.sql(
		"""UPDATE `tabWhatsApp Templates`
		SET status = %(event)s
		WHERE id = %(message_template_id)s""",
		data
	)

def update_message_status(data):
	"""Update message status."""
	id = data['statuses'][0]['id']
	status = data['statuses'][0]['status']
	conversation = data['statuses'][0].get('conversation', {}).get('id')
	name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

	doc = frappe.get_doc("WhatsApp Message", name)
	doc.status = status
	if conversation:
		doc.conversation_id = conversation
	doc.save(ignore_permissions=True)
