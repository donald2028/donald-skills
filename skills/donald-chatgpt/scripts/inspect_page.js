(() => {
  const root = document.body;
  const visible = el => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const controls = [...root.querySelectorAll(
    'button,a[href],input,textarea,[role=textbox],[role=menuitem],[role=radio]'
  )].filter(el => (visible(el) || el.type === 'file') && !el.closest('nav,aside:not([role=alert]),[role=navigation]'));
  globalThis.__donaldChatgptRefs = controls;
  return {
    url: location.href,
    title: document.title,
    controls: controls.map((el, index) => ({
      ref: '@e' + (index + 1), tag: el.tagName.toLowerCase(), id: el.id,
      role: el.getAttribute('role'), label: el.getAttribute('aria-label'),
      text: el.innerText?.trim() || '', type: el.getAttribute('type'),
      disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
      pressed: el.getAttribute('aria-pressed'), checked: el.getAttribute('aria-checked'),
      accept: el.getAttribute('accept'), href: el.getAttribute('href'),
    })),
  };
})()
