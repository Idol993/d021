import json
import smtplib
import base64
import hashlib
import hmac
import time
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import config


class NotificationSeverity:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    SUCCESS = "success"


class Notifier:
    def __init__(self):
        self.enabled = config.get("notification.enabled", True)
        channels_cfg = config.get("notification.channels", {})
        self.wework_cfg = channels_cfg.get("wework", {})
        self.dingtalk_cfg = channels_cfg.get("dingtalk", {})
        self.email_cfg = channels_cfg.get("email", {})

    def send(
        self,
        title: str,
        content: str,
        severity: str = NotificationSeverity.INFO,
        extra: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "channels": {}}

        results: Dict[str, Any] = {}

        if self.wework_cfg.get("enabled"):
            results["wework"] = self._send_wework(title, content, severity, extra)

        if self.dingtalk_cfg.get("enabled"):
            results["dingtalk"] = self._send_dingtalk(title, content, severity, extra)

        if self.email_cfg.get("enabled"):
            results["email"] = self._send_email(title, content, severity, extra, attachments)

        return {"status": "sent", "channels": results}

    def _send_wework(
        self,
        title: str,
        content: str,
        severity: str,
        extra: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            webhook_url = self.wework_cfg["webhook_url"]
            color_map = {
                NotificationSeverity.INFO: "info",
                NotificationSeverity.SUCCESS: "green",
                NotificationSeverity.WARNING: "warning",
                NotificationSeverity.CRITICAL: "red",
            }
            msg_content = f"**{title}**\n\n{content}"
            if extra:
                msg_content += "\n\n---\n"
                for k, v in extra.items():
                    msg_content += f"\n> **{k}**: {v}"

            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": msg_content,
                },
            }
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp_data = resp.json()
            return {"success": resp_data.get("errcode") == 0, "response": resp_data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _send_dingtalk(
        self,
        title: str,
        content: str,
        severity: str,
        extra: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            webhook_url = self.dingtalk_cfg["webhook_url"]
            secret = self.dingtalk_cfg.get("secret")

            if secret:
                timestamp = str(round(time.time() * 1000))
                string_to_sign = f"{timestamp}\n{secret}"
                hmac_code = hmac.new(
                    secret.encode("utf-8"),
                    string_to_sign.encode("utf-8"),
                    digestmod=hashlib.sha256,
                ).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
                webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

            msg_content = f"### {title}\n\n{content}\n"
            if extra:
                msg_content += "\n---\n"
                for k, v in extra.items():
                    msg_content += f"\n- **{k}**: {v}"

            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": msg_content,
                },
            }
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp_data = resp.json()
            return {"success": resp_data.get("errcode") == 0, "response": resp_data}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _send_email(
        self,
        title: str,
        content: str,
        severity: str,
        extra: Optional[Dict[str, Any]],
        attachments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        try:
            smtp_host = self.email_cfg["smtp_host"]
            smtp_port = self.email_cfg["smtp_port"]
            use_ssl = self.email_cfg.get("use_ssl", True)
            username = self.email_cfg["username"]
            password = self.email_cfg["password"]
            sender = self.email_cfg.get("sender", username)
            recipients = self.email_cfg.get("recipients", [])

            msg = MIMEMultipart()
            msg["From"] = sender
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = f"[{severity.upper()}] {title}"

            html_content = f"""
            <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6;">
                <h2 style="color: {self._severity_color(severity)};">{title}</h2>
                <div style="margin: 20px 0;">{content.replace(chr(10), '<br>')}</div>
            """
            if extra:
                html_content += "<hr><div><ul>"
                for k, v in extra.items():
                    html_content += f"<li><strong>{k}</strong>: {v}</li>"
                html_content += "</ul></div>"
            html_content += "</body></html>"

            msg.attach(MIMEText(html_content, "html", "utf-8"))

            if attachments:
                for att_path in attachments:
                    p = Path(att_path)
                    if p.exists():
                        part = MIMEBase("application", "octet-stream")
                        with open(p, "rb") as f:
                            part.set_payload(f.read())
                        encoders.encode_base64(part)
                        part.add_header(
                            "Content-Disposition",
                            f"attachment; filename={p.name}",
                        )
                        msg.attach(part)

            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
                server.starttls()
            server.login(username, password)
            server.sendmail(sender, recipients, msg.as_string())
            server.quit()
            return {"success": True, "recipients": recipients}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _severity_color(severity: str) -> str:
        return {
            NotificationSeverity.INFO: "#2196F3",
            NotificationSeverity.SUCCESS: "#4CAF50",
            NotificationSeverity.WARNING: "#FF9800",
            NotificationSeverity.CRITICAL: "#F44336",
        }.get(severity, "#000000")


notifier = Notifier()
