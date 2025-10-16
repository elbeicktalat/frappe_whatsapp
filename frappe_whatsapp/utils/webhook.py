"""Webhook."""
import frappe
import json
import requests
import time
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account


# --- Utility Functions (Assuming these are defined elsewhere or correctly imported) ---
# frappe.get_doc, frappe.local.form_dict, frappe.generate_hash, get_whatsapp_account, etc.

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


# --------------------------------------------------------------------------------------
# NEW/MODIFIED FUNCTIONS START HERE
# --------------------------------------------------------------------------------------

def handle_media_message(message, message_type, whatsapp_account, sender_profile_name, is_reply, reply_to_message_id):
    """Handles all incoming media messages (image, audio, video, document, sticker)."""
    try:
        token = whatsapp_account.get_password("token")
        url = f"{whatsapp_account.url}/{whatsapp_account.version}/"

        # Use message_type to access the specific media object (e.g., message['image'])
        media_id = message[message_type]["id"]

        # For stickers, the 'caption' field does not exist.
        caption = message[message_type].get("caption")

        headers = {
            'Authorization': 'Bearer ' + token
        }

        # 1. Get Media Metadata
        response = requests.get(f'{url}{media_id}/', headers=headers)

        if response.status_code == 200:
            media_data = response.json()
            media_url = media_data.get("url")
            mime_type = media_data.get("mime_type")
            # Simple extension extraction (e.g., image/jpeg -> jpeg). Handle stickers separately if needed.
            file_extension = mime_type.split('/')[-1] if '/' in mime_type else "dat"

            # 2. Download Media File
            media_response = requests.get(media_url, headers=headers)
            if media_response.status_code == 200:
                file_data = media_response.content
                file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

                # Determine the message content based on caption or file path
                message_content = caption if caption else f"/files/{file_name}"

                # 3. Create WhatsApp Message Doc
                message_doc = frappe.get_doc({
                    "doctype": "WhatsApp Message",
                    "type": "Incoming",
                    "from": message['from'],
                    "message_id": message['id'],
                    "reply_to_message_id": reply_to_message_id,
                    "is_reply": is_reply,
                    "message": message_content,
                    "content_type": message_type,
                    "profile_name": sender_profile_name,
                    "whatsapp_account": whatsapp_account.name
                })
                message_doc.insert(ignore_permissions=True)

                # 4. Create File Doc and attach
                file_doc = frappe.get_doc(
                    {
                        "doctype": "File",
                        "file_name": file_name,
                        "attached_to_doctype": "WhatsApp Message",
                        "attached_to_name": message_doc.name,
                        "content": file_data,
                        "attached_to_field": "attach"
                    }
                )
                file_doc.save(ignore_permissions=True)

                # 5. Update WhatsApp Message with file URL
                message_doc.attach = file_doc.file_url
                message_doc.save(ignore_permissions=True)

    except Exception as e:
        # Log media handling errors
        frappe.error_log(f"Error handling media message of type {message_type}: {e}", "WHATSAPP_WEBHOOK_MEDIA_ERROR")


def handle_incoming_message(message, whatsapp_account, sender_profile_name):
    """Processes a single incoming message based on its type."""
    try:
        message_type = message.get('type')

        # Use get('context') and check for 'id' to determine reply status
        context = message.get('context')
        is_reply = bool(context and context.get('id'))
        reply_to_message_id = context['id'] if is_reply else None

        common_fields = {
            "doctype": "WhatsApp Message",
            "type": "Incoming",
            "from": message['from'],
            "message_id": message['id'],
            "reply_to_message_id": reply_to_message_id,
            "is_reply": is_reply,
            "profile_name": sender_profile_name,
            "whatsapp_account": whatsapp_account.name
        }

        if message_type == 'text':
            frappe.get_doc({
                **common_fields,
                "message": message['text']['body'],
                "content_type": message_type,
            }).insert(ignore_permissions=True)

        elif message_type == 'reaction':
            frappe.get_doc({
                **common_fields,
                "message": message['reaction']['emoji'],
                "reply_to_message_id": message['reaction']['message_id'],
                "content_type": "reaction",
                "is_reply": True,
            }).insert(ignore_permissions=True)

        # Interactive covers list replies and quick reply buttons
        elif message_type == 'interactive':
            interactive_data = message['interactive']
            message_text = ""
            content_type = ""

            if 'button_reply' in interactive_data:
                message_text = interactive_data['button_reply']['title']
                content_type = "button_reply"
            elif 'list_reply' in interactive_data:
                message_text = interactive_data['list_reply']['title']
                content_type = "list_reply"
            elif 'nfm_reply' in interactive_data:
                message_text = interactive_data['nfm_reply']['response_json']
                content_type = "flow_reply"

            if message_text:
                frappe.get_doc({
                    **common_fields,
                    "message": message_text,
                    "content_type": content_type,
                    "is_reply": True,
                }).insert(ignore_permissions=True)

        elif message_type == 'location':
            location_data = message.get('location', {})

            latitude = location_data.get('latitude')
            longitude = location_data.get('longitude')
            name = location_data.get('name')
            address = location_data.get('address')

            if latitude and longitude:
                message_text = f"Lat: {latitude}, Lon: {longitude}"
            else:
                message_text = "Location received, but coordinates are missing."

            if name:
                message_text += f"\nName: {name}"
            if address:
                message_text += f"\nAddress: {address}"

            frappe.get_doc({
                **common_fields,
                "message": message_text,
                "content_type": message_type,
            }).insert(ignore_permissions=True)

        elif message_type == 'contact':
            contact_messages = []
            for contact in message['contacts']:
                contact_name = contact.get('name', {}).get('formatted_name', 'Unknown Name')
                phone_number = next(
                    (phone['wa_id'] for phone in contact.get('phones', []) if 'wa_id' in phone),
                    'No WA ID'
                )
                contact_messages.append(f"{contact_name} ({phone_number})")

            frappe.get_doc({
                **common_fields,
                "message": " | ".join(contact_messages),
                "content_type": message_type,
            }).insert(ignore_permissions=True)

        elif message_type == "button":
            frappe.get_doc({
                **common_fields,
                "message": message['button']['text'],
                "content_type": message_type,
            }).insert(ignore_permissions=True)

        # Media/Files: image, audio, video, document, sticker (sticker is a media type)
        elif message_type in ["image", "audio", "video", "document", "sticker"]:
            handle_media_message(message, message_type, whatsapp_account, sender_profile_name, is_reply,
                                 reply_to_message_id)

        # Fallback for unsupported or new types (system, unsupported, etc.)
        else:
            message_content = f"Unhandled type: {message_type}"

            frappe.get_doc({
                **common_fields,
                "message": message_content,
                "content_type": message_type,
            }).insert(ignore_permissions=True)

    except Exception as e:
        # Log general message handling errors
        frappe.error_log(f"Error processing message ID {message.get('id')}. Type: {message.get('type')}. Error: {e}",
                         "WHATSAPP_WEBHOOK_MESSAGE_PROCESSING_ERROR")


def post():
    """Post."""
    data = frappe.local.form_dict

    try:
        frappe.get_doc({
            "doctype": "WhatsApp Notification Log",
            "template": "Webhook",
            "meta_data": json.dumps(data)
        }).insert(ignore_permissions=True)

    except Exception as e:
        # Log if Notification Log insertion fails
        frappe.error_log(f"Error saving WhatsApp Notification Log: {e}", "WHATSAPP_WEBHOOK_LOG_ERROR")

    messages = []
    phone_id = None

    # Use a separate try block for robust data extraction
    try:
        # Extract messages and phone_id from the nested structure
        messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
        phone_id = data["entry"][0]["changes"][0]["value"].get("metadata", {}).get("phone_number_id")

        sender_profile_name = next(
            (
                contact.get("profile", {}).get("name")
                for entry in data.get("entry", [])
                for change in entry.get("changes", [])
                for contact in change.get("value", {}).get("contacts", [])
            ),
            None,
        )

        # Attempt to extract phone_id again if it was missed in the first pass (e.g., status update)
        if not phone_id:
            phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get(
                "phone_number_id")

    except Exception as e:
        # This catches key errors or index errors during initial data parsing
        frappe.error_log(f"Error extracting data from webhook payload: {e}. Payload: {json.dumps(data)[:500]}...",
                         "WHATSAPP_WEBHOOK_DATA_EXTRACTION_ERROR")
        # If extraction fails, we exit
        return Response("Error processing payload", status=500)

    whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None
    if not whatsapp_account:

        # Handle status updates if no account is found (they might not have a phone_id in message structure)
        if not messages:
            changes = data.get("entry", [{}])[0].get("changes", [{}])[0]
            if changes:
                update_status(changes)
        return

    if messages:
        for message in messages:
            handle_incoming_message(message, whatsapp_account, sender_profile_name)

    else:
        # Handle status updates, which won't have a 'messages' key
        changes = data.get("entry", [{}])[0].get("changes", [{}])[0]

        if changes:
            update_status(changes)
    return


# --------------------------------------------------------------------------------------
# ORIGINAL/UNMODIFIED STATUS UPDATE FUNCTIONS FOLLOW
# --------------------------------------------------------------------------------------

def update_status(data):
    """Update status hook."""
    try:
        if data.get("field") == "message_template_status_update":
            update_template_status(data['value'])

        elif data.get("field") == "messages":
            update_message_status(data['value'])
    except Exception as e:
        frappe.error_log(f"Error in update_status function: {e}", "WHATSAPP_WEBHOOK_STATUS_UPDATE_ERROR")


def update_template_status(data):
    """Update template status."""
    try:
        frappe.db.sql(
            """UPDATE `tabWhatsApp Templates`
               SET status = %(event)s
               WHERE id = %(message_template_id)s""",
            data
        )
    except Exception as e:
        frappe.error_log(f"Error updating template status for ID {data.get('message_template_id')}: {e}",
                         "WHATSAPP_WEBHOOK_TEMPLATE_UPDATE_ERROR")


def update_message_status(data):
    """Update message status."""
    try:
        id = data['statuses'][0]['id']
        status = data['statuses'][0]['status']
        conversation = data['statuses'][0].get('conversation', {}).get('id')
        name = frappe.db.get_value("WhatsApp Message", filters={"message_id": id})

        if name:
            doc = frappe.get_doc("WhatsApp Message", name)
            doc.status = status
            if conversation:
                doc.conversation_id = conversation
            doc.save(ignore_permissions=True)

    except Exception as e:
        frappe.error_log(f"Error updating message status for ID {data.get('statuses', [{}])[0].get('id')}: {e}",
                         "WHATSAPP_WEBHOOK_MESSAGE_STATUS_UPDATE_ERROR")
