#!/usr/bin/env python3
"""Print sanitized Telegram bot/destination identity for the QCC trigger monitor.
Never prints bot token or numeric chat ID.
"""
from __future__ import annotations
import json
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote

ENV = Path('/home/ubuntu/openclaw_workspace/qcc-chatgpt-ops/chatgpt-trigger.env')

def read_env(path: Path) -> dict[str,str]:
    out = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k,v = line.split('=',1)
        v=v.strip()
        if len(v)>=2 and v[0]==v[-1] and v[0] in "'\"":
            v=v[1:-1]
        out[k.strip()] = v
    return out

def get_json(url: str) -> dict:
    with urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))

def main() -> int:
    env = read_env(ENV)
    token = env.get('TELEGRAM_BOT_TOKEN','')
    chat_id = env.get('TELEGRAM_CHAT_ID','')
    if not token or not chat_id:
        print('Telegram configuration incomplete')
        return 2
    me = get_json(f'https://api.telegram.org/bot{token}/getMe').get('result',{})
    chat = get_json(f'https://api.telegram.org/bot{token}/getChat?chat_id={quote(chat_id)}').get('result',{})
    print('bot_username=@' + str(me.get('username','unknown')))
    print('bot_name=' + str(me.get('first_name','unknown')))
    print('destination_type=' + str(chat.get('type','unknown')))
    if chat.get('title'):
        print('destination_title=' + str(chat['title']))
    if chat.get('username'):
        print('destination_username=@' + str(chat['username']))
    if chat.get('first_name'):
        name = str(chat['first_name'])
        if chat.get('last_name'):
            name += ' ' + str(chat['last_name'])
        print('destination_name=' + name)
    print('numeric_chat_id=REDACTED')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
