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
        frappe.log_error(f"Error handling media message of type {message_type}: {e}", "WHATSAPP_WEBHOOK_MEDIA_ERROR")


def handle_incoming_message(message, whatsapp_account, sender_profile_name):
    """Processes a single incoming message based on its type."""
    message_type = message.get('type')
    is_reply = bool(message.get('context'))
    reply_to_message_id = message['context']['id'] if is_reply else None

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
            "reply_to_message_id": message['reaction']['message_id'],  # reaction's target is in reaction key
            "content_type": "reaction",
            "is_reply": True,  # reactions are always replies
        }).insert(ignore_permissions=True)

    # Interactive covers list replies and quick reply buttons
    elif message_type == 'interactive':
        # Determine if it's a button reply or a list response
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
            # For Flow (NFM) replies
            message_text = interactive_data['nfm_reply']['response_json']  # Store the JSON response
            content_type = "flow_reply"

        if message_text:
            frappe.get_doc({
                **common_fields,
                "message": message_text,
                "content_type": content_type,
                "is_reply": True,  # Interactive messages are always replies to a previous message
            }).insert(ignore_permissions=True)

    elif message_type == 'location':
        location_data = message['location']
        # Store coordinates and address information in a structured or combined way
        message_text = f"Lat: {location_data.get('latitude')}, Lon: {location_data.get('longitude')}"
        if location_data.get('name'):
            message_text += f", Name: {location_data['name']}"
        if location_data.get('address'):
            message_text += f", Address: {location_data['address']}"

        frappe.get_doc({
            **common_fields,
            "message": message_text,
            "content_type": message_type,
        }).insert(ignore_permissions=True)

    elif message_type == 'contact':
        # Contacts is a list of contacts. Extract name and phone number.
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
        # Use the raw message object for the message content if possible, or just the type
        message_data_key = message_type
        message_content = message.get(message_data_key, {}).get(message_data_key, f"Unhandled type: {message_type}")

        frappe.get_doc({
            **common_fields,
            "message": message_content,
            "content_type": message_type,
        }).insert(ignore_permissions=True)


def post():
    """Post."""
    data = frappe.local.form_dict
    frappe.get_doc({
        "doctype": "WhatsApp Notification Log",
        "template": "Webhook",
        "meta_data": json.dumps(data)
    }).insert(ignore_permissions=True)

    messages = []
    phone_id = None
    try:
        # Extract messages and phone_id from the nested structure
        # This is the standard path for single entry/change
        messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
        phone_id = data["entry"][0]["changes"][0]["value"].get("metadata", {}).get("phone_number_id")
    except (KeyError, IndexError):
        # Fallback for other structures or empty arrays (e.g., status updates)
        try:
            # Attempt to extract messages from the first change in the first entry
            messages = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [])
        except (KeyError, IndexError):
            pass  # messages remain []

        try:
            # Attempt to extract phone_id from the first change in the first entry
            phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get(
                "phone_number_id")
        except (KeyError, IndexError):
            pass  # phone_id remains None

    sender_profile_name = next(
        (
            contact.get("profile", {}).get("name")
            for entry in data.get("entry", [])
            for change in entry.get("changes", [])
            for contact in change.get("value", {}).get("contacts", [])
        ),
        None,
    )

    whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None
    if not whatsapp_account:
        # If it's a status update, it might not have a phone_id from the message structure,
        # but we need the account to check for templates/messages. If it's *only* a status update
        # and no account is found, we can proceed to update status without an account reference
        # if the update_status function is robust enough, but for incoming messages we need it.

        # Let's ensure update_status is called if there are no messages
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
        changes = None
        try:
            changes = data["entry"][0]["changes"][0]
        except (KeyError, IndexError):
            # Fallback for nested structure if necessary
            changes = data.get("entry", [{}])[0].get("changes", [{}])[0]

        if changes:
            update_status(changes)
    return


# --------------------------------------------------------------------------------------
# ORIGINAL/UNMODIFIED STATUS UPDATE FUNCTIONS FOLLOW
# --------------------------------------------------------------------------------------

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
