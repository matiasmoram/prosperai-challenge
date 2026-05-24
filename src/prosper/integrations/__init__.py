"""F6 — Mail + Calendar: outbound-mail store + staff web router."""

from prosper.integrations.mail import MailMessage, MailStore, make_message

__all__ = ["MailMessage", "MailStore", "make_message"]
