import smtplib
import ssl
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os
import re
import configparser
import logging
from pathlib import Path
import sys
import getpass

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('SecureMailer')

# Config file location
CONFIG_FILE = 'email_config.ini'

def create_default_config():
    """Create a default config file if it doesn't exist"""
    config = configparser.ConfigParser()
    config['EMAIL'] = {
        'SENDER_EMAIL': 'youremail@example.com',
        'SMTP_SERVER': 'smtp.gmail.com',
        'SMTP_PORT': '587'
    }
    
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)
    logger.info(f"Created default config file at {CONFIG_FILE}")
    logger.info("Please update the config file with your email settings")

def load_config():
    """Load and validate configuration; raise ValueError for invalid settings."""
    if not os.path.exists(CONFIG_FILE):
        create_default_config()
        raise ValueError("Edit the new config file with your email settings and run again.")

    config = configparser.ConfigParser(interpolation=None)
    try:
        with open(CONFIG_FILE) as configfile:
            config.read_file(configfile)
        if 'EMAIL' not in config:
            raise ValueError("Missing EMAIL configuration section")
        settings = config['EMAIL']
        for key in ('SENDER_EMAIL', 'SMTP_SERVER', 'SMTP_PORT'):
            if not settings.get(key, '').strip():
                raise ValueError("Missing required configuration: " + key)
        port = settings.getint('SMTP_PORT')
        if not 1 <= port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if not is_valid_email(settings['SENDER_EMAIL']):
            raise ValueError("SENDER_EMAIL must be a valid email address")
        security = settings.get('SMTP_SECURITY', 'ssl' if port == 465 else 'starttls').lower()
        if security not in ('starttls', 'ssl'):
            raise ValueError("SMTP_SECURITY must be starttls or ssl")
    except (OSError, configparser.Error) as exc:
        raise ValueError("Unable to read email configuration: " + str(exc)) from exc

    return {
        'sender_email': settings['SENDER_EMAIL'],
        'smtp_server': settings['SMTP_SERVER'],
        'smtp_port': port,
        'smtp_security': security,
    }


def is_valid_email(email):
    """Validate plain email addresses, including rejecting header newlines."""
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return isinstance(email, str) and re.fullmatch(pattern, email) is not None


def normalize_recipients(value, label, required=False):
    """Accept an address or a list/tuple and validate every recipient."""
    if value is None:
        value = []
    elif isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(label + " must be an address or a list of addresses")
    if required and not value:
        raise ValueError("At least one " + label + " address is required")
    if any(not is_valid_email(address) for address in value):
        raise ValueError("Invalid " + label + " email address")
    return list(dict.fromkeys(value))


def send_email(sender_email, password, receiver_email, subject, body, attachment_paths=None, cc=None, bcc=None):
    """Send using verified TLS. Return True only if all recipients are accepted.

    Recipients accept an address or a list/tuple. Requested attachments must all
    be readable; failures abort before connecting. False can indicate partial
    delivery, so inspect the logs before retrying to avoid duplicate messages.
    """
    try:
        if not is_valid_email(sender_email):
            raise ValueError("Invalid sender email address")
        receivers = normalize_recipients(receiver_email, 'recipient', required=True)
        cc = normalize_recipients(cc, 'CC')
        bcc = normalize_recipients(bcc, 'BCC')
        if not isinstance(subject, str) or any(char in subject for char in '\r\n'):
            raise ValueError("Subject must be text without line breaks")
        config = load_config()

        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = ', '.join(receivers)
        msg['Subject'] = subject
        if cc:
            msg['Cc'] = ', '.join(cc)
        all_recipients = list(dict.fromkeys(receivers + cc + bcc))
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if isinstance(attachment_paths, (str, os.PathLike)):
            attachment_paths = [attachment_paths]
        for attachment_path in attachment_paths or []:
            path = Path(attachment_path)
            content_type, encoding = mimetypes.guess_type(str(path))
            if not content_type or encoding:
                content_type = 'application/octet-stream'
            maintype, subtype = content_type.split('/', 1)
            part = MIMEBase(maintype, subtype)
            # Read every requested file before connecting; never omit one silently.
            part.set_payload(path.read_bytes())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment', filename=path.name)
            msg.attach(part)

        text = msg.as_string()
        context = ssl.create_default_context()
        logger.info("Connecting to %s:%s...", config['smtp_server'], config['smtp_port'])
        use_ssl = config['smtp_security'] == 'ssl'
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        kwargs = {'timeout': 30}
        if use_ssl:
            kwargs['context'] = context
        with smtp_class(config['smtp_server'], config['smtp_port'], **kwargs) as server:
            if not use_ssl:
                server.starttls(context=context)
            server.login(sender_email, password)
            refused = server.sendmail(sender_email, all_recipients, text)
            if refused:
                logger.error("Partial delivery: %d recipient(s) refused. Other recipients may "
                             "have received the message; do not retry the full list blindly.", len(refused))
                return False
        logger.info("Email accepted by the SMTP server for all recipients.")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("Authentication failed. Check your email and app password.")
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)
    return False


def prompt_recipients(prompt, required=False):
    """Prompt again rather than silently dropping invalid recipients."""
    while True:
        value = input(prompt).strip()
        addresses = [address.strip() for address in value.split(',')] if value else []
        try:
            return normalize_recipients(addresses, 'recipient', required=required)
        except ValueError as exc:
            print(str(exc))


def main():
    """Main function for running the script directly"""
    print("📧 SecureMailer - Email Sending Utility 🔐")
    print("------------------------------------------")
    
    # Load configuration
    try:
        config = load_config()
    except Exception as e:
        logger.error(f"Failed to load configuration: {str(e)}")
        return 1
    
    # Get email details
    sender_email = config['sender_email']
    print(f"Sender email: {sender_email}")
    
    receiver_list = prompt_recipients(
        "Enter recipient email address(es) (comma-separated): ", required=True)
    cc_list = prompt_recipients("Enter CC email address(es) (or Enter to skip): ")
    bcc_list = prompt_recipients("Enter BCC email address(es) (or Enter to skip): ")

    # Get subject
    subject = input("Enter email subject: ")
    
    # Get body with multi-line support
    print("Enter email body (press Enter twice when done):")
    body = []
    while True:
        line = input()
        if not line and not body:
            # Don't end on first empty line if body is empty
            continue
        if not line and body and body[-1] == "":
            # End on second consecutive empty line
            break
        body.append(line)
    body_text = "\n".join(body)
    
    # Ask for attachments
    attachments = []
    print("Enter attachment paths (or press Enter to skip/finish):")
    while True:
        attachment = input("> ")
        if not attachment:
            break
        path = Path(attachment)
        if not path.is_file():
            logger.warning(f"Not a readable file path: {attachment}")
            continue
        attachments.append(str(path))
        logger.info(f"Added attachment: {path.name}")
    
    # Get password securely
    password = getpass.getpass("Enter your email password (input will be hidden): ")
    
    # Send the email
    success = send_email(
        sender_email=sender_email,
        password=password,
        receiver_email=receiver_list,
        subject=subject,
        body=body_text,
        attachment_paths=attachments if attachments else None,
        cc=cc_list if cc_list else None,
        bcc=bcc_list if bcc_list else None
    )
    
    if success:
        return 0
    else:
        logger.error("Sending did not fully succeed. Check the errors above before retrying.")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(130)
