"""User-initiated Muse native OTP login; temporary credentials stay in memory.

Protocol was read from Muse's public login JavaScript on 2026-11-05:
restart -> optional start -> send-otp -> confirm-otp (or resend-otp).
This adapter never registers accounts, accepts terms, resets passwords, or
attempts to pass checkpoints. A Muse session must be verified before import.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re
import secrets
import threading
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit
import uuid

import requests


SITE = "https://muse.ai"
AUTH_BASE = SITE + "/api/auth/native/"
ACTIVE_STATES = {"starting", "awaiting_code", "verifying"}
PUBLIC_ACCOUNT_FIELDS = {
    "id", "label", "enabled", "ok", "session_ok", "workspace_available",
    "vm_state", "note", "expires_at", "cookie_count",
}
UNSUPPORTED_MESSAGE = "此账号需要在 Muse 官网完成额外登录步骤；请先在官网完成，再使用手动导入。"


class NativeLoginError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _FlowStopped(Exception):
    pass


class _ProtocolFailure(Exception):
    pass


class _Unsupported(Exception):
    pass


@dataclass(repr=False)
class _Flow:
    login_id: str
    label: str
    session: requests.Session = field(repr=False)
    expires_at: float
    contact_hint: str = ""
    state: str = "starting"
    message: str = "正在准备登录。"
    csrf: str = field(default="", repr=False)
    attempts: int = 0
    next_send: float = 0
    account: dict | None = None
    timer: object | None = field(default=None, repr=False)
    operation: threading.Lock = field(default_factory=threading.Lock, repr=False)


class NativeLoginService:
    def __init__(self, on_authenticated: Callable, *, session_factory=requests.Session,
                 clock=time.time, timer_factory=threading.Timer, ttl: int = 600,
                 resend_interval: int = 60, max_verify_attempts: int = 5):
        self._save = on_authenticated
        self._session_factory = session_factory
        self._clock = clock
        self._timer_factory = timer_factory
        self._ttl = ttl
        self._interval = resend_interval
        self._max_attempts = max_verify_attempts
        self._lock = threading.RLock()
        self._flows: dict[str, _Flow] = {}
        self._active_id: str | None = None
        self._send_times: deque[float] = deque()
        self._closed = False

    @staticmethod
    def _identifier(value: str) -> str:
        value = value.strip() if isinstance(value, str) else ""
        if not value or len(value) > 254 or any(ord(c) < 32 for c in value):
            raise NativeLoginError(400, "请输入有效的 Muse 邮箱或手机号。")
        if "@" in value:
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                raise NativeLoginError(400, "请输入有效的邮箱地址。")
        elif not re.fullmatch(r"\+?[0-9 ()-]{7,25}", value):
            raise NativeLoginError(400, "请输入邮箱或包含区号的手机号。")
        return value

    @staticmethod
    def _mask(identifier: str) -> str:
        if "@" in identifier:
            local, domain = identifier.rsplit("@", 1)
            return local[:1] + "***@" + domain
        return "***" + re.sub(r"\D", "", identifier)[-4:]

    def _public(self, flow: _Flow) -> dict:
        with self._lock:
            result = {
                "login_id": flow.login_id,
                "state": flow.state,
                "message": flow.message,
                "contact_hint": flow.contact_hint,
                "expires_at": int(flow.expires_at),
                "resend_after": max(0, int(flow.next_send - self._clock() + 0.999)),
            }
            if flow.account is not None:
                result["account"] = {k: v for k, v in flow.account.items()
                                     if k in PUBLIC_ACCOUNT_FIELDS}
            return result

    @staticmethod
    def _destroy(flow: _Flow):
        flow.csrf = ""
        if flow.timer is not None:
            flow.timer.cancel()
            flow.timer = None
        try:
            flow.session.cookies.clear()
        finally:
            flow.session.close()

    def _end(self, flow: _Flow, state: str, message: str):
        with self._lock:
            flow.state, flow.message = state, message
            if self._active_id == flow.login_id:
                self._active_id = None
            self._destroy(flow)

    def _expire(self, login_id: str):
        with self._lock:
            flow = self._flows.get(login_id)
            if flow and flow.state in ACTIVE_STATES:
                self._end(flow, "expired", "本次登录已过期，请重新获取验证码。")

    def _assert_active(self, flow: _Flow):
        with self._lock:
            if self._clock() >= flow.expires_at and flow.state in ACTIVE_STATES:
                self._end(flow, "expired", "本次登录已过期，请重新获取验证码。")
            if self._closed or flow.state not in ACTIVE_STATES:
                raise _FlowStopped()

    def _get(self, login_id: str) -> _Flow:
        with self._lock:
            flow = self._flows.get(login_id)
            if flow is None:
                raise NativeLoginError(404, "找不到本次登录，请重新开始。")
            if self._clock() >= flow.expires_at and flow.state in ACTIVE_STATES:
                self._expire(login_id)
            return flow

    def _rate_check(self):
        now = self._clock()
        while self._send_times and now - self._send_times[0] >= 3600:
            self._send_times.popleft()
        if self._send_times and now - self._send_times[-1] < self._interval:
            raise NativeLoginError(429, "验证码发送过于频繁，请等待一分钟后重试。")
        if len(self._send_times) >= 6:
            raise NativeLoginError(429, "本小时验证码发送次数已达上限，请稍后重试。")

    def _send_attempt(self, flow: _Flow):
        with self._lock:
            self._assert_active(flow)
            self._rate_check()
            now = self._clock()
            self._send_times.append(now)
            flow.next_send = now + self._interval

    def _request(self, flow: _Flow, method: str, url: str, payload=None, *, csrf=True):
        self._assert_active(flow)
        headers = {}
        if csrf and flow.csrf:
            headers["X-Hatch-CSRF-Token"] = flow.csrf
        kwargs = {"headers": headers, "timeout": (5, 15), "allow_redirects": False}
        if payload is not None:
            kwargs["json"] = payload
        try:
            response = flow.session.request(method, url, **kwargs)
            self._assert_active(flow)
            return response
        except _FlowStopped:
            self._destroy(flow)  # Clear any cookies received after cancellation.
            raise

    @staticmethod
    def _json(response) -> dict:
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise _ProtocolFailure("Muse 登录响应无法识别，请稍后重试。") from None
        if not isinstance(data, dict):
            raise _ProtocolFailure("Muse 登录响应无法识别，请稍后重试。")
        return data

    @staticmethod
    def _problem(response) -> str:
        try:
            data = response.json()
            kind = data.get("type", "") if isinstance(data, dict) else ""
        except (ValueError, TypeError):
            return ""
        return kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", kind) else ""

    def _check(self, response):
        if 200 <= response.status_code < 300:
            return
        kind = self._problem(response)
        if kind in {"checkpointRequired", "regionNotSupported", "additionalVerificationRequired",
                    "twoFactorMethodUnsupported", "registrationDisabled", "registrationBlocked",
                    "accountSelectionRequired", "confirmationFailed", "pendingConfirmation"}:
            raise _Unsupported(UNSUPPORTED_MESSAGE)
        if kind in {"authFlowExpired", "csrfFailed", "session_invalid"}:
            raise _ProtocolFailure("登录会话已过期或验证失败，请重新开始。")
        if response.status_code == 429 or kind == "tooManyAttempts":
            raise _ProtocolFailure("Muse 暂时限制了登录尝试，请稍后重试。")
        if response.status_code in (401, 403):
            raise _Unsupported("Muse 未允许本次登录，请在官网检查账号和额外验证要求。")
        raise _ProtocolFailure("Muse 登录请求未成功，请稍后重试。")

    def _perform(self, flow: _Flow, action: Callable):
        if not flow.operation.acquire(blocking=False):
            raise NativeLoginError(409, "上一步登录请求仍在处理中，请稍候。")
        try:
            self._assert_active(flow)
            action()
        except _FlowStopped:
            pass
        except _Unsupported as exc:
            self._end(flow, "unsupported", str(exc))
        except _ProtocolFailure as exc:
            self._end(flow, "failed", str(exc))
        except requests.RequestException:
            self._end(flow, "failed", "无法连接 Muse 登录服务，请稍后重试。")
        except NativeLoginError:
            raise
        except Exception:
            self._end(flow, "failed", "本次登录未完成，请重试；登录信息未输出到日志。")
        finally:
            with self._lock:
                if flow.state not in ACTIVE_STATES:
                    self._destroy(flow)
            flow.operation.release()
        return self._public(flow)

    def start(self, identifier: str, label: str = "") -> dict:
        identifier = self._identifier(identifier)
        label = label.strip()[:120] if isinstance(label, str) else ""
        with self._lock:
            if self._closed:
                raise NativeLoginError(503, "登录服务正在关闭，请稍后重试。")
            if self._active_id:
                existing = self._get(self._active_id)
                if existing.state in ACTIVE_STATES:
                    raise NativeLoginError(409, "已有登录正在等待完成，请先完成或取消。")
            self._rate_check()
            session = self._session_factory()
            session.headers.update({
                "Accept": "application/json", "Content-Type": "application/json",
                "Origin": SITE, "Referer": SITE + "/",
                "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
                "X-Hatch-Waterfall-Id": str(uuid.uuid4()),
                "X-Hatch-Caa-Reg-Entry-Point": "login_home",
            })
            flow = _Flow(secrets.token_urlsafe(24), label or self._mask(identifier),
                         session, self._clock() + self._ttl, contact_hint=self._mask(identifier))
            self._flows[flow.login_id] = flow
            self._active_id = flow.login_id
            for old_id in list(self._flows)[:-20]:
                if old_id != self._active_id:
                    self._flows.pop(old_id)
            flow.timer = self._timer_factory(self._ttl, lambda: self._expire(flow.login_id))
            flow.timer.daemon = True
            flow.timer.start()

        def begin():
            response = self._request(flow, "POST", AUTH_BASE + "restart", {}, csrf=False)
            self._check(response)
            data = self._json(response)
            csrf_token = data.get("csrf_token")
            if not isinstance(csrf_token, str) or not 8 <= len(csrf_token) <= 4096:
                raise _ProtocolFailure("Muse 未返回有效登录会话，请重试。")
            flow.csrf = csrf_token
            state = data.get("flow_state")
            if state == "new":
                response = self._request(flow, "POST", AUTH_BASE + "start", {})
                self._check(response)
                data = self._json(response)
                state = data.get("flow_state", "started")
            if state != "started":
                raise _Unsupported(UNSUPPORTED_MESSAGE)
            self._send_attempt(flow)
            response = self._request(flow, "POST", AUTH_BASE + "send-otp", {"contact_point": identifier})
            self._check(response)
            self._await_code(flow, self._json(response))

        return self._perform(flow, begin)

    def _await_code(self, flow: _Flow, data: dict):
        state = data.get("registration_state")
        if state != "otp_sent":
            if state == "password_required":
                raise _Unsupported("Muse 要求此账号使用密码登录；本入口仅处理验证码，请在官网完成登录后导入。")
            raise _Unsupported(UNSUPPORTED_MESSAGE)
        with self._lock:
            self._assert_active(flow)
            flow.state = "awaiting_code"
            flow.message = "验证码已发送，请填写你收到的验证码。"

    def verify(self, login_id: str, code: str) -> dict:
        flow = self._get(login_id)
        if flow.state not in ACTIVE_STATES:
            return self._public(flow)
        code = code.strip() if isinstance(code, str) else ""
        if not re.fullmatch(r"[0-9]{4,12}", code):
            raise NativeLoginError(400, "请输入收到的数字验证码。")

        def confirm():
            if flow.state != "awaiting_code":
                raise NativeLoginError(409, "请先获取验证码。")
            if flow.attempts >= self._max_attempts:
                raise _ProtocolFailure("验证码尝试次数已达上限，请重新开始登录。")
            flow.attempts += 1
            response = self._request(flow, "POST", AUTH_BASE + "confirm-otp", {"otp_code": code})
            kind = self._problem(response) if response.status_code >= 400 else ""
            if kind in {"invalidCode", "invalidOtp", "invalidOTP", "otpInvalid", "otpExpired", "codeExpired"}:
                if flow.attempts >= self._max_attempts:
                    raise _ProtocolFailure("验证码尝试次数已达上限，请重新开始登录。")
                flow.message = "验证码不正确或已过期，请检查后重新输入，或重新发送。"
                return
            if 300 <= response.status_code < 400:
                target = urljoin(SITE + "/", response.headers.get("Location", ""))
                parsed = urlsplit(target)
                if parsed.scheme != "https" or parsed.netloc != "muse.ai" or parsed.username or parsed.password:
                    raise _Unsupported(UNSUPPORTED_MESSAGE)
                # A redirect is followed only to the same Muse origin, with no OTP body.
                redirected = self._request(flow, "GET", target, csrf=False)
                self._check(redirected)
            else:
                self._check(response)
                data = self._json(response)
                state = data.get("registration_state")
                # The official UI has no fixed success-state enum: it handles
                # extra steps, then completes by using the issued session.
                if state in {
                    "two_factor_required", "account_selection_required", "completion_required",
                    "ntp_upsell", "registration_required", "password_required", "otp_sent",
                    "new", "started", "reactivation_required", "linking_disclosure_required",
                    "registration_blocked", "ntp_upsell_restart_required",
                }:
                    raise _Unsupported(UNSUPPORTED_MESSAGE)
                if data.get("reactivation_required") or data.get("checkpoint_url"):
                    raise _Unsupported(UNSUPPORTED_MESSAGE)
            self._complete(flow)

        return self._perform(flow, confirm)

    def _complete(self, flow: _Flow):
        cookies, expires = self._cookies(flow)
        if not cookies.get("hatch_sess"):
            raise _Unsupported("Muse 登录还需要在官网完成后续验证，尚未取得可用会话。")
        response = self._request(flow, "GET", SITE + "/api/session", csrf=False)
        self._check(response)
        session_result = self._json(response)
        if session_result.get("status") != "assigned":
            raise _Unsupported("登录已验证，但 Muse 工作区尚未就绪，请先在官网完成剩余步骤。")
        cookies, expires = self._cookies(flow)
        if not cookies.get("hatch_sess"):
            raise _ProtocolFailure("Muse 未保留有效登录会话，请重新登录。")
        with self._lock:
            self._assert_active(flow)
            # Cancellation cannot race the final import. Only sanitized account data is returned.
            account = self._save(cookies, expires, flow.label, session_result)
            flow.account = account if isinstance(account, dict) else None
            message = "登录成功，账号已保存。"
            if str(session_result.get("vm_state", "")).upper() == "DISABLED":
                message = "登录成功并已保存新会话，但 Muse 返回该会话的工作区不可用，请核对官网状态。"
            self._end(flow, "authenticated", message)

    def _cookies(self, flow: _Flow) -> tuple[dict, dict]:
        cookies, expires = {}, {}
        now = self._clock()
        for cookie in flow.session.cookies:
            if (cookie.domain or "").lstrip(".") != "muse.ai":
                continue
            if cookie.expires is not None and cookie.expires <= now:
                continue
            if cookie.value:
                cookies[cookie.name] = cookie.value
                if cookie.expires is not None:
                    expires[cookie.name] = int(cookie.expires)
        return cookies, expires

    def resend(self, login_id: str) -> dict:
        flow = self._get(login_id)
        if flow.state not in ACTIVE_STATES:
            return self._public(flow)

        def send_again():
            if flow.state != "awaiting_code":
                raise NativeLoginError(409, "当前登录步骤不能重新发送验证码。")
            self._send_attempt(flow)
            response = self._request(flow, "POST", AUTH_BASE + "resend-otp", {})
            self._check(response)
            self._await_code(flow, self._json(response))

        return self._perform(flow, send_again)

    def status(self, login_id: str) -> dict:
        return self._public(self._get(login_id))

    def current(self) -> dict:
        """Recover an in-progress login after a page refresh, without local storage."""
        with self._lock:
            if self._active_id:
                flow = self._get(self._active_id)
                if flow.state in ACTIVE_STATES:
                    return self._public(flow)
            return {"state": "idle"}

    def cancel(self, login_id: str) -> dict:
        flow = self._get(login_id)
        with self._lock:
            if flow.state in ACTIVE_STATES:
                self._end(flow, "expired", "本次登录已取消。")
        return self._public(flow)

    def shutdown(self):
        with self._lock:
            self._closed = True
            for flow in self._flows.values():
                if flow.state in ACTIVE_STATES:
                    self._end(flow, "expired", "登录服务已关闭，请重新开始。")
                else:
                    self._destroy(flow)
            self._flows.clear()
            self._active_id = None
