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
    """Post."""
    data = frappe.local.form_dict

    # 1. Log Incoming Webhook Data
    frappe.get_doc({
        "doctype": "WhatsApp Notification Log",
        "template": "Webhook",
        "meta_data": json.dumps(data)
    }).insert(ignore_permissions=True)

    messages = []
    phone_id = None
    sender_profile_name = None

    # 2. Data Extraction

    # Path 1: Extract messages and phone_id
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

    # Attempt to extract phone_id again if it was missed in the first pass
    if not phone_id:
        phone_id = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("metadata", {}).get(
            "phone_number_id")

    whatsapp_account = get_whatsapp_account(phone_id) if phone_id else None
    if not whatsapp_account:
        # If no account is found, check if it's a status update
        if not messages:
            changes = data.get("entry", [{}])[0].get("changes", [{}])[0]
            if changes:
                update_status(changes)
        return

    if messages:
        for message in messages:
            # 3. Individual Message Processing
            message_type = message.get('type')
            message_id = message.get('id', 'N/A')

            # Robustly determine if it's a reply
            is_reply = bool(message.get('context') and message.get('context', {}).get('id'))
            reply_to_message_id = message.get('context', {}).get('id') if is_reply else None

            common_fields = {
                "doctype": "WhatsApp Message",
                "type": "Incoming",
                "from": message.get('from'),
                "message_id": message_id,
                "reply_to_message_id": reply_to_message_id,
                "is_reply": is_reply,
                "content_type": message_type,
                "profile_name": sender_profile_name,
                "whatsapp_account": whatsapp_account.name
            }

            if message_type == 'text':
                frappe.get_doc({
                    **common_fields,
                    "message": message['text']['body'],
                }).insert(ignore_permissions=True)

            elif message_type == 'reaction':
                frappe.get_doc({
                    **common_fields,
                    "message": message['reaction']['emoji'],
                    "reply_to_message_id": message['reaction']['message_id'],
                }).insert(ignore_permissions=True)

            elif message_type == 'location':
                location_data = message.get('location')
                message_text = f"{json.dumps(location_data)}"
                frappe.get_doc({
                    **common_fields,
                    "message": message_text,
                }).insert(ignore_permissions=True)

            # FIX: Changed 'contact' to 'contacts' (plural) to match the webhook payload
            elif message_type == 'contacts':
                contact_messages = []
                for contact in message.get('contacts', []):
                    contact_name = contact.get('name', {}).get('formatted_name', 'Unknown Name')

                    phone_number = next(
                        (phone.get('wa_id') for phone in contact.get('phones', []) if phone.get('wa_id')),
                        'No WA ID'
                    )
                    contact_messages.append(f"{contact_name} ({phone_number})")

                frappe.get_doc({
                    **common_fields,
                    "message": " | ".join(contact_messages) or "Contact shared",
                }).insert(ignore_permissions=True)

            elif message_type == 'interactive':
                interactive_data = message['interactive']
                message_text = "Interactive message received"
                if 'button_reply' in interactive_data:
                    message_text = interactive_data['button_reply']['title']
                elif 'list_reply' in interactive_data:
                    message_text = interactive_data['list_reply'].get('title', message_text)
                elif 'nfm_reply' in interactive_data:
                    message_text = interactive_data['nfm_reply'].get('response_json', message_text)

                frappe.get_doc({
                    **common_fields,
                    "message": message_text,
                    "is_reply": True,
                }).insert(ignore_permissions=True)

            elif message_type in ["image", "audio", "video", "document", "sticker"]:
                # --- Media Handling Block ---
                token = whatsapp_account.get_password("token")
                url = f"{whatsapp_account.url}/{whatsapp_account.version}/"

                media_id = message[message_type]["id"]
                headers = {'Authorization': 'Bearer ' + token}

                # 1. Get Media Metadata
                response = requests.get(f'{url}{media_id}/', headers=headers)

                if response.status_code == 200:
                    media_data = response.json()
                    media_url = media_data.get("url")
                    mime_type = media_data.get("mime_type")
                    file_extension = mime_type.split('/')[-1]

                    # 2. Download Media File
                    media_response = requests.get(media_url, headers=headers)
                    if media_response.status_code == 200:
                        file_data = media_response.content
                        file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

                        message_doc_dict = {
                            **common_fields,
                            "message": message[message_type].get("caption", f"/files/{file_name}"),
                        }

                        message_doc = frappe.get_doc(message_doc_dict)
                        message_doc.insert(ignore_permissions=True)

                        # 3. Create File Doc and attach
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

                        # 4. Update WhatsApp Message with file URL
                        message_doc.attach = file_doc.file_url
                        message_doc.save(ignore_permissions=True)

            elif message_type == "button":
                button_text = message.get('button', {}).get('text', 'Button reply received')
                frappe.get_doc({
                    **common_fields,
                    "message": button_text,
                }).insert(ignore_permissions=True)

            # FIX: Simplified fallback to prevent AttributeError on list types (like 'contacts')
            else:
                message_content = f"Unhandled message type received: {message_type}"
                frappe.get_doc({
                    **common_fields,
                    "message_id": message_id,
                    "message": message_content,
                }).insert(ignore_permissions=True)


    else:
        # 4. Status Update Handling
        changes = data["entry"][0]["changes"][0]
        update_status(changes)
    return


# --------------------------------------------------------------------------------------
# STATUS UPDATE FUNCTIONS
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

    if name:
        doc = frappe.get_doc("WhatsApp Message", name)
        doc.status = status
        if conversation:
            doc.conversation_id = conversation
        doc.save(ignore_permissions=True)
