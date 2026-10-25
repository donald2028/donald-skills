(() => {
  const visible = el => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const units = [...document.querySelectorAll('[data-message-author-role],[data-chatgpt-search-unit-key]')];
  const containers = (units.length ? units : [...document.querySelectorAll(
    '[data-testid^="conversation-turn-"], [data-turn-key]'
  )]).filter(el => visible(el) && !el.closest('nav,aside,[data-sidebar-item]'));
  const messages = [];
  let lastUserElement = null;
  for (const el of containers) {
    // Prefer the complete turn wrapper when both wrapper and role node exist.
    if (containers.some(parent => parent !== el && parent.contains(el))) continue;
    const roleNode = el.matches('[data-message-author-role]') ? el
      : el.querySelector('[data-message-author-role]');
    const heading = [...el.querySelectorAll('h4,h5,h6,[role="heading"]')]
      .map(node => node.textContent.trim()).join(' ');
    const unitRole = el.getAttribute('data-chatgpt-search-unit-key')?.match(/:(user|assistant)$/)?.[1];
    const role = roleNode?.getAttribute('data-message-author-role') || unitRole
      || (el.querySelector('[data-testid="user-message"],.user-message-bubble')
        || /^(You said|你说|您说)/i.test(heading) ? 'user'
        : /^(ChatGPT said|ChatGPT 说|ChatGPT说|Assistant)/i.test(heading) ? 'assistant' : '');
    if (!['user', 'assistant'].includes(role)) continue;
    if (role === 'user') lastUserElement = el;
    const body = role === 'user'
      ? el.querySelector('[data-testid="user-message"],[data-user-message-bubble],.user-message-bubble,.whitespace-pre-wrap') || roleNode || el
      : el.querySelector('[data-markdown-text-style="assistant-message"],.markdown,[data-message-content]') || roleNode || el;
    const clone = body.cloneNode(true);
    clone.querySelectorAll('button,svg,[aria-hidden="true"],h4.sr-only,h5.sr-only,h6.sr-only')
      .forEach(node => node.remove());
    messages.push({
      id: el.getAttribute('data-message-id') || roleNode?.getAttribute('data-message-id')
        || el.getAttribute('data-chatgpt-search-message-ids')?.split(' ')[0]
        || el.getAttribute('data-turn-key') || el.getAttribute('data-testid') || el.id || '',
      role,
      turn_state: el.closest('[data-talvt-turn-state]')?.getAttribute('data-talvt-turn-state') || '',
      text: body.innerText?.trim() || '',
      html: clone.innerHTML,
      code: [...body.querySelectorAll('pre')].map(node => node.innerText),
      links: [...body.querySelectorAll('a[href]')].map(a => ({text: a.innerText, href: a.href})),
    });
  }
  const composer = [...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"],textarea')]
    .find(visible);
  const buttons = [...document.querySelectorAll('button')].filter(visible);
  const label = el => [el.getAttribute('aria-label'), el.getAttribute('data-testid'), el.innerText]
    .filter(Boolean).join(' ');
  const challenge = [...document.querySelectorAll('body *')].find(el =>
    !el.childElementCount && visible(el)
    && !el.closest('nav,aside:not([role=alert]),[data-chatgpt-search-unit-key],[data-message-author-role]')
    && /cloudflare_challenge|verify you are human|checking your browser|验证您是人类|验证你是人类/i.test(el.innerText || '')
  );
  const challengeFrame = [...document.querySelectorAll('iframe')].find(el => visible(el)
    && /challenges\.cloudflare\.com|challenge|turnstile/i.test(el.src + ' ' + el.title));
  const serviceAlert = [...document.querySelectorAll('[role=alert]')].find(el => visible(el)
    && /cloudflare_challenge|something went wrong|network error|failed to (?:fetch|load)|try again later|出了点问题|网络错误/i.test(el.innerText));
  const retry = serviceAlert && [...serviceAlert.querySelectorAll('button')].find(el => visible(el)
    && !el.disabled && /^(retry|try again|重试)$/i.test((el.innerText || el.getAttribute('aria-label') || '').trim()));
  const lastUser = [...messages].reverse().find(message => message.role === 'user');
  const retryForUser = retry && lastUserElement
    && !!(lastUserElement.compareDocumentPosition(serviceAlert) & Node.DOCUMENT_POSITION_FOLLOWING)
    ? lastUser?.id : '';
  globalThis.__donaldChatgptRetry = retryForUser ? {element: retry, userId: retryForUser} : null;
  let gate = null;
  if (challengeFrame) {
    gate = {kind: 'verification', error_type: 'verification_pending',
            message: challengeFrame.title || 'Browser verification'};
  } else if (serviceAlert) {
    gate = {kind: 'service_error', error_type: 'transient_service_error',
            message: serviceAlert.innerText.trim(), retry_for_user_id: retryForUser || ''};
  } else if (challenge) {
    gate = {kind: 'verification', error_type: 'verification_pending', message: challenge.innerText.trim()};
  }
  return {
    url: location.href,
    title: document.title,
    ready_state: document.readyState,
    has_composer: !!composer,
    composer_text: composer?.matches('textarea,input') ? composer.value : composer?.innerText || '',
    generating: buttons.some(el => /stop-(?:button|generating)|stop generating|stop streaming|停止生成|停止回答/i.test(label(el))),
    continuation_required: buttons.some(el => /continue generating|继续生成/i.test(label(el))),
    gate,
    messages,
  };
})()
