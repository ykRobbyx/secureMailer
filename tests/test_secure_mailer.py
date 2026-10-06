import configparser
import email
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import secure_mailer as mailer


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / 'email_config.ini'
        self.config.write_text('[EMAIL]\nSENDER_EMAIL = sender@example.com\n'
                               'SMTP_SERVER = smtp.example.com\nSMTP_PORT = 587\n')
        config_patch = patch.object(mailer, 'CONFIG_FILE', str(self.config))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        smtp_patch = patch.object(mailer.smtplib, 'SMTP')
        self.smtp = smtp_patch.start()
        self.addCleanup(smtp_patch.stop)
        ssl_patch = patch.object(mailer.smtplib, 'SMTP_SSL')
        self.smtp_ssl = ssl_patch.start()
        self.addCleanup(ssl_patch.stop)
        self.server = self.smtp.return_value.__enter__.return_value
        self.server.sendmail.return_value = {}
        self.ssl_server = self.smtp_ssl.return_value.__enter__.return_value
        self.ssl_server.sendmail.return_value = {}

    def send(self, **kwargs):
        arguments = dict(sender_email='sender@example.com', password='test-password',
                         receiver_email='to@example.com', subject='Hello', body='Hello, 世界')
        arguments.update(kwargs)
        return mailer.send_email(**arguments)

    def test_recipients_bcc_privacy_unicode_and_tls(self):
        self.assertTrue(self.send(receiver_email=['to@example.com', 'to@example.com'],
                                  cc='cc@example.com', bcc=['hidden@example.com']))
        sender, recipients, text = self.server.sendmail.call_args[0]
        self.assertEqual(recipients, ['to@example.com', 'cc@example.com', 'hidden@example.com'])
        message = email.message_from_string(text)
        self.assertIsNone(message['Bcc'])
        self.assertNotIn('hidden@example.com', text)
        self.assertEqual(message['Cc'], 'cc@example.com')
        self.assertEqual(message.get_payload(0).get_payload(decode=True).decode(), 'Hello, 世界')
        context = self.server.starttls.call_args[1]['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual([call[0] for call in self.server.method_calls],
                         ['starttls', 'login', 'sendmail'])

    def test_invalid_inputs_never_connect(self):
        cases = [dict(sender_email='sender@example.com\n'), dict(receiver_email=[]),
                 dict(receiver_email=None), dict(receiver_email=42),
                 dict(cc=['valid@example.com', 'bad']), dict(bcc='bad'),
                 dict(subject='Hello\r\nBcc: injected@example.com')]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(self.send(**case))
        self.smtp.assert_not_called()
        self.smtp_ssl.assert_not_called()

    def test_attachment_round_trip_and_single_content_type(self):
        attachment = self.root / 'résumé.txt'
        attachment.write_bytes(b'attachment content\x00')
        self.assertTrue(self.send(attachment_paths=attachment))
        message = email.message_from_string(self.server.sendmail.call_args[0][2])
        part = message.get_payload(1)
        self.assertEqual(part.get_filename(), attachment.name)
        self.assertEqual(part.get_payload(decode=True), attachment.read_bytes())
        self.assertEqual(part.get_all('Content-Type'), ['text/plain'])

    def test_missing_or_directory_attachment_aborts(self):
        for path in (self.root / 'missing.txt', self.root):
            with self.subTest(path=path):
                self.assertFalse(self.send(attachment_paths=[path]))
        self.smtp.assert_not_called()

    def test_partial_delivery_is_not_success(self):
        self.server.sendmail.return_value = {'to@example.com': (550, b'Refused')}
        with self.assertLogs('SecureMailer', level='ERROR') as logs:
            self.assertFalse(self.send(cc='accepted@example.com'))
        self.assertIn('Partial delivery', logs.output[0])

    def test_ssl_port_465(self):
        self.config.write_text(self.config.read_text().replace('587', '465'))
        self.assertTrue(self.send())
        self.smtp.assert_not_called()
        self.ssl_server.starttls.assert_not_called()
        self.assertTrue(self.smtp_ssl.call_args[1]['context'].check_hostname)

    def test_explicit_ssl_mode(self):
        with self.config.open('a') as stream:
            stream.write('SMTP_SECURITY = ssl\n')
        self.assertTrue(self.send())
        self.smtp_ssl.assert_called_once()

    def test_tls_failure_does_not_authenticate_or_send(self):
        self.server.starttls.side_effect = ssl.SSLError('certificate verification failed')
        self.assertFalse(self.send())
        self.server.login.assert_not_called()
        self.server.sendmail.assert_not_called()

    def test_authentication_failure(self):
        self.server.login.side_effect = mailer.smtplib.SMTPAuthenticationError(535, b'Refused')
        self.assertFalse(self.send())
        self.server.sendmail.assert_not_called()

    def test_configuration_errors_do_not_exit_library(self):
        cases = ['', '[OTHER]\nx = y', '[EMAIL]\nSMTP_PORT = 587',
                 self.config.read_text().replace('587', 'invalid'),
                 self.config.read_text().replace('587', '0'),
                 self.config.read_text().replace('587', '65536'),
                 self.config.read_text() + 'SMTP_SECURITY = plaintext\n',
                 '[EMAIL\n']
        for config in cases:
            with self.subTest(config=config):
                self.config.write_text(config)
                with self.assertRaises(ValueError):
                    mailer.load_config()
                self.assertFalse(self.send())
        self.smtp.assert_not_called()

    def test_missing_config_creates_template_without_exiting(self):
        self.config.unlink()
        self.assertFalse(self.send())
        self.assertTrue(self.config.is_file())
        self.smtp.assert_not_called()

    def test_percent_in_sender_config_is_literal(self):
        self.config.write_text(self.config.read_text().replace('sender@', 'sender%tag@'))
        self.assertEqual(mailer.load_config()['sender_email'], 'sender%tag@example.com')

    def test_prompt_retries_all_invalid_optional_recipients(self):
        with patch('builtins.input', side_effect=['bad,worse', 'valid@example.com']), patch('builtins.print'):
            self.assertEqual(mailer.prompt_recipients('CC: '), ['valid@example.com'])

    def test_main_returns_failure_for_bad_configuration(self):
        self.config.write_text('')
        with patch('builtins.print'):
            self.assertEqual(mailer.main(), 1)


if __name__ == '__main__':
    unittest.main()
