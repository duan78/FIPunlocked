// FIP onglets — pont Chrome <-> daemon fipd (127.0.0.1:8199)
// Pousse la liste des onglets (POST /app/fenetres) et releve les commandes
// (GET /app/fenetres) : activer / fermer / recharger / epingler / fermer les autres.
const DAEMON = 'http://127.0.0.1:8199/app/fenetres';
let cursor = 0;

async function push() {
  try {
    const tabs = await chrome.tabs.query({});
    await fetch(DAEMON, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tabs: tabs.map(t => ({
          id: t.id, windowId: t.windowId, active: t.active,
          title: t.title || '', url: t.url || '', pinned: !!t.pinned
        }))
      })
    });
  } catch (e) { /* daemon absent : silencieux */ }
}

async function poll() {
  try {
    const r = await fetch(DAEMON + '?cursor=' + cursor);
    const j = await r.json();
    if (j && j.cmd && j.cursor !== cursor) {
      cursor = j.cursor;
      const c = j.cmd;
      try {
        if (c.action === 'activate') {
          chrome.tabs.update(c.tabId, { active: true });
          if (c.windowId) chrome.windows.update(c.windowId, { focused: true });
        } else if (c.action === 'close') {
          chrome.tabs.remove(c.tabId);
        } else if (c.action === 'reload') {
          chrome.tabs.reload(c.tabId);
        } else if (c.action === 'pin') {
          chrome.tabs.get(c.tabId, t => chrome.tabs.update(c.tabId, { pinned: !t.pinned }));
        } else if (c.action === 'close_others') {
          chrome.tabs.query({}, tabs => {
            const others = tabs.filter(t => t.windowId === c.windowId &&
                                            t.id !== c.tabId && !t.pinned);
            chrome.tabs.remove(others.map(t => t.id));
          });
        }
      } catch (e) { /* onglet deja disparu */ }
      push();
    }
  } catch (e) { /* daemon absent */ }
}

chrome.tabs.onCreated.addListener(push);
chrome.tabs.onUpdated.addListener(push);
chrome.tabs.onRemoved.addListener(push);
chrome.tabs.onActivated.addListener(push);
chrome.tabs.onMoved.addListener(push);
chrome.tabs.onAttached.addListener(push);
chrome.tabs.onDetached.addListener(push);
chrome.windows.onFocusChanged.addListener(push);
chrome.windows.onCreated.addListener(push);
chrome.windows.onRemoved.addListener(push);

push();
setInterval(push, 4000);
setInterval(poll, 1000);
