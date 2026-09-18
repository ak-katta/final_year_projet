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
  function selectorFor(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const testId = el.getAttribute('data-testid');
    if (testId) return `[data-testid="${testId}"]`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;

    // walk up a few parents building a nth-of-type path as a last resort
    let path = [];
    let node = el;
    for (let i = 0; i < 4 && node && node.nodeType === 1; i++) {
      let piece = node.tagName.toLowerCase();
      if (node.parentElement) {
        const sibs = Array.from(node.parentElement.children).filter(s => s.tagName === node.tagName);
        if (sibs.length > 1) piece += `:nth-of-type(${sibs.indexOf(node) + 1})`;
      }
      path.unshift(piece);
      node = node.parentElement;
    }
    return path.join(' > ');
  }

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  const elements = Array.from(document.querySelectorAll(
    'button, a, input, select, textarea, [role="button"]'
  )).slice(0, 150).map(el => {
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


def snapshot(page) -> PageSnapshot:
    """Scrapes the page currently loaded into `page` into a PageSnapshot."""
    t0 = time.time()
    data = page.evaluate(SCRAPE_JS)

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
                page.wait_for_timeout(300)
            except Exception as e:
                state.add_warning(f"crawl: couldn't reach {target}: {e}")
                continue

        visited.append(target)
        snap = snapshot(page)
        snapshots.append(snap)
        state.add_snapshot(snap)
        capture(state, page, kind="full_page", label=f"page {len(visited)}: {snap.title or target}")

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
