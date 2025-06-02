import os
import json
import io
import logging
import azure.functions as func
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueServiceClient
from azure.core.exceptions import ResourceExistsError
from reportlab.pdfgen import canvas
from datetime import datetime, timezone

# Turn on debug‐level logging for the function worker
logging.basicConfig(level=logging.DEBUG)

def create_invoice_pdf(order: dict) -> bytes:
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, 800, "MyApp Invoice")
    p.setFont("Helvetica", 12)
    p.drawString(50, 770, f"Order ID: {order.get('id')}")
    p.drawString(50, 750, f"Product: {order.get('name')}")
    p.drawString(50, 730, f"Price: ₹{order.get('price')}")
    # Use datetime.now(timezone.utc) instead of utcnow()
    p.drawString(
        50,
        710,
        f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    p.drawString(50, 680, "Thank you for your order!")
    p.showPage()
    p.save()
    buffer.seek(0)
    return buffer.read()

app = func.FunctionApp()

@app.function_name(name="ProcessOrder")
@app.queue_trigger(
    arg_name="msg",
    queue_name="orders-queue",
    connection="AzureWebJobsStorage"
)
def process_order(msg: func.QueueMessage):
    logging.info("▶️ Entered ProcessOrder trigger")
    try:
        # ── Step 1: parse JSON ──────────────────────────────────────────
        try:
            order = msg.get_json()
            logging.debug(f"✅ Parsed JSON message = {order}")
        except Exception:
            logging.error("❌ Failed to parse message as JSON.", exc_info=True)
            raise

        # Verify required fields:
        if not all(k in order for k in ("id", "name", "price")):
            err = ValueError(f"Message missing required key(s): {order}")
            logging.error("❌ Message does not contain id/name/price", exc_info=True)
            raise err

        order_id = order["id"]
        product_name = order["name"]
        try:
            price = int(order["price"])
        except Exception:
            logging.error(f"❌ Could not convert price to int: {order['price']}", exc_info=True)
            raise

        # ── Step 2: generate PDF ─────────────────────────────────────────
        try:
            pdf_bytes = create_invoice_pdf({
                "id": order_id,
                "name": product_name,
                "price": price
            })
            logging.debug(f"✅ PDF bytes length = {len(pdf_bytes)}")
        except Exception:
            logging.error("❌ Error while generating PDF.", exc_info=True)
            raise

        # ── Step 3: upload to blob ───────────────────────────────────────
        try:
            connection_string = os.environ["AzureWebJobsStorage"]
        except KeyError:
            logging.error("❌ AzureWebJobsStorage not found in environment variables.", exc_info=True)
            raise

        try:
            blob_service = BlobServiceClient.from_connection_string(connection_string)
        except Exception:
            logging.error("❌ Could not create BlobServiceClient.", exc_info=True)
            raise

        try:
            container_client = blob_service.create_container("invoices")
            logging.debug("📦 Created container 'invoices'")
        except ResourceExistsError:
            container_client = blob_service.get_container_client("invoices")
            logging.debug("📦 Container 'invoices' already exists, reused client.")

        invoice_blob_name = f"invoice_{order_id}.pdf"
        try:
            container_client.upload_blob(
                name=invoice_blob_name,
                data=pdf_bytes,
                overwrite=True
            )
            logging.debug(f"✅ Uploaded blob: invoices/{invoice_blob_name}")
        except Exception:
            logging.error(f"❌ Failed to upload blob '{invoice_blob_name}'.", exc_info=True)
            raise

        # ── Step 4: enqueue downstream message ────────────────────────────
        try:
            queue_service = QueueServiceClient.from_connection_string(connection_string)
            queue_client = queue_service.get_queue_client("invoices-queue")
        except Exception:
            logging.error("❌ Could not create QueueServiceClient or queue client.", exc_info=True)
            raise

        try:
            queue_client.create_queue()
            logging.debug("📋 Created queue 'invoices-queue'")
        except ResourceExistsError:
            logging.debug("📋 Queue 'invoices-queue' already exists, skipping creation.")

        message_payload = {"id": order_id, "blobName": invoice_blob_name}
        message_text = json.dumps(message_payload)
        try:
            queue_client.send_message(message_text)
            logging.debug(f"✅ Sent message to 'invoices-queue': {message_payload}")
        except Exception:
            logging.error("❌ Failed to send message to invoices-queue.", exc_info=True)
            raise

        logging.info("✔️ ProcessOrder completed without errors.")

    except Exception:
        logging.error(
            "🔥 process_order is about to re‐throw an exception, so the host will retry/poison.",
            exc_info=True
        )
        raise
