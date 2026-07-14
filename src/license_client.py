"""Lemon Squeezy license activation & validation (commercial builds).

Model — MONTHLY SUBSCRIPTION (50 MYR/mo), enforced online:
  * The buyer subscribes on Lemon Squeezy and receives a license key whose
    expiry follows the subscription: every successful renewal pushes
    `expires_at` forward; cancellation / failed payment flips the key to
    "expired" on LS's side.
  * First launch → POST /v1/licenses/activate binds the key to THIS machine
    (an LS "instance"); we persist the proof in data/license.json.
  * Every ~24h we re-validate against /v1/licenses/validate — and sooner if
    the locally cached expires_at has passed (so a renewal unlocks promptly
    and a lapsed subscription locks promptly).
  * Offline grace: if the network / LS API is unreachable, the last GOOD
    check keeps the app alive for GRACE_SEC. Short on purpose: the bot
    needs internet to trade at all, so grace only really covers an LS
    outage. An explicit "valid: false" answer locks immediately — that's a
    lapsed subscription or revocation, not an outage.
  * Online attempts are throttled to one per minute so a locked install
    (UI polling every few seconds) never hammers the LS API.
  * data/license.json is bound to this Mac's IOPlatformUUID via HMAC, so
    copying the file to another machine fails closed; re-activating there
    instead is stopped by the key's activation_limit on LS's side.

Dev machines: MMT_LICENSE_ENFORCED=0 in .env turns the whole gate off.
Ship builds MUST NOT include a .env — and for real releases the packaged
entry point should pin enforcement on (see docs in the activation PR).

This is deterrence, not unbreakable DRM. The durable protection is selling
a compiled (Nuitka/PyArmor) build; this module just makes the honest path
easy and the dishonest path annoying and revocable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

from . import config as _cfg  # noqa: F401 — imported for its .env side-effect

ROOT = Path(__file__).resolve().parent.parent
LICENSE_FILE = ROOT / "data" / "license.json"

_API = "https://api.lemonsqueezy.com/v1/licenses"
_HDRS = {"Accept": "application/json"}

# Pin the key to YOUR store/product so a cheap key bought for some other
# Lemon Squeezy product can't activate this app. 0 = not pinned yet.
# TODO(release): fill these in after creating the product in Lemon Squeezy
# (both appear in the /validate response meta and in the LS dashboard URL).
LEMON_STORE_ID = 0
LEMON_PRODUCT_ID = 0

REVALIDATE_SEC = 24 * 3600        # silent online re-check cadence
GRACE_SEC = 3 * 24 * 3600         # offline survival on the last good check
ONLINE_COOLDOWN_SEC = 60          # min spacing between LS API attempts
_SALT = b"mmt-license-v1|f3a9c1e07d5b4288"

_lock = threading.Lock()
# Last online attempt: t=epoch, ok=True/False/None (None = network soft-fail),
# msg=detail. Lets a locked/offline install answer from memory instead of
# re-hitting the LS API on every polled request.
_online_last = {"t": 0.0, "ok": True, "msg": ""}


def _enforced() -> bool:
    return os.getenv("MMT_LICENSE_ENFORCED", "1") != "0"


# ── machine identity ──────────────────────────────────────────────────────────

def _hw_uuid() -> str:
    """Stable per-Mac hardware UUID (survives renames/reinstalls of the app)."""
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return f"mac-{uuid.getnode():012x}"   # fallback: primary MAC address


def _fp() -> str:
    return hashlib.sha256(_SALT + _hw_uuid().encode()).hexdigest()


def _sig_key() -> bytes:
    return hmac.new(_SALT, _hw_uuid().encode(), hashlib.sha256).digest()


def _sign(st: dict) -> str:
    msg = "|".join(str(st.get(k, "")) for k in
                   ("key", "instance_id", "fp", "activated_at", "last_ok", "expires_at"))
    return hmac.new(_sig_key(), msg.encode(), hashlib.sha256).hexdigest()


def _parse_ts(s) -> int:
    """ISO timestamp from the LS API → epoch (0 = none/unparseable)."""
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


# ── local state ───────────────────────────────────────────────────────────────

def _load() -> dict | None:
    try:
        st = json.loads(LICENSE_FILE.read_text())
    except Exception:
        return None
    if not isinstance(st, dict) or not st.get("key"):
        return None
    return st


def _save(st: dict) -> None:
    st["sig"] = _sign(st)
    LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LICENSE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(LICENSE_FILE)


def _bound_here(st: dict) -> bool:
    return (hmac.compare_digest(st.get("sig", ""), _sign(st))
            and st.get("fp") == _fp())


# ── Lemon Squeezy API ─────────────────────────────────────────────────────────

def _meta_ok(j: dict) -> bool:
    meta = j.get("meta") or {}
    if LEMON_STORE_ID and meta.get("store_id") != LEMON_STORE_ID:
        return False
    if LEMON_PRODUCT_ID and meta.get("product_id") != LEMON_PRODUCT_ID:
        return False
    return True


def _invalid_msg(j: dict) -> str:
    status = str((j.get("license_key") or {}).get("status", ""))
    if status == "expired":
        return "订阅已到期（取消或扣款失败）— 续费后重新打开本页即可解锁"
    if status == "disabled":
        return "license 已被停用，请联系卖家"
    return str(j.get("error") or "license 已失效")


def _online_validate(st: dict) -> tuple[bool | None, str, dict]:
    """One /validate round-trip. True/False are definitive; None = soft
    failure (network / 5xx / garbage response) → caller applies grace.
    Third element is the license_key object (expiry etc.) when present."""
    try:
        r = requests.post(f"{_API}/validate", headers=_HDRS, timeout=10,
                          data={"license_key": st["key"],
                                "instance_id": st.get("instance_id", "")})
    except Exception as e:
        return None, f"授权服务器不可达：{type(e).__name__}", {}
    if r.status_code >= 500:
        return None, f"授权服务器错误（HTTP {r.status_code}）", {}
    try:
        j = r.json()
    except Exception:
        return None, "授权服务器响应异常", {}
    lk = j.get("license_key") or {}
    if j.get("valid"):
        if not _meta_ok(j):
            return False, "此 license 不属于本产品", lk
        return True, "ok", lk
    return False, _invalid_msg(j), lk


def _grace_verdict(st: dict, detail: str) -> tuple[bool, str]:
    """Network unreachable → live off the last good check, briefly."""
    age = time.time() - float(st.get("last_ok") or 0)
    if 0 <= age < GRACE_SEC:
        left_h = int((GRACE_SEC - age) / 3600)
        return True, f"离线宽限中（约剩 {left_h} 小时）：{detail}"
    return False, f"超过 {GRACE_SEC // 86400} 天无法在线验证 license：{detail}"


# ── public API ────────────────────────────────────────────────────────────────

def activate(key: str) -> tuple[bool, str]:
    """Bind `key` to this machine. Returns (ok, message-for-the-user)."""
    key = (key or "").strip()
    if len(key) < 8:
        return False, "请输入有效的 license key"
    name = f"{socket.gethostname()}#{_fp()[:10]}"
    try:
        r = requests.post(f"{_API}/activate", headers=_HDRS, timeout=15,
                          data={"license_key": key, "instance_name": name})
        j = r.json()
    except Exception as e:
        return False, f"无法连接授权服务器：{type(e).__name__}"
    if not j.get("activated"):
        err = str(j.get("error") or "激活失败")
        low = err.lower()
        if "activation limit" in low:
            return False, ("此 key 的激活台数已用完 — 请先在旧机器上解绑"
                           "（设置 → 授权 → 解绑），或联系卖家。")
        if "not found" in low:
            return False, "license key 不存在，请检查是否复制完整"
        if "expired" in low or str((j.get("license_key") or {}).get("status")) == "expired":
            return False, "此 key 的订阅已到期，请先续费再激活"
        return False, err
    if not _meta_ok(j):
        return False, "此 license 不属于本产品"
    inst = (j.get("instance") or {}).get("id", "")
    meta = j.get("meta") or {}
    lk = j.get("license_key") or {}
    now = int(time.time())
    with _lock:
        _save({
            "key": key,
            "instance_id": inst,
            "fp": _fp(),
            "email": meta.get("customer_email", ""),
            "activated_at": now,
            "last_ok": now,
            "expires_at": _parse_ts(lk.get("expires_at")),
        })
        _online_last.update(t=time.time(), ok=True, msg="ok")
    return True, "已激活"


def deactivate() -> tuple[bool, str]:
    """Unbind this machine so the buyer can move to a new Mac. Requires the
    LS API to confirm — otherwise the activation slot would stay burned."""
    st = _load()
    if not st:
        return True, "本机未激活"
    try:
        r = requests.post(f"{_API}/deactivate", headers=_HDRS, timeout=15,
                          data={"license_key": st["key"],
                                "instance_id": st.get("instance_id", "")})
        j = r.json()
    except Exception as e:
        return False, f"解绑需要联网：{type(e).__name__}"
    err = str(j.get("error") or "")
    if j.get("deactivated") or "not found" in err.lower():
        # "not found" = instance already gone on LS's side → local file is stale
        with _lock:
            LICENSE_FILE.unlink(missing_ok=True)
        return True, "已解绑，可在新机器上重新激活"
    return False, err or "解绑失败"


def _cache_ok(st: dict) -> bool:
    """Is the last good check still trustworthy without going online?"""
    now = time.time()
    last_ok = float(st.get("last_ok") or 0)
    if now < last_ok - 7200:            # clock rolled back → distrust cache
        return False
    exp = float(st.get("expires_at") or 0)
    if exp and now > exp:               # subscription window lapsed → re-check
        return False                    # (a renewal will have pushed exp forward)
    return 0 <= now - last_ok < REVALIDATE_SEC


def validate(force: bool = False) -> tuple[bool, str]:
    """Cheap cached check, safe to call on every request. Hits the network at
    most once per ONLINE_COOLDOWN_SEC (or when `force`)."""
    if not _enforced():
        return True, "dev (未强制)"
    st = _load()
    if not st:
        return False, "未激活"
    if not _bound_here(st):
        return False, "license 文件与本机不匹配，请重新激活"
    if not force and _cache_ok(st):
        return True, "ok"
    with _lock:
        # Re-read: another thread may have refreshed while we waited.
        st = _load()
        if not st or not _bound_here(st):
            return False, "license 文件与本机不匹配，请重新激活"
        if not force and _cache_ok(st):
            return True, "ok"
        # Throttle: a locked/expired install polls this on every request —
        # answer from the last online attempt instead of re-hitting LS.
        if not force and time.time() - _online_last["t"] < ONLINE_COOLDOWN_SEC:
            if _online_last["ok"] is False:
                return False, _online_last["msg"]
            return _grace_verdict(st, _online_last["msg"])
        ok, msg, lk = _online_validate(st)
        _online_last.update(t=time.time(), ok=ok, msg=msg)
        if ok:
            st["last_ok"] = int(time.time())
            exp = _parse_ts(lk.get("expires_at"))
            if exp:
                st["expires_at"] = exp
            _save(st)
            return True, "ok"
        if ok is False:
            return False, msg
        return _grace_verdict(st, msg)


def status() -> dict:
    """Non-sensitive snapshot for the UI (activation page + settings)."""
    st = _load() or {}
    key = st.get("key", "")
    ok, msg = validate()
    return {
        "enforced": _enforced(),
        "activated": bool(st),
        "valid": ok,
        "msg": msg,
        "key_masked": (key[:4] + "…" + key[-4:]) if len(key) >= 12 else ("已设置" if key else ""),
        "email": st.get("email", ""),
        "activated_at": st.get("activated_at"),
        "last_ok": st.get("last_ok"),
        "expires_at": st.get("expires_at") or None,
        "machine": socket.gethostname(),
    }
