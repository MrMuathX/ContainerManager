import logging
import httpx
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional
from app.config import settings, NOTIFICATION_SETTINGS_FILE
from app.models import NotificationSettings
from app.services.ai_gateway import ask_ai

logger = logging.getLogger(__name__)

async def send_notification(title: str, message_body: str, cause: str = None, effect: str = None, recommendation: str = None, links: List[dict] = None):
    """
    Sends notification via enabled providers.
    links: list of {"text": "Start", "url": "..."}
    """
    conf = settings.notification_settings
    
    # Construct Full Message
    html_content = f"<h2>{title}</h2>"
    if cause: html_content += f"<p><b>Cause:</b> {cause}</p>"
    if effect: html_content += f"<p><b>Effect:</b> {effect}</p>"
    if recommendation: html_content += f"<p><b>Recommendation:</b> {recommendation}</p>"
    html_content += f"<p>{message_body}</p>"
    
    if links:
        html_content += "<h3>Actions:</h3>"
        for link in links:
            html_content += f' <a href="{link["url"]}" style="padding: 10px; background: #007bff; color: white; text-decoration: none; border-radius: 5px;">{link["text"]}</a> '

    plain_text = f"{title}\n\n"
    if cause: plain_text += f"Cause: {cause}\n"
    if effect: plain_text += f"Effect: {effect}\n"
    if recommendation: plain_text += f"Recommendation: {recommendation}\n"
    plain_text += f"\n{message_body}\n"
    if links:
        plain_text += "\nActions:\n"
        for link in links:
            plain_text += f"- {link['text']}: {link['url']}\n"

    # Dispatch to providers
    if "email" in conf.enabled_providers:
        await _send_email(title, html_content, plain_text)
    
    if "telegram" in conf.enabled_providers:
        await _send_telegram(plain_text)
        
    if "mqtt" in conf.enabled_providers:
        await _send_mqtt(title, plain_text)
        
    if "webhook" in conf.enabled_providers:
        await _send_webhook(title, plain_text, cause, effect, recommendation)

async def _send_email(subject: str, html: str, text: str):
    conf = settings.notification_settings.email
    if not all([conf.smtp_host, conf.smtp_user, conf.smtp_password, conf.to_email]):
        logger.warning("Email config incomplete")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = conf.from_email or conf.smtp_user
    msg["To"] = conf.to_email

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        if conf.smtp_port == 465:
            server = smtplib.SMTP_SSL(conf.smtp_host, conf.smtp_port)
        else:
            server = smtplib.SMTP(conf.smtp_host, conf.smtp_port)
            if conf.use_tls:
                server.starttls()
        
        server.login(conf.smtp_user, conf.smtp_password)
        server.sendmail(msg["From"], [msg["To"]], msg.as_string())
        server.quit()
        logger.info("Email notification sent")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

async def _send_telegram(text: str):
    conf = settings.notification_settings.telegram
    if not conf.bot_token or not conf.chat_id:
        logger.warning("Telegram config incomplete")
        return
    
    url = f"https://api.telegram.org/bot{conf.bot_token}/sendMessage"
    payload = {"chat_id": conf.chat_id, "text": text}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Telegram notification sent")
    except Exception as e:
        logger.error(f"Failed to send Telegram: {e}")

async def _send_mqtt(title: str, text: str):
    from app.services.ha_mqtt import AIOMQTT_AVAILABLE
    if not settings.MQTT_ENABLED or not AIOMQTT_AVAILABLE:
        return
    
    import aiomqtt
    conf = settings.notification_settings.mqtt
    try:
        async with aiomqtt.Client(
            hostname=settings.MQTT_HOST,
            port=settings.MQTT_PORT,
            username=settings.MQTT_USER or None,
            password=settings.MQTT_PASSWORD or None,
            identifier=settings.MQTT_CLIENT_ID + "_notifier",
        ) as client:
            payload = json.dumps({"title": title, "message": text})
            await client.publish(conf.topic, payload=payload)
            logger.info("MQTT notification sent")
    except Exception as e:
        logger.error(f"Failed to send MQTT notification: {e}")

async def _send_webhook(title: str, text: str, cause: str, effect: str, recommendation: str):
    conf = settings.notification_settings.webhook
    if not conf.url:
        return
    
    payload = {
        "title": title,
        "message": text,
        "details": {
            "cause": cause,
            "effect": effect,
            "recommendation": recommendation
        }
    }
    
    try:
        async with httpx.AsyncClient() as client:
            if conf.method.upper() == "POST":
                resp = await client.post(conf.url, json=payload, headers=conf.headers, timeout=10)
            else:
                resp = await client.get(conf.url, params=payload, headers=conf.headers, timeout=10)
            resp.raise_for_status()
            logger.info("Webhook notification sent")
    except Exception as e:
        logger.error(f"Failed to send Webhook: {e}")

async def generate_ai_notification(event_type: str, container_name: str, logs: Optional[str] = None):
    """
    Uses AI to generate Cause, Effect, and Recommendation.
    """
    prompt = f"""Identify the cause, effect, and recommendation for the following Docker event:
Event: {event_type}
Container Name: {container_name}
"""
    if logs:
        prompt += f"\nRecent Logs:\n{logs}"
        
    prompt += "\n\nFormat the response strictly as a JSON object with keys: cause, effect, recommendation."
    
    try:
        response = await ask_ai(prompt)
        # Try to parse JSON from AI response
        # Sometimes AI wraps it in backticks
        clean_resp = response.strip()
        if "```json" in clean_resp:
            clean_resp = clean_resp.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_resp:
            clean_resp = clean_resp.split("```")[1].split("```")[0].strip()
            
        data = json.loads(clean_resp)
        return data.get("cause", ""), data.get("effect", ""), data.get("recommendation", "")
    except Exception as e:
        logger.error(f"AI generation failed: {e}")
        return "Manual check required", "Container issue detected", "Inspect logs and state"
async def test_notification_provider(provider: str, config: NotificationSettings):
    """
    Tests a specific provider with provided configuration.
    """
    test_title = f"Test Notification ({provider.capitalize()})"
    test_body = "This is a test notification from ContainerManager to verify your settings are correct."
    test_cause = "Manual Test Trigger"
    test_effect = "Verification of delivery channel"
    test_recommendation = "No action required if received"
    
    html = f"<h2>{test_title}</h2><p>{test_body}</p>"
    text = f"{test_title}\n\n{test_body}"

    try:
        if provider == "email":
            # Temporarily override settings if needed, but the internal _send_* use local config if passed or settings.notification_settings
            # Actually _send_email uses settings.notification_settings.email
            # Need to refactor _send_* to accept config or use instances
            pass # See below for better implementation
        
        # Refactoring _send functions to accept config override
        if provider == "email":
            await _send_email_direct(test_title, html, text, config.email)
        elif provider == "telegram":
            await _send_telegram_direct(text, config.telegram)
        elif provider == "mqtt":
            await _send_mqtt_direct(test_title, text, config.mqtt)
        elif provider == "webhook":
            await _send_webhook_direct(test_title, text, test_cause, test_effect, test_recommendation, config.webhook)
        
        return {"success": True, "message": f"Test notification sent to {provider}"}
    except Exception as e:
        logger.error(f"Test failed for {provider}: {e}")
        return {"success": False, "message": str(e)}

async def _send_email_direct(subject: str, html: str, text: str, conf):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = conf.from_email or conf.smtp_user
    msg["To"] = conf.to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    if conf.smtp_port == 465:
        server = smtplib.SMTP_SSL(conf.smtp_host, conf.smtp_port)
    else:
        server = smtplib.SMTP(conf.smtp_host, conf.smtp_port)
        if conf.use_tls:
            server.starttls()
    server.login(conf.smtp_user, conf.smtp_password)
    server.sendmail(msg["From"], [msg["To"]], msg.as_string())
    server.quit()

async def _send_telegram_direct(text: str, conf):
    url = f"https://api.telegram.org/bot{conf.bot_token}/sendMessage"
    payload = {"chat_id": conf.chat_id, "text": text}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=10)
        resp.raise_for_status()

async def _send_mqtt_direct(title: str, text: str, conf):
    import aiomqtt
    async with aiomqtt.Client(
        hostname=settings.MQTT_HOST,
        port=settings.MQTT_PORT,
        username=settings.MQTT_USER or None,
        password=settings.MQTT_PASSWORD or None,
        identifier=settings.MQTT_CLIENT_ID + "_test",
    ) as client:
        payload = json.dumps({"title": title, "message": text})
        await client.publish(conf.topic, payload=payload)

async def _send_webhook_direct(title: str, text: str, cause: str, effect: str, recommendation: str, conf):
    payload = {"title": title, "message": text, "details": {"cause": cause, "effect": effect, "recommendation": recommendation}}
    async with httpx.AsyncClient() as client:
        if conf.method.upper() == "POST":
            resp = await client.post(conf.url, json=payload, headers=conf.headers, timeout=10)
        else:
            resp = await client.get(conf.url, params=payload, headers=conf.headers, timeout=10)
        resp.raise_for_status()
