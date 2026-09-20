"""Telegram Bot API delivery with acknowledgement IDs and no credential logging."""
import json
import re
import urllib.request
import uuid


class TelegramTransport:
    def __init__(self, token, chat_id, opener=None):
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
            raise ValueError('Telegram bot token is missing or malformed')
        self.chat_id = int(chat_id)
        self.bot_id = int(token.split(':', 1)[0])
        if self.chat_id <= 0:
            raise ValueError('Knowledge approvals require a private Telegram chat')
        self._url = 'https://api.telegram.org/bot' + token + '/'
        self._open = opener or urllib.request.urlopen

    def _request(self, method, data, content_type):
        request = urllib.request.Request(self._url + method, data=data,
                                         headers={'Content-Type': content_type})
        try:
            with self._open(request, timeout=15) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('Oversized response')
            envelope = json.loads(raw)
            result = envelope.get('result') if isinstance(envelope, dict) else None
            if not isinstance(result, dict) or envelope.get('ok') is not True:
                raise ValueError('No successful message acknowledgement')
            chat = result.get('chat')
            if (type(result.get('message_id')) is not int or result['message_id'] <= 0
                    or type(result.get('date')) is not int or not isinstance(chat, dict)
                    or chat.get('id') != self.chat_id):
                raise ValueError('Message acknowledgement does not match the recipient')
            return result
        except (OSError, ValueError, TypeError, KeyError):
            # URLError/HTTPError text may contain the bot token embedded in the URL.
            raise OSError('Telegram delivery could not be confirmed; inspect local ticket status') from None

    def send(self, text, silent=False):
        if not text or len(text.encode('utf-16-le')) // 2 > 4096:
            raise ValueError('Telegram text must fit the message limit')
        body = {'chat_id': self.chat_id, 'text': text, 'disable_notification': silent,
                'link_preview_options': {'is_disabled': True}}
        return self._request('sendMessage', json.dumps(body).encode(), 'application/json')

    def document(self, name, data, caption, silent=False):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or len(data) > 1_000_000:
            raise ValueError('Invalid approval document')
        if len(caption.encode('utf-16-le')) // 2 > 1024:
            raise ValueError('Approval document caption is too long')
        boundary = 'knowledge-' + uuid.uuid4().hex
        parts = []
        for key, value in {'chat_id': str(self.chat_id), 'caption': caption,
                           'disable_notification': 'true' if silent else 'false'}.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                          f'{value}\r\n').encode())
        parts += [(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                   f'filename="{name}"\r\nContent-Type: text/markdown; charset=utf-8\r\n\r\n').encode(),
                  data, f'\r\n--{boundary}--\r\n'.encode()]
        return self._request('sendDocument', b''.join(parts), 'multipart/form-data; boundary=' + boundary)
