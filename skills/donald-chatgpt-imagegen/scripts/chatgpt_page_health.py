"""Shared ChatGPT page detection and bounded page-recovery policy for text and image jobs."""
from __future__ import annotations

import json
from typing import Any

DEFAULT_PAGE_RECOVERY_ATTEMPTS = 3


class RecoverablePageError(RuntimeError):
    pass


def page_failure(page: dict[str, Any], failures: int = 0, phase: str = '') -> str | None:
    if page.get('humanReason') or (page.get('gate') or {}).get('kind') == 'verification':
        return None
    reason = page.get('crashReason') or ''
    if reason == 'unexpected_blank_page' and phase == 'browser_startup':
        reason = ''
    return str(reason) if reason else ('page_unreadable' if failures >= 3 else None)


def page_recovery_action(attempt: int, *, submitted: bool = False, preserve_target: bool = False) -> str:
    if preserve_target:
        return 'preserve'
    return 'retry' if attempt == 1 and not submitted else 'reopen'


CONVERSATION_DOM_JS = r"""
(() => {
  const legacyUsers = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
  const legacyAssistants = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
  const merge = (legacy, current) => [...legacy, ...current.filter(
    (el, index) => current.indexOf(el) === index
      && !current.some((node) => node !== el && node.contains(el))
      && !legacy.some((node) => node === el || node.contains(el) || el.contains(node))
  )].sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
  const users = merge(legacyUsers, Array.from(document.querySelectorAll(
    '[data-user-message-bubble],[data-chatgpt-search-unit-key$=":user"],[data-testid="user-message"],.user-message-bubble'
  )));
  const assistants = merge(legacyAssistants, [
    ...Array.from(document.querySelectorAll('[data-conversation-role="assistant"]'))
      .map((el) => el.parentElement).filter(Boolean),
    ...document.querySelectorAll('[data-chatgpt-search-unit-key$=":assistant"]'),
  ]);
  const turnSelector = 'section[data-testid^="conversation-turn-"],[data-turn-key]';
  const allTurns = Array.from(document.querySelectorAll(turnSelector));
  const turns = allTurns.filter((el) => !allTurns.some((other) => other !== el && other.contains(el)));
  const turnFor = (el) => turns.find((turn) => turn.contains(el))
    || (turns.length ? null : el?.closest('article,section')) || null;
  const userFor = (el) => el ? users.filter((user) => user.contains(el) || el.contains(user)
    || (user.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)).at(-1) || null : null;
  const submittedUserFor = (prompt) => {
    const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const expected = normalize(prompt);
    const matches = expected ? users.filter((user) => [user.innerText, user.textContent]
      .some((text) => normalize(text).includes(expected))) : [];
    const messages = new Map();
    for (const user of matches) {
      const turn = turnFor(user);
      messages.set(turn || user, turn ? users.filter((node) => turnFor(node) === turn).at(-1) : user);
    }
    return messages.size === 1 ? messages.values().next().value : null;
  };
  return {users, assistants, turns, turnFor, userFor, submittedUserFor};
})()
""".strip()


def original_turn_script(prompt: str, baseline: int = 0, turn_key: str = '') -> str:
    """Identify one post-dispatch user turn; URLs alone cannot identify local conversations."""
    return r"""
JSON.stringify((() => {
  const {users, turnFor} = __CONVERSATION_DOM__;
  const normalize = text => (text || '').replace(/\s+/g, ' ').trim();
  const expected = normalize(__PROMPT__);
  const currentUsers = users.filter(user => !user.closest('nav,aside,[role=navigation],[data-sidebar-item],#history'));
  const matches = currentUsers.filter(user => expected && [user.innerText, user.textContent]
    .some(text => normalize(text).includes(expected)));
  const user = matches.length === 1 ? matches[0] : null;
  const root = user && (turnFor(user) || user);
  const key = root?.getAttribute('data-turn-key') || user?.getAttribute('data-message-id') || '';
  return {verified: Boolean(user && user === currentUsers.at(-1)
    && (__TURN_KEY__ ? key === __TURN_KEY__ : currentUsers.length > __BASELINE__)), turn_key: key, user_count: currentUsers.length,
    url: location.href};
})())
""".replace('__CONVERSATION_DOM__', CONVERSATION_DOM_JS).replace('__PROMPT__', json.dumps(prompt)).replace(
        '__BASELINE__', str(int(baseline))).replace('__TURN_KEY__', json.dumps(turn_key))


def original_history_script(prompt: str, *, click: bool = False) -> str:
    """Select one exact-prompt history control, including role=button entries without href."""
    return r"""
JSON.stringify((() => {
  const access = JSON.parse(__PAGE_ACCESS__);
  if (access.humanReason) return {found: false, human_required: true};
  const normalize = text => (text || '').replace(/\s+/g, ' ').trim();
  const expected = normalize(__PROMPT__);
  const scope = 'nav,aside,[role=navigation],[data-sidebar-item],#history,[aria-label="Chat history"]';
  const matches = [...document.querySelectorAll('a,button,[role=button]')].filter(el =>
    el.closest(scope) && el.getClientRects().length && !el.matches('[disabled],[aria-disabled=true]')
    && expected && [el.getAttribute('aria-label'), el.innerText, el.textContent]
      .some(text => normalize(text).includes(expected)));
  const unique = matches.filter(el => !matches.some(other => other !== el && el.contains(other)));
  if (unique.length !== 1) return {found: false, matches: unique.length};
  if (__CLICK__) unique[0].click();
  return {found: true, clicked: __CLICK__, matches: 1};
})())
""".replace('__PAGE_ACCESS__', PAGE_ACCESS_STATE_JS.replace('__EXPECTED_PROMPT__', json.dumps(prompt))).replace(
        '__PROMPT__', json.dumps(prompt)).replace('__CLICK__', 'true' if click else 'false')


HUMAN_VERIFICATION_STATE_JS = r"""
(() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const frames = Array.from(document.querySelectorAll('iframe'))
    .filter((frame) => /captcha|challenge|turnstile|verify|cloudflare/i.test(`${frame.src} ${frame.title}`));
  const visibleFrames = frames.filter(visible);
  const excluded = "nav,aside:not([role='alert']),[role='navigation'],[inert],#history,[aria-label='Chat history']," +
    "[data-sidebar-item],[data-message-author-role],[data-chatgpt-search-unit-key],[data-turn-key],[data-testid^='conversation-turn']," +
    "[data-user-message-bubble],[data-conversation-role],form,#prompt-textarea,[contenteditable='true']";
  const markers = [
    'verify you are human', 'verifying you are human', 'confirm you are human',
    'checking your browser', 'performing security verification',
    'complete the security check', 'complete the challenge', 'cloudflare ray id',
    '验证码', '安全验证', '人机验证', '验证您是真人', '验证您是人类', '验证你是人类'
  ];
  const texts = Array.from(document.querySelectorAll('body *')).filter(visible)
    .filter((el) => !el.closest(excluded) && !el.querySelector(excluded))
    .map((el) => (el.innerText || '').replace(/\s+/g, ' ').toLowerCase());
  return {
    challengeFrame: visibleFrames.length > 0,
    challengeFrameCount: frames.length,
    visibleChallengeFrameCount: visibleFrames.length,
    challengeTextMarker: markers.find((marker) => texts.some((text) => text.includes(marker))) || '',
  };
})()
""".strip()


CURRENT_RESPONSE_ERROR_STATE_JS = r"""
((expectedPrompt) => {
  const {users, turnFor, userFor, submittedUserFor} = __CONVERSATION_DOM__;
  const currentUser = expectedPrompt ? submittedUserFor(expectedPrompt) : users.at(-1);
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const excluded = "nav,aside:not([role='alert']),[role='navigation'],[inert],#history,[aria-label='Chat history']," +
    "[data-sidebar-item],[data-message-author-role='user'],[data-user-message-bubble]," +
    "[data-chatgpt-search-unit-key$=':user'],[data-testid='user-message'],.user-message-bubble," +
    "form,#prompt-textarea,[contenteditable='true']";
  const belongsToCurrentResponse = (el) => {
    if (!currentUser || el.closest(excluded) || el.contains(currentUser) || userFor(el) !== currentUser) return false;
    const owner = turnFor(el);
    if (owner) return userFor(owner) === currentUser;
    const surface = el.closest('[role="alert"],[aria-live="assertive"],[data-testid*="error" i]') || el.parentElement;
    const text = surface?.innerText || '';
    return text.length <= 800 && /cloudflare_challenge/i.test(text);
  };
  const labelParts = (el) => [el.getAttribute('aria-label'), el.getAttribute('title'), el.innerText, el.textContent]
    .filter(Boolean).map((value) => value.trim()).filter(Boolean);
  const retryLabels = ["Try again", "Retry", "重新生成", "再试一次", "重试"];
  const retryControls = Array.from(document.querySelectorAll('button,[role="button"],a'))
    .filter(visible).filter(belongsToCurrentResponse)
    .filter((el) => labelParts(el).some((label) => retryLabels.some((retry) => label.toLowerCase() === retry.toLowerCase())));
  const errorSurfaces = Array.from(document.querySelectorAll(
    '[role="alert"],[aria-live="assertive"],[data-testid*="error" i],[class*="toast" i]'
  )).filter(visible).filter(belongsToCurrentResponse);
  // Some response errors sit after the user turn without an assistant wrapper or alert role.
  const retryErrors = retryControls.map((el) => el.parentElement).filter(Boolean)
    .filter((el) => visible(el) && belongsToCurrentResponse(el)
      && (el.innerText || '').length <= 800 && /cloudflare_challenge/i.test(el.innerText || ''));
  const errorSurfaceTexts = [...errorSurfaces, ...retryErrors]
    .map((el) => (el.innerText || el.textContent || '').trim()).filter(Boolean)
    .filter((value, index, values) => values.indexOf(value) === index).slice(0, 8);
  return {retryControls, retryControlLabels: retryControls.map((el) => labelParts(el)[0]), errorSurfaceTexts};
})
""".strip().replace("__CONVERSATION_DOM__", CONVERSATION_DOM_JS)


PAGE_ACCESS_STATE_JS = r"""
JSON.stringify((() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const hasPrompt = Array.from(document.querySelectorAll("[contenteditable='true'],#prompt-textarea,textarea")).some(visible);
  const rawText = document.body?.innerText || "";
  const text = rawText.toLowerCase();
  const loginPath = /\/(auth\/)?(login|signin)(\/|$)/i.test(location.pathname);
  const loginControl = Array.from(document.querySelectorAll("a,button"))
    .filter(visible)
    .some((el) => /^(log in|sign in|continue with|登录|登入)/i.test((el.innerText || "").trim()));
  const {challengeFrame, challengeFrameCount, visibleChallengeFrameCount, challengeTextMarker} = __HUMAN_VERIFICATION_STATE__;
  const challengeText = Boolean(challengeTextMarker);
  const {retryControlLabels, errorSurfaceTexts} = (__CURRENT_RESPONSE_ERROR_STATE__)(__EXPECTED_PROMPT__);
  const responseErrorText = errorSurfaceTexts.find((value) => /cloudflare_challenge/i.test(value)) || '';
  const responseError = responseErrorText ? {
    error_type: 'chatgpt_response_service_error', matched_marker: 'cloudflare_challenge',
    message: responseErrorText.slice(-800), retry_available: retryControlLabels.length > 0,
  } : null;
  let humanReason = "";
  if (challengeFrame || challengeText) humanReason = "anti_automation_verification";
  else if (loginPath || loginControl) humanReason = "login_required";
  const transientPageErrorPattern = /failed to load subscription|something went wrong|network error|failed to load|unable to load|could not load (?:this |the )?(?:chatgpt )?conversation|try again later|we couldn.t load your account|browser check couldn.t finish/i;
  const errorExcluded = "nav,aside:not([role='alert']),[role='navigation'],[inert],#history,[aria-label='Chat history']," +
    "[data-sidebar-item],[data-sidebar-chatgpt-conversation-key],[data-message-author-role],[data-chatgpt-search-unit-key]," +
    "[data-turn-key],[data-testid^='conversation-turn'],#prompt-textarea,form,textarea,[contenteditable='true']";
  const transientPageErrorTexts = Array.from(document.querySelectorAll("body *"))
    .filter(visible)
    .filter((el) => !el.closest(errorExcluded) && !el.querySelector(errorExcluded))
    .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
    .filter((value) => value.length >= 8 && value.length <= 800 && transientPageErrorPattern.test(value))
    .sort((a, b) => a.length - b.length);
  const transientPageErrorText = transientPageErrorTexts[0] || "";
  const crashMarkers = [
    "aw, snap!", "this page isn't working", "this page isn’t working",
    "this site can't be reached", "this site can’t be reached", "page crashed",
    "out of memory", "err_connection_", "err_internet_", "err_timed_out",
    "err_failed", "err_http2_", "err_network_"
  ];
  const matchedCrashMarker = crashMarkers.find((marker) => text.includes(marker)) || "";
  let crashReason = "";
  if (location.href.startsWith("chrome-error://")) crashReason = "chrome_error_page";
  else if (matchedCrashMarker) crashReason = matchedCrashMarker;
  else if (transientPageErrorText) crashReason = "chatgpt_transient_page_error";
  else if (document.readyState === "complete" && location.href === "about:blank"
           && !hasPrompt && !rawText.trim()) crashReason = "unexpected_blank_page";
  return {
    hasPrompt,
    humanReason,
    responseError,
    humanEvidence: {
      challengeFrameCount,
      visibleChallengeFrameCount,
      challengeTextMarker, loginPath, loginControl,
    },
    crashReason,
    transientPageErrorText,
    href: location.href,
    title: document.title,
    readyState: document.readyState,
  };
})())
""".strip().replace("__HUMAN_VERIFICATION_STATE__", HUMAN_VERIFICATION_STATE_JS).replace(
    "__CURRENT_RESPONSE_ERROR_STATE__", CURRENT_RESPONSE_ERROR_STATE_JS
)


PAGE_ERROR_RETRY_JS = r"""
JSON.stringify((() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  };
  const excluded = "nav,aside:not([role='alert']),[role='navigation'],[inert],#history,[aria-label='Chat history']," +
    "[data-sidebar-item],[data-sidebar-chatgpt-conversation-key]," +
    "[data-message-author-role],[data-chatgpt-search-unit-key],[data-turn-key],[data-testid^='conversation-turn'],form";
  const error = /failed to load|could not load (?:this |the )?(?:chatgpt )?conversation|something went wrong|network error|unable to load|try again later|we couldn.t load your account|browser check couldn.t finish|加载失败|网络错误/i;
  for (const el of document.querySelectorAll("button,[role='button']")) {
    if (!visible(el) || el.closest(excluded) || el.matches("[disabled],[aria-disabled='true']")) continue;
    const label = (el.getAttribute("aria-label") || el.innerText || "").trim();
    if (!/^(try again|retry|reload|再试一次|重试|重新加载)$/i.test(label)) continue;
    let container = el.parentElement;
    for (let depth = 0; container && container !== document.body && depth < 4; depth++, container = container.parentElement) {
      const text = (container.innerText || "").trim();
      if (text.length <= 800 && error.test(text) && !container.querySelector(excluded)) {
        const r = el.getBoundingClientRect();
        globalThis.__donaldPageErrorRetry = {element: el, url: location.href};
        return {found: true, label, x: r.left + r.width / 2, y: r.top + r.height / 2};
      }
    }
  }
  return {found: false};
})())
""".strip()


CLICK_PAGE_ERROR_RETRY_JS = r"""
JSON.stringify((() => {
  const control = JSON.parse(__PAGE_ERROR_RETRY__);
  const retry = globalThis.__donaldPageErrorRetry;
  if (!control.found || !retry?.element?.isConnected || retry.url !== location.href) return {clicked: false};
  retry.element.click();
  return {clicked: true};
})())
""".strip().replace('__PAGE_ERROR_RETRY__', PAGE_ERROR_RETRY_JS)
