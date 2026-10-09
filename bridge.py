#!/usr/bin/env python3
"""WeChat iLink / Clawbot bridge (stdlib only + qrcode for the login QR image).

Commands:
  login       Fetch a login QR code from Tencent iLink, save QR PNG.
  wait-login  Poll QR status until confirmed; persist session (bot_token etc.).
  serve       Long-poll getupdates -> inbox/, flush outbox/pending -> sendmessage.
  send        Send one text using the cached context_token from the last user msg.
  status      Show login / serve status (never prints the token).

Secrets: state/session.json holds bot_token. Never log it, never print it.
Token death signal is ret/errcode == -14 (no refresh); serve writes
state/NEEDS_RELOGIN and exits so the watchdog can alert the user.
"""

import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
INBOX_DIR = os.path.join(ROOT, "inbox")
PENDING_DIR = os.path.join(ROOT, "outbox", "pending")
SENT_DIR = os.path.join(ROOT, "outbox", "sent")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")
PENDING_LOGIN_PATH = os.path.join(STATE_DIR, "login_pending.json")
LAST_CONTEXT_PATH = os.path.join(STATE_DIR, "last_context.json")
CURSOR_PATH = os.path.join(STATE_DIR, "get_updates_buf.txt")
NEEDS_RELOGIN_PATH = os.path.join(STATE_DIR, "NEEDS_RELOGIN")
PID_PATH = os.path.join(STATE_DIR, "serve.pid")
QR_PNG_PATH = os.path.join(ROOT, "login_qr.png")

DEFAULT_BASEURL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
APP_CLIENT_VERSION = "132102"
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "Muse-WeChat-Bridge/1.0.0"
ACK_TEXT = "收到，我想想…"
ACK_COOLDOWN_SECS = 60

_rng = random.SystemRandom()


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def wechat_uin():
    return base64.b64encode(str(_rng.randint(0, 2**32 - 1)).encode()).decode()


def common_headers(token=None, minimal=False):
    headers = {"Content-Type": "application/json"}
    if not minimal:
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["X-WECHAT-UIN"] = wechat_uin()
    headers["iLink-App-Id"] = APP_ID
    headers["iLink-App-ClientVersion"] = APP_CLIENT_VERSION
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def http_json(method, url, headers, body=None, timeout=40):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
    except Exception as e:  # network hiccup etc.
        return {"_error": str(e)}
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_error": "non-json response", "_raw_head": raw[:200]}


def atomic_write_json(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_session():
    return load_json(SESSION_PATH)


def api_base(session=None):
    if session and session.get("baseurl"):
        return str(session["baseurl"]).rstrip("/")
    return DEFAULT_BASEURL


def errcode_of(resp):
    for key in ("errcode", "ret"):
        val = resp.get(key)
        if isinstance(val, int) and val != 0:
            return val
    return 0


class NeedsRelogin(Exception):
    pass


def check_token_alive(resp):
    if errcode_of(resp) == -14:
        raise NeedsRelogin(resp.get("errmsg", "session expired (-14)"))


def mark_needs_relogin(reason=""):
    atomic_write_json(NEEDS_RELOGIN_PATH, {"at": datetime.now(timezone.utc).isoformat(), "reason": str(reason)})


# ---------------------------------------------------------------- login ----

def cmd_login(_args):
    """POST get_bot_qrcode (no Authorization, no base_info), render QR PNG."""
    url = f"{DEFAULT_BASEURL}/ilink/bot/get_bot_qrcode?bot_type=3"
    resp = http_json("POST", url, common_headers(), body={"local_token_list": []}, timeout=30)
    if "_error" in resp:
        print(f"login failed: {resp['_error']}", file=sys.stderr)
        return 1
    qrcode = resp.get("qrcode")
    img_content = resp.get("qrcode_img_content")
    if not qrcode or not img_content:
        print(f"login failed: unexpected response keys={sorted(resp.keys())}", file=sys.stderr)
        return 1
    atomic_write_json(PENDING_LOGIN_PATH, {"qrcode": qrcode, "created_at": datetime.now(timezone.utc).isoformat()})
    try:
        import qrcode  # pip package; only used to render the login QR image
        img = qrcode.make(img_content)
        img.save(QR_PNG_PATH)
    except ImportError:
        print("qrcode package missing; run: pip install qrcode", file=sys.stderr)
        return 1
    print(f"QR_READY path={QR_PNG_PATH}")
    return 0


def cmd_wait_login(_args):
    """Long-poll get_qrcode_status until confirmed; persist session.json."""
    pending = load_json(PENDING_LOGIN_PATH)
    if not pending or not pending.get("qrcode"):
        print("no pending login; run `login` first", file=sys.stderr)
        return 1
    qrcode = pending["qrcode"]
    url = f"{DEFAULT_BASEURL}/ilink/bot/get_qrcode_status?qrcode={urllib.request.quote(qrcode)}"
    headers = common_headers(minimal=True)  # only iLink-App-Id + ClientVersion
    deadline = time.time() + 300  # QR login sessions live ~5 minutes
    while time.time() < deadline:
        resp = http_json("GET", url, headers, timeout=40)
        if "_error" in resp:
            log(f"poll error: {resp['_error']}; retrying")
            time.sleep(2)
            continue
        status = str(resp.get("status", "")).lower()
        if status in ("confirmed", "binded_redirect"):
            session = {
                "bot_token": resp.get("bot_token"),
                "baseurl": resp.get("baseurl") or DEFAULT_BASEURL,
                "ilink_bot_id": resp.get("ilink_bot_id"),
                "ilink_user_id": resp.get("ilink_user_id"),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            if not session["bot_token"]:
                print("confirmed but no bot_token in response", file=sys.stderr)
                return 1
            atomic_write_json(SESSION_PATH, session)
            try:
                os.remove(PENDING_LOGIN_PATH)
            except FileNotFoundError:
                pass
            if os.path.exists(NEEDS_RELOGIN_PATH):
                os.remove(NEEDS_RELOGIN_PATH)
            log(f"LOGIN_OK bot_id={session['ilink_bot_id']} user_id={session['ilink_user_id']}")
            return 0
        if status == "expired":
            print("QR expired; run `login` again", file=sys.stderr)
            return 1
        if status in ("scaned_but_redirect",) and resp.get("redirect_host"):
            url = f"https://{resp['redirect_host']}/ilink/bot/get_qrcode_status?qrcode={urllib.request.quote(qrcode)}"
        # status wait/scaned/unknown -> keep polling
        time.sleep(1)
    print("wait-login timed out (5 min); run `login` again", file=sys.stderr)
    return 1


# ----------------------------------------------------------------- send ----

def send_message(session, to_user_id, text, context_token):
    """POST /ilink/bot/sendmessage. Returns response dict."""
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": f"muse-{uuid.uuid4().hex}",
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
            "context_token": context_token,
        },
        "base_info": base_info(),
    }
    resp = http_json("POST", f"{api_base(session)}/ilink/bot/sendmessage",
                     common_headers(token=session.get("bot_token")), body=body, timeout=30)
    check_token_alive(resp)
    return resp


def cmd_send(args):
    session = load_session()
    if not session or not session.get("bot_token"):
        print("not logged in; run login + wait-login first", file=sys.stderr)
        return 1
    ctx = load_json(LAST_CONTEXT_PATH)
    if not ctx or not ctx.get("context_token"):
        print("no cached context_token yet: the user must DM the bot first", file=sys.stderr)
        return 1
    to_user = args.to or ctx.get("to_user_id") or session.get("ilink_user_id")
    try:
        resp = send_message(session, to_user, args.text, ctx["context_token"])
    except NeedsRelogin as e:
        mark_needs_relogin(str(e))
        print("token expired (-14); NEEDS_RELOGIN written", file=sys.stderr)
        return 2
    if "_error" in resp or errcode_of(resp) != 0:
        print(f"send failed: ret={resp.get('ret')} errcode={resp.get('errcode')} errmsg={resp.get('errmsg')} net={resp.get('_error')}", file=sys.stderr)
        return 1
    print("SEND_OK")
    return 0


# ---------------------------------------------------------------- serve ----

def extract_text(msg):
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            text = (item.get("text_item") or {}).get("text")
            if text:
                return text
    return None


def flush_outbox(session):
    """Send every outbox/pending/*.json; move successes to outbox/sent/."""
    try:
        names = sorted(os.listdir(PENDING_DIR))
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(PENDING_DIR, name)
        payload = load_json(path)
        if not payload or not payload.get("text"):
            continue
        try:
            resp = send_message(session, payload.get("to_user_id", ""),
                                str(payload["text"]), payload.get("context_token", ""))
        except NeedsRelogin:
            raise
        if "_error" in resp or errcode_of(resp) != 0:
            log(f"outbox send failed file={name} ret={resp.get('ret')} err={resp.get('_error')}")
            continue
        os.replace(path, os.path.join(SENT_DIR, name))
        log(f"outbox sent file={name}")


def cmd_serve(_args):
    session = load_session()
    if not session or not session.get("bot_token"):
        print("not logged in; run login + wait-login first", file=sys.stderr)
        return 1
    for d in (STATE_DIR, INBOX_DIR, PENDING_DIR, SENT_DIR):
        os.makedirs(d, exist_ok=True)
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    whitelist = str(session.get("ilink_user_id") or "")

    def is_owner(uid):
        if not whitelist or not uid:
            return False
        return uid == whitelist or uid.split("@")[0] == whitelist.split("@")[0]

    cursor = ""
    last_ack_ts = 0.0
    try:
        with open(CURSOR_PATH, "r", encoding="utf-8") as f:
            cursor = f.read().strip()
    except FileNotFoundError:
        pass

    log(f"serve started pid={os.getpid()} owner={whitelist}")
    while True:
        try:
            resp = http_json("POST", f"{api_base(session)}/ilink/bot/getupdates",
                             common_headers(token=session.get("bot_token")),
                             body={"get_updates_buf": cursor, "base_info": base_info()},
                             timeout=45)
            if "_error" in resp:
                time.sleep(3)
                continue
            check_token_alive(resp)
            if errcode_of(resp) != 0:
                log(f"getupdates ret={resp.get('ret')} errcode={resp.get('errcode')} msg={resp.get('errmsg')}")
                time.sleep(3)
                continue
            new_cursor = resp.get("get_updates_buf")
            if new_cursor:
                cursor = new_cursor
                with open(CURSOR_PATH, "w", encoding="utf-8") as f:
                    f.write(cursor)
            for msg in resp.get("msgs") or []:
                if msg.get("group_id"):
                    continue  # 1:1 only; ignore group messages
                if msg.get("message_type") not in (1, None):
                    continue  # only USER messages (1); BOT echoes are 2
                sender = str(msg.get("from_user_id") or "")
                if not is_owner(sender):
                    continue  # only the owner's own WeChat ID
                text = extract_text(msg)
                if not text:
                    continue
                record = {
                    "message_id": msg.get("message_id") or msg.get("seq") or uuid.uuid4().hex,
                    "text": text,
                    "from_user_id": sender,
                    "context_token": msg.get("context_token"),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
                fname = f"msg_{record['message_id']}.json"
                atomic_write_json(os.path.join(INBOX_DIR, fname), record, mode=0o644)
                atomic_write_json(LAST_CONTEXT_PATH,
                                  {"to_user_id": sender, "context_token": record["context_token"]})
                log(f"inbox <- {sender}: {text[:80]}")
                now = time.time()
                if record["context_token"] and now - last_ack_ts > ACK_COOLDOWN_SECS:
                    try:
                        ack_resp = send_message(session, sender, ACK_TEXT, record["context_token"])
                        if "_error" not in ack_resp and errcode_of(ack_resp) == 0:
                            last_ack_ts = now
                            log("ack sent")
                    except NeedsRelogin:
                        raise
                    except Exception as e:
                        log(f"ack failed: {e}")
            flush_outbox(session)
        except NeedsRelogin as e:
            mark_needs_relogin(str(e))
            log("token expired (-14); NEEDS_RELOGIN written; serve exiting")
            return 2
        except KeyboardInterrupt:
            log("serve stopped by KeyboardInterrupt")
            return 0
        except Exception as e:  # keep the loop alive on transient faults
            log(f"serve loop error: {e}")
            time.sleep(3)


# --------------------------------------------------------------- status ----

def cmd_status(_args):
    session = load_session()
    logged_in = bool(session and session.get("bot_token"))
    print(f"logged_in: {logged_in}")
    if session:
        print(f"bot_id: {session.get('ilink_bot_id')}")
        print(f"user_id: {session.get('ilink_user_id')}")
        print(f"baseurl: {session.get('baseurl')}")
        print(f"session_saved_at: {session.get('saved_at')}")
    pid_alive = False
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        pid_alive = True
        print(f"serve_pid: {pid} (alive)")
    except Exception:
        print("serve_pid: not running")
    def count(d):
        try:
            return len([n for n in os.listdir(d) if n.endswith(".json")])
        except FileNotFoundError:
            return 0
    print(f"inbox_files: {count(INBOX_DIR)}")
    print(f"outbox_pending: {count(PENDING_DIR)}")
    print(f"outbox_sent: {count(SENT_DIR)}")
    print(f"needs_relogin: {os.path.exists(NEEDS_RELOGIN_PATH)}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="WeChat iLink bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("wait-login")
    sub.add_parser("serve")
    p_send = sub.add_parser("send")
    p_send.add_argument("--text", required=True)
    p_send.add_argument("--to", default=None)
    sub.add_parser("status")
    args = parser.parse_args()
    handlers = {
        "login": cmd_login,
        "wait-login": cmd_wait_login,
        "serve": cmd_serve,
        "send": cmd_send,
        "status": cmd_status,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
