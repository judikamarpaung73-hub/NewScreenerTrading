"""Telegram transport; explicit caller opt-in. Does not log tokens/URLs."""
import json
import time
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from .utils import logger


class TelegramClient:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"

    def _call(self, method, payload, timeout=20):
        req = Request(self.base+"/"+method, data=urlencode(payload).encode(),
                      headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urlopen(req, timeout=timeout) as response:
            data = json.load(response)
        if not data.get("ok"):
            raise RuntimeError("Telegram API rejected request")
        return data["result"]

    def send_message(self, text, retries=2):
        if not self.token or not self.chat_id:
            return False
        for attempt in range(retries):
            try:
                self._call("sendMessage", dict(chat_id=self.chat_id,text=text,parse_mode="HTML",disable_web_page_preview=True))
                return True
            except Exception as exc:
                logger.error("Telegram send failed: %s", type(exc).__name__)
                if attempt+1 < retries:
                    time.sleep(1)
        return False

    def get_updates(self, offset=None, timeout=30):
        payload = {"timeout":timeout}
        if offset is not None:
            payload["offset"] = offset
        try:
            return self._call("getUpdates",payload,timeout+10)
        except Exception as exc:
            logger.error("Telegram polling failed: %s",type(exc).__name__)
            time.sleep(3)
            return []

    def download_file(self, file_id, dest_path):
        try:
            info = self._call("getFile",{"file_id":file_id})
            if info.get("file_size",0) > 10_000_000:
                return False
            url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
            with urlopen(url,timeout=30) as r:
                data = r.read(10_000_001)
            if len(data) > 10_000_000:
                return False
            with open(dest_path,"wb") as f:
                f.write(data)
            return True
        except Exception as exc:
            logger.error("Telegram download failed: %s",type(exc).__name__)
            return False
