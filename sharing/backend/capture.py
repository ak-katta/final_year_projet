"""
Browser side of things. Boots a Playwright page, scrapes it into a
PageSnapshot the LLM agents can plan against, and saves screenshots as
the test run goes along.
"""

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import sync_playwright

from state import (
    QAState, PageSnapshot, InteractiveElement, FormInfo, FormField,
    LinkInfo, ImageInfo, Screenshot,
)


# Pulled out of the page in one round trip instead of a bunch of small
# page.query_selector calls, which would be painfully slow.
SCRAPE_JS = """
() => {
  function isUnique(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }

  function selectorFor(el) {
    const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\"');
    const tag = el.tagName.toLowerCase();

    if (el.id) {
      const s = '#' + esc(el.id);
      if (isUnique(s)) return s;
    }
    const testId = el.getAttribute('data-testid');
    if (testId) {
      const s = `[data-testid="${testId}"]`;
      if (isUnique(s)) return s;
    }
    if (el.name) {
      const s = `${tag}[name="${el.name}"]`;
      if (isUnique(s)) return s;
    }
    // search boxes and other custom widgets frequently skip id/name but
    // keep accessibility/placeholder attributes — try those next, since
    // they're what a real search input is most likely to have
    const aria = el.getAttribute('aria-label');
    if (aria) {
      const s = `${tag}[aria-label="${aria}"]`;
      if (isUnique(s)) return s;
    }
    const role = el.getAttribute('role');
    if (role) {
      const s = `${tag}[role="${role}"]`;
      if (isUnique(s)) return s;
    }
    if (el.type) {
      const s = `${tag}[type="${el.type}"]`;
      if (isUnique(s)) return s;
    }
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) {
      const s = `${tag}[placeholder="${placeholder}"]`;
      if (isUnique(s)) return s;
    }

    // last resort: walk up the tree building an nth-of-type path, anchoring
    // on the first ancestor that has a unique id if we hit one
    let path = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && isUnique('#' + esc(node.id))) {
        path.unshift('#' + esc(node.id));
        break;
      }
      let piece = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(s => s.tagName === node.tagName);
        if (sibs.length > 1) piece += `:nth-of-type(${sibs.indexOf(node) + 1})`;
      }
      path.unshift(piece);
      if (isUnique(path.join(' > '))) return path.join(' > ');
      node = parent;
    }
    // an ambiguous selector is worse than no selector: it silently drives
    // every click into whichever matching element happens to come first in
    // the DOM, which is usually a hidden one, and the step times out
    const full = path.join(' > ');
    return isUnique(full) ? full : '';
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  // Visible elements get first claim on the 150-element budget: a test can
  // only ever drive something the user can actually see, and on big sites
  // (collapsed nav drawers, duplicated mobile menus) hidden nodes would
  // otherwise crowd the real controls out of the list entirely.
  const rawEls = Array.from(document.querySelectorAll(
    'button, a, input, select, textarea, [role="button"]'
  ));
  const shown = [], hidden = [];
  for (const el of rawEls) (isVisible(el) ? shown : hidden).push(el);

  const elements = shown.concat(hidden).slice(0, 150).map(el => {
    const box = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.value || '').trim().slice(0, 80),
      selector: selectorFor(el),
      href: el.getAttribute('href') || '',
      input_type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      is_visible: isVisible(el),
      is_enabled: !el.disabled,
      bounding_box: { x: box.x, y: box.y, width: box.width, height: box.height },
      aria_label: el.getAttribute('aria-label') || '',
    };
  });

  const forms = Array.from(document.forms).map(f => {
    const submitBtn = f.querySelector('[type="submit"], button:not([type])');
    return {
      action: f.action || '',
      method: (f.method || 'get').toUpperCase(),
      submit_selector: submitBtn ? selectorFor(submitBtn) : '',
      fields: Array.from(f.elements).filter(e => e.name).map(e => ({
        name: e.name,
        type: e.type || 'text',
        required: !!e.required,
        placeholder: e.placeholder || '',
        selector: selectorFor(e),
      })),
    };
  });

  const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 200).map(a => ({
    href: a.href,
    text: (a.innerText || '').trim().slice(0, 80),
    external: a.hostname !== location.hostname,
  }));

  const images = Array.from(document.querySelectorAll('img')).slice(0, 100).map(img => ({
    src: img.src,
    alt: img.alt || '',
    loaded_ok: img.complete && img.naturalWidth > 0,
    natural_width: img.naturalWidth,
    natural_height: img.naturalHeight,
  }));

  return {
    title: document.title,
    text: document.body ? document.body.innerText.slice(0, 4000) : '',
    html: document.documentElement.outerHTML.slice(0, 8000),
    elements, forms, links, images,
  };
}
"""


def launch(state: QAState, headless: bool = True):
    """Starts a browser, opens the target URL, wires up console/network logging."""
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(viewport=state.viewport)
    page = context.new_page()

    page.on("console", lambda msg: state.add_console({
        "type": msg.type, "text": msg.text, "location": str(msg.location),
    }))
    page.on("pageerror", lambda err: state.add_page_error(str(err)))
    page.on("requestfailed", lambda req: state.add_request_failure(req.url))

    state._playwright = pw
    state._browser = browser
    state._page = page

    t0 = time.time()
    page.goto(state.url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(500)  # let late JS settle before we scrape anything
    state.page_load_ms = (time.time() - t0) * 1000

    return page


def close(state: QAState) -> None:
    try:
        if state._browser:
            state._browser.close()
    finally:
        if state._playwright:
            state._playwright.stop()
    state._browser = None
    state._page = None
    state._playwright = None


def _is_navigation_race(err: Exception) -> bool:
    """True for the errors Playwright raises when the page moved mid-evaluate."""
    msg = str(err).lower()
    return (
        "execution context was destroyed" in msg
        or "most likely because of a navigation" in msg
        or "target closed" in msg and "navigat" in msg
    )


def snapshot(page, retries: int = 2) -> PageSnapshot:
    """
    Scrapes the page currently loaded into `page` into a PageSnapshot.

    Plenty of real sites (amazon.in is one) bounce through a client-side
    redirect a moment after domcontentloaded, which destroys the execution
    context out from under page.evaluate. That isn't a page defect, it's a
    race — so wait for the new document and scrape that one instead.
    """
    t0 = time.time()
    data = None
    last_err: Exception | None = None

    for _ in range(retries + 1):
        try:
            data = page.evaluate(SCRAPE_JS)
            break
        except Exception as e:
            last_err = e
            if not _is_navigation_race(e):
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(500)

    if data is None:
        raise last_err if last_err else RuntimeError("could not scrape page")

    elements = [InteractiveElement(**e) for e in data["elements"]]
    forms = [
        FormInfo(
            action=f["action"], method=f["method"], submit_selector=f["submit_selector"],
            fields=[FormField(**fld) for fld in f["fields"]],
        )
        for f in data["forms"]
    ]
    links = [LinkInfo(href=l["href"], text=l["text"], external=l["external"]) for l in data["links"]]
    images = [ImageInfo(**i) for i in data["images"]]

    dom_hash = hashlib.sha1(data["html"].encode("utf-8", "ignore")).hexdigest()[:12]

    return PageSnapshot(
        url=page.url,
        title=data["title"],
        dom_excerpt=data["html"],
        dom_hash=dom_hash,
        text_content=data["text"],
        interactive_elements=elements,
        forms=forms,
        links=links,
        images=images,
        load_time_ms=(time.time() - t0) * 1000,
    )


def crawl(state: QAState, page, max_pages: int = 1) -> list[PageSnapshot]:
    """
    Snapshots the page currently loaded, then (if max_pages > 1) follows
    same-origin links breadth-first up to max_pages pages total. Each page
    gets a full-page screenshot too. Leaves `page` sitting on whichever
    page it visited last — callers that need to land back on the original
    URL before doing anything else have to navigate there themselves.
    """
    origin = urlparse(page.url).netloc.lower()
    start_url = page.url
    visited: list[str] = []
    queue: list[str] = [start_url]
    snapshots: list[PageSnapshot] = []

    while queue and len(visited) < max_pages:
        target = urldefrag(queue.pop(0))[0]
        if target in visited:
            continue

        if target != page.url:
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(500)
            except Exception as e:
                state.add_warning(f"crawl: couldn't reach {target}: {e}")
                continue

        visited.append(target)
        try:
            snap = snapshot(page)
        except Exception as e:
            # one unscrapeable page shouldn't sink the whole run — note it
            # and carry on with whatever else we can reach
            state.add_warning(f"crawl: couldn't scrape {target}: {e}")
            continue
        snapshots.append(snap)
        state.add_snapshot(snap)
        try:
            capture(state, page, kind="full_page", label=f"page {len(visited)}: {snap.title or target}")
        except Exception as e:
            state.add_warning(f"crawl: screenshot failed for {target}: {e}")

        for link in snap.links:
            if len(queue) >= max_pages * 4:
                break
            if link.external or not link.href:
                continue
            full = urldefrag(urljoin(page.url, link.href))[0]
            if urlparse(full).netloc.lower() != origin:
                continue
            if re.search(r"logout|signout|log-out|sign-out|delete|remove", full, re.I):
                continue  # don't crawl into anything that mutates state
            if full not in visited and full not in queue:
                queue.append(full)

    state.site_metadata["pages_discovered"] = len(snapshots)
    state.site_metadata["discovered_urls"] = [s.url for s in snapshots]
    return snapshots


def capture(
    state: QAState,
    page,
    kind: str = "viewport",
    label: str = "",
    description: str = "",
    step_index: int | None = None,
    test_id: str | None = None,
    selector: str | None = None,
) -> Screenshot:
    """Takes a screenshot, drops it in runs/<run_id>/screenshots/, and registers it on state."""
    out_dir = Path(state.output_dir) / state.run_id / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    test = state.get_test(test_id) if test_id else state.get_current_test()

    shot = Screenshot(
        kind=kind,
        label=label,
        description=description,
        phase=state.phase.value,
        test_id=test.id if test else None,
        test_name=test.name if test else "",
        step_index=step_index,
        url=page.url,
        selector=selector,
        viewport=state.viewport,
    )
    file_path = out_dir / f"{shot.id}.png"

    try:
        if selector:
            page.locator(selector).screenshot(path=str(file_path))
        elif kind == "full_page":
            page.screenshot(path=str(file_path), full_page=True)
        else:
            page.screenshot(path=str(file_path))
    except Exception:
        # selector could've disappeared, element could be off-screen, etc —
        # a plain viewport shot is still better than nothing for the report
        page.screenshot(path=str(file_path))

    shot.path = str(file_path)
    shot.relative_path = f"{state.run_id}/screenshots/{shot.id}.png"
    shot.file_size_bytes = file_path.stat().st_size if file_path.exists() else 0

    state.add_screenshot(shot)
    return shot
