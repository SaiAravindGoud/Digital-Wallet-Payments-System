
import logging
import smtplib
import ssl

from email.message import EmailMessage

from app.config import settings


logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, body: str) -> bool:
    """
    Send an email using Gmail SMTP over SSL.
    """

    if not settings.smtp_username or not settings.smtp_password:
        raise RuntimeError("SMTP credentials are not configured")

    sender = settings.smtp_from or settings.smtp_username

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    try:
        ssl_context = ssl.create_default_context()

        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=30,
            context=ssl_context,
        ) as server:

            server.login(
                settings.smtp_username.strip(),
                settings.smtp_password.replace(" ", "").strip(),
            )

            server.send_message(message)

        logger.info("Email sent successfully to %s", to_email)
        return True

    except (smtplib.SMTPException, OSError) as exc:
        logger.exception("Failed to send email to %s", to_email)
        raise RuntimeError("Email delivery failed") from exc
