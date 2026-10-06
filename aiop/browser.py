"""The operator's only way to touch a web app: five tools and a compact snapshot.

Tools: goto, click, fill, select, read. There is deliberately no "run JavaScript"
tool and no download tool. JavaScript is used internally (to tag elements and to
resolve a ref to the real element), never on the LLM's behalf.

Every snapshot re-tags the interactive elements with fresh `data-aiop-ref`
values (e1, e2, ...), so a ref is only valid against the latest snapshot.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser as PWBrowser
from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, Playwright, Response, sync_playwright

from aiop.models import Observation, Target, ToolResult

MAX_SNAPSHOT_CHARS = 8000


class BrowserError(Exception):
    """Base class; the recovery layer classifies these."""


class MissingElement(BrowserError):
    pass


class DomainNotAllowed(BrowserError):
    pass


class ActionFailed(BrowserError):
    pass


# Tags interactive elements and returns the page as compact lines, in document order.
_SNAPSHOT_JS = r"""
() => {
  const INTERACTIVE = 'a[href], button, input:not([type=hidden]), select, textarea';
  document.querySelectorAll('[data-aiop-ref]').forEach(el => el.removeAttribute('data-aiop-ref'));
  let n = 0;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, k) => s.length > k ? s.slice(0, k) + '…' : s;
  const tag = el => { const r = 'e' + (++n); el.setAttribute('data-aiop-ref', r); return r; };
  const labelOf = el => clean(el.getAttribute('aria-label') ||
      (el.labels && el.labels[0] ? Array.from(el.labels[0].childNodes)
          .filter(c => c.nodeType === 3).map(c => c.textContent).join(' ') : '') ||
      el.getAttribute('placeholder') || el.name || el.id);
  const eff = el => el.dataset.aiopEffect ? ` effect=${el.dataset.aiopEffect}` : '';
  const describe = el => {
    const r = tag(el), t = el.tagName;
    if (t === 'A') return `[${r}] link "${cut(clean(el.innerText), 80)}" -> ${el.getAttribute('href')}${eff(el)}`;
    if (t === 'BUTTON' || (t === 'INPUT' && ['submit', 'button'].includes(el.type))) {
      const kind = (t === 'BUTTON' ? (el.getAttribute('type') || 'submit') : el.type);
      return `[${r}] button "${clean(el.innerText || el.value)}"${kind === 'submit' ? ' (submit)' : ''}${eff(el)}`;
    }
    if (t === 'SELECT') {
      const opts = Array.from(el.options).map(o => clean(o.text) + (o.dataset.aiopEffect ? ` {effect=${o.dataset.aiopEffect}}` : ''));
      const cur = el.selectedOptions[0] ? clean(el.selectedOptions[0].text) : '';
      return `[${r}] select "${labelOf(el)}" value="${cur}" options: ${opts.join(' | ')}${eff(el)}`;
    }
    if (el.type === 'checkbox' || el.type === 'radio')
      return `[${r}] ${el.type} "${labelOf(el)}" checked=${el.checked}${eff(el)}`;
    return `[${r}] textbox "${labelOf(el)}" value="${cut(el.value || '', 120)}"${eff(el)}`;
  };
  const lines = [], done = new Set();
  for (const el of document.body.querySelectorAll('*')) {
    if (done.has(el) || !visible(el)) continue;
    if (el.matches(INTERACTIVE)) { lines.push(describe(el)); continue; }
    const t = el.tagName;
    if (/^H[1-6]$/.test(t)) { lines.push('#'.repeat(+t[1]) + ' ' + clean(el.innerText)); continue; }
    if (t === 'TR') {
      const cells = Array.from(el.children).map(c => {
        const links = Array.from(c.querySelectorAll(INTERACTIVE));
        links.forEach(l => done.add(l));
        return links.map(l => `[${tag(l)}] `).join('') + cut(clean(c.innerText), 200);
      });
      lines.push('| ' + cells.join(' | ') + ' |');
      continue;
    }
    if (['P', 'LI', 'DIV', 'SPAN', 'TD', 'DD', 'DT'].includes(t)) {
      // only the element's own text; interactive children are emitted on their own
      const own = clean(Array.from(el.childNodes).filter(c => c.nodeType === 3 ||
          (c.nodeType === 1 && ['B', 'STRONG', 'EM', 'I', 'CODE'].includes(c.tagName)))
          .map(c => c.textContent).join(' '));
      if (own) lines.push(cut(own, 400));
    }
  }
  const err = document.querySelector('[role=alert], #error, .error');
  return { title: document.title, lines, error: err ? clean(err.innerText) : null };
}
"""

# Resolves a ref to what it really is: the policy engine judges this, not the LLM's words.
_RESOLVE_JS = r"""
(ref) => {
  const el = document.querySelector(`[data-aiop-ref="${ref}"]`);
  if (!el) return null;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const t = el.tagName;
  const isSubmit = (t === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit') ||
                   (t === 'INPUT' && el.type === 'submit');
  const out = { ref, tag: t.toLowerCase(), label: clean(el.innerText || el.value || el.getAttribute('aria-label') || el.name),
                effect: el.dataset.aiopEffect || null, page_url: location.href,
                href: t === 'A' ? el.href : null, is_submit: isSubmit,
                form_action: null, form_method: null, form_fields: {}, form_effects: [] };
  const form = el.form || null;
  if (form && isSubmit) {
    out.form_action = form.action;
    out.form_method = (form.getAttribute('method') || 'get').toLowerCase();
    for (const f of form.elements) {
      if (!f.name) continue;
      if (f.tagName === 'SELECT') {
        const o = f.selectedOptions[0];
        out.form_fields[f.name] = o ? clean(o.text) : '';
        if (o && o.dataset.aiopEffect) out.form_effects.push(o.dataset.aiopEffect);
      } else if (f.type === 'checkbox' || f.type === 'radio') {
        if (f.checked) out.form_fields[f.name] = f.value;
      } else if (!['submit', 'button'].includes(f.type)) {
        out.form_fields[f.name] = f.value;
      }
    }
  }
  return out;
}
"""


class Browser:
    """A Playwright page restricted to the company's allowed origins."""

    def __init__(self, allowed_origins: list[str], headless: bool = True, timeout_ms: int = 8000):
        if not allowed_origins:
            raise ValueError("at least one allowed origin is required")
        self.allowed = [o.rstrip("/") for o in allowed_origins]
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw: Playwright | None = None
        self._browser: PWBrowser | None = None
        self._page: Page | None = None
        self._status: int | None = None

    # ---------- lifecycle ----------
    def start(self) -> Browser:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(accept_downloads=False)
        self._page = ctx.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        self._page.on("response", self._on_response)
        return self

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._pw = self._browser = self._page = None

    def __enter__(self) -> Browser:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("browser not started")
        return self._page

    def _on_response(self, resp: Response) -> None:
        if resp.request.is_navigation_request() and resp.frame == self.page.main_frame:
            self._status = resp.status

    # ---------- domain allow-list ----------
    def absolute(self, url: str) -> str:
        return urljoin(self.allowed[0] + "/", url) if not urlparse(url).scheme else url

    def is_allowed(self, url: str) -> bool:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" in self.allowed

    # ---------- the five tools ----------
    def goto(self, url: str) -> ToolResult:
        url = self.absolute(url)
        if not self.is_allowed(url):
            raise DomainNotAllowed(f"{url} is outside the allowed origins {self.allowed}")
        return self._act(lambda: self.page.goto(url, wait_until="load"))

    def click(self, ref: str) -> ToolResult:
        loc = self._locate(ref)
        return self._act(lambda: (loc.click(), self.page.wait_for_load_state("load")))

    def fill(self, ref: str, value: str) -> ToolResult:
        loc = self._locate(ref)
        return self._act(lambda: loc.fill(value))

    def select(self, ref: str, value: str) -> ToolResult:
        loc = self._locate(ref)
        return self._act(lambda: loc.select_option(label=value))

    def read(self) -> ToolResult:
        return ToolResult(ok=True, observation=self.snapshot())

    # ---------- helpers for the runtime ----------
    def resolve(self, ref: str) -> Target:
        """What the ref really points at, read from the live page."""
        info = self.page.evaluate(_RESOLVE_JS, ref)
        if info is None:
            raise MissingElement(f"no element with ref {ref} on {self.page.url}")
        return Target(**info)

    def refill(self, target: Target) -> str:
        """Re-open the form behind `target`, restore its field values by name, and return
        the fresh ref of its submit control. Used by recovery retries and approval resumes,
        so a retry never depends on refs from a page that no longer exists."""
        if not (target.is_submit and target.form_action):
            raise ActionFailed("refill needs a submit target with a form")
        self.goto(target.page_url)
        idx = self.page.evaluate(
            "(a) => Array.from(document.forms).findIndex(f => f.action === a)", target.form_action)
        if idx < 0:
            raise MissingElement(f"form posting to {target.form_action} not found on {target.page_url}")
        form = self.page.locator("form").nth(idx)
        for name, value in target.form_fields.items():
            field = form.locator(f'[name="{name}"]').first
            if field.evaluate("e => e.tagName") == "SELECT":
                field.select_option(label=value)
            else:
                field.fill(value)
        self.snapshot()  # re-tag
        submit = form.locator("button[type=submit], button:not([type]), input[type=submit]").filter(
            has_text=target.label) if target.tag == "button" else form.locator("input[type=submit]")
        if submit.count() == 0:
            raise MissingElement(f"submit control '{target.label}' not found in the form")
        ref = submit.first.get_attribute("data-aiop-ref")
        assert ref is not None
        return ref

    def snapshot(self) -> Observation:
        data = self.page.evaluate(_SNAPSHOT_JS)
        head = f"URL: {self.page.url}\nTitle: {data['title']}\nHTTP: {self._status}"
        if data["error"]:
            head += f"\nPAGE ERROR: {data['error']}"
        body = "\n".join(data["lines"])
        if len(body) > MAX_SNAPSHOT_CHARS:
            body = body[:MAX_SNAPSHOT_CHARS] + "\n… (snapshot truncated)"
        return Observation(url=self.page.url, title=data["title"], http_status=self._status,
                           error_text=data["error"], snapshot=f"{head}\n---\n{body}")

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)

    def _locate(self, ref: str):
        loc = self.page.locator(f'[data-aiop-ref="{ref}"]')
        if loc.count() == 0:
            raise MissingElement(f"no element with ref {ref} on {self.page.url}")
        return loc.first

    def _act(self, fn) -> ToolResult:
        try:
            fn()
        except PWError as e:
            # a timeout or detached element: report it, with whatever the page now shows
            return ToolResult(ok=False, observation=self.snapshot(), error=str(e).splitlines()[0])
        obs = self.snapshot()
        if not self.is_allowed(obs.url):
            raise DomainNotAllowed(f"navigation left the allowed origins: {obs.url}")
        return ToolResult(ok=True, observation=obs)
