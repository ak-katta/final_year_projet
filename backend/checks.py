"""
Deterministic black-box audits that run on every page the crawler found.

These are the checks that don't need an LLM to invent them: whether links
resolve, whether the console is clean, whether form fields have labels,
whether anything overlaps. Each one produces a normal TestCase with its
status already filled in, so it lands in the same test table, the same
coverage matrix and the same report as an LLM-planned test — the only
difference is that a human wrote the assertion instead of a model.

Everything here stays black-box: it reads the rendered DOM and real HTTP
responses, never the application's source.
"""

import re
from datetime import datetime
from urllib.parse import urljoin, urldefrag, urlparse

from state import (
    QAState, TestCase, Step, Bug, ActionType, TestStatus, TestCategory,
    TestArea, TestTechnique, Severity, A11yIssue, PageSnapshot,
)
from capture import capture


# How many links to actually request per run. Checking every link on a
# big site turns a 2-minute run into a 20-minute one, and the first few
# dozen already surface the broken ones.
MAX_LINKS_CHECKED = 40
LINK_TIMEOUT_MS = 10_000

# A page slower than this is worth flagging even when it loads fine.
SLOW_PAGE_MS = 5_000


# ── Browser-side audits ─────────────────────────────────────────────

# Shared prelude for both audits.
#
# `visible()` here has to be right or every rule downstream is wrong:
# `display` is NOT an inherited property, so reading an element's OWN
# computed display never reveals a `display:none` ANCESTOR. Real sites are
# full of collapsed hover menus and flyouts whose items are laid out
# `display:flex` inside a hidden container — checking the element itself
# marked all 347 of them on flipkart.com as visible-but-unclickable.
# checkVisibility() asks the engine whether the element is actually
# rendered, which is the question we mean.
VISIBILITY_JS = """
  const sel = (el) => {
    if (el.id) return '#' + el.id;
    const tag = el.tagName.toLowerCase();
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return tag + cls;
  };
  // Parked off the left/top edge is how sites hide skip-links and
  // screen-reader-only text (`left: -9999px`). The engine still calls those
  // rendered, but a sighted user never sees them, so no visual rule should
  // fire on one. Content merely below the fold is NOT off-screen — it just
  // needs scrolling — so only negative-side placement counts.
  const offScreen = (el) => {
    const r = el.getBoundingClientRect();
    return r.right <= 0 || r.bottom <= 0;
  };
  // rendered, but possibly zero-sized — for rules that are ABOUT the size
  const rendered = (el) => {
    const ok = el.checkVisibility
      ? el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})
      : (el.getClientRects().length > 0 || getComputedStyle(el).position === 'fixed');
    return ok && !offScreen(el);
  };
  const visible = (el) => {
    if (!rendered(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
"""


A11Y_JS = """
() => {
  const issues = [];
""" + VISIBILITY_JS + """
  const accName = (el) => (
    (el.getAttribute('aria-label') || '') ||
    (el.getAttribute('title') || '') ||
    (el.innerText || '').trim() ||
    (el.getAttribute('alt') || '') ||
    (el.getAttribute('aria-labelledby') ? 'labelledby' : '') ||
    Array.from(el.querySelectorAll('img[alt]')).map(i => i.alt).join(' ').trim()
  );

  // 1. images without alt text.
  //
  // Severity depends on whether anything else names the image. An icon
  // inside a link that already has its own text is still announced, so
  // that is a tidiness issue (it wants alt=""), not a blocker — whereas an
  // unnamed image that IS the link is genuinely unusable non-visually.
  for (const img of document.querySelectorAll('img')) {
    if (!visible(img)) continue;
    if (img.hasAttribute('alt')) continue;
    if (img.getAttribute('role') === 'presentation'
        || img.getAttribute('aria-hidden') === 'true') continue;

    const host = img.closest('a, button, [role="button"]');
    const hostNamed = host && (
      (host.innerText || '').trim() ||
      host.getAttribute('aria-label') ||
      host.getAttribute('title')
    );
    issues.push({
      rule: 'image-alt',
      impact: hostNamed ? 'minor' : 'serious',
      selector: sel(img),
      description: (hostNamed
        ? 'Decorative image has no alt attribute (its link is labelled, so add alt=""): '
        : 'Image has no alt attribute and nothing else names it: ')
        + (img.getAttribute('src') || '').slice(0, 80)});
  }

  // 2. form controls without a label
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (!visible(el)) continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button') continue;
    const labelled =
      el.getAttribute('aria-label') ||
      el.getAttribute('aria-labelledby') ||
      el.getAttribute('title') ||
      (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) ||
      el.closest('label');
    if (!labelled) {
      issues.push({rule: 'form-label', impact: 'critical', selector: sel(el),
        description: 'Form control has no associated label (no <label for>, aria-label or title)'});
    }
  }

  // 3. buttons and links with no accessible name
  for (const el of document.querySelectorAll('button, a, [role="button"]')) {
    if (!visible(el)) continue;
    if (!accName(el)) {
      issues.push({rule: 'control-name', impact: 'serious', selector: sel(el),
        description: el.tagName.toLowerCase() + ' has no text, aria-label or title — a screen reader announces nothing'});
    }
  }

  // 4. document-level basics
  if (!document.documentElement.getAttribute('lang')) {
    issues.push({rule: 'html-lang', impact: 'serious', selector: 'html',
      description: '<html> has no lang attribute, so screen readers cannot pick a pronunciation'});
  }
  if (!document.title || !document.title.trim()) {
    issues.push({rule: 'document-title', impact: 'serious', selector: 'title',
      description: 'Page has no <title>'});
  }

  // 5. heading order
  const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).filter(visible);
  if (heads.length && !document.querySelector('h1')) {
    issues.push({rule: 'heading-h1', impact: 'moderate', selector: 'body',
      description: 'Page has headings but no <h1>'});
  }
  let prev = 0;
  for (const h of heads) {
    const lvl = parseInt(h.tagName[1], 10);
    if (prev && lvl > prev + 1) {
      issues.push({rule: 'heading-order', impact: 'minor', selector: sel(h),
        description: 'Heading jumps from h' + prev + ' to h' + lvl + ': "' + (h.innerText || '').trim().slice(0, 40) + '"'});
    }
    prev = lvl;
  }

  // 6. duplicate ids — breaks label[for] and aria-labelledby
  const seen = {};
  for (const el of document.querySelectorAll('[id]')) {
    const id = el.id;
    if (!id || !id.trim()) continue;   // id="" is empty, not duplicated
    if (seen[id]) {
      issues.push({rule: 'duplicate-id', impact: 'moderate', selector: '#' + id,
        description: 'Duplicate id "' + id + '" appears more than once'});
    }
    seen[id] = true;
  }

  // 7. positive tabindex hijacks the natural focus order
  for (const el of document.querySelectorAll('[tabindex]')) {
    const ti = parseInt(el.getAttribute('tabindex'), 10);
    if (ti > 0) {
      issues.push({rule: 'tabindex-positive', impact: 'minor', selector: sel(el),
        description: 'tabindex=' + ti + ' overrides the natural tab order'});
    }
  }

  // 8. Text contrast.
  //
  // Deliberately conservative: a wrong contrast failure is worse than a
  // missed one, and the background of a text node is genuinely hard to
  // resolve from CSS alone. We only judge an element when we can see the
  // whole picture — it paints its own text directly, every ancestor up to
  // the painted background is image-free, and that background actually
  // sits behind the text. Anything else is skipped rather than guessed at.
  const lum = (c) => {
    const m = c.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x));
    if (p.length > 3 && p[3] < 0.95) return null;   // translucent: unknowable
    const f = p.slice(0, 3).map(v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2];
  };

  // its own text, not a descendant's — otherwise every wrapper div gets
  // judged against the styling of the deepest child that actually paints
  const ownText = (el) => {
    for (const n of el.childNodes) {
      if (n.nodeType === 3 && n.nodeValue.trim()) return n.nodeValue.trim();
    }
    return '';
  };

  const contains = (outer, inner) =>
    outer.left <= inner.left + 1 && outer.top <= inner.top + 1 &&
    outer.right >= inner.right - 1 && outer.bottom >= inner.bottom - 1;

  /** Luminance of the background actually painted behind `el`, or null. */
  const bgBehind = (el) => {
    const rect = el.getBoundingClientRect();
    let node = el;
    while (node && node !== document.documentElement) {
      const st = getComputedStyle(node);
      // a gradient, sprite or photo behind the text: we cannot sample it
      if (st.backgroundImage && st.backgroundImage !== 'none') return null;
      const l = lum(st.backgroundColor);
      if (l !== null) {
        // the painted box has to actually cover the text for its colour
        // to be the one behind it
        return contains(node.getBoundingClientRect(), rect) ? l : null;
      }
      node = node.parentElement;
    }
    const bodySt = document.body ? getComputedStyle(document.body) : null;
    if (bodySt && bodySt.backgroundImage !== 'none') return null;
    const bodyLum = bodySt ? lum(bodySt.backgroundColor) : null;
    return bodyLum !== null ? bodyLum : 1;   // browser default is white
  };

  let checked = 0;
  for (const el of document.querySelectorAll('p, span, a, li, h1, h2, h3, td, label, button')) {
    if (checked >= 150) break;
    if (!visible(el)) continue;
    const text = ownText(el);
    if (text.length < 2) continue;

    const st = getComputedStyle(el);
    const fg = lum(st.color);
    if (fg === null) continue;
    const bg = bgBehind(el);
    if (bg === null) continue;          // couldn't determine it — say nothing
    checked++;

    const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
    const size = parseFloat(st.fontSize);
    const large = size >= 24 || (size >= 18.66 && parseInt(st.fontWeight, 10) >= 700);
    const need = large ? 3.0 : 4.5;
    if (ratio < need) {
      issues.push({rule: 'color-contrast', impact: 'serious', selector: sel(el),
        description: 'Contrast ' + ratio.toFixed(2) + ':1 is below the ' + need + ':1 minimum for "' +
          text.slice(0, 30) + '"'});
    }
  }

  return issues.slice(0, 100);
}
"""


UI_JS = """
() => {
  const issues = [];
""" + VISIBILITY_JS + """
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;

  // Only elements the engine says are actually rendered. Anything inside a
  // collapsed menu is not a UI defect, it is a menu that is closed.
  const controls = Array.from(document.querySelectorAll(
    'button, a, input, select, textarea, [role="button"]')).filter(rendered);

  // 1. rendered control with no size at all — present and painted, but
  //    occupying no space, so genuinely unclickable
  for (const el of controls) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) {
      issues.push({rule: 'zero-size-control', blocking: true, selector: sel(el),
        description: (el.innerText || el.value || el.tagName).toString().trim().slice(0, 40) +
          ' renders at ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' — it cannot be clicked'});
    }
  }

  const onscreen = controls.filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0;
  });

  // There was an "is this control covered by something?" rule here. It
  // asked the engine what sits at a control's midpoint, which is the only
  // sound way to detect occlusion — and it still could not be trusted on
  // real pages. On amazon.in it reported 27 covered nav links that are
  // plainly clickable, and blamed a different element each run (an <img>,
  // then a poster div, then a <video>) as the hero carousel settled.
  // A check that invents 27 defects on every large site is worse than no
  // check, so it is gone. Playwright's own actionability trial is the
  // trustworthy way to answer this, one element at a time, if it is ever
  // worth the per-element cost.

  // 3. content spilling past the right edge of the viewport
  for (const el of onscreen) {
    const r = el.getBoundingClientRect();
    if (r.right > vw + 2) {
      issues.push({rule: 'overflows-viewport', blocking: false, selector: sel(el),
        description: 'Extends ' + Math.round(r.right - vw) + 'px past the ' + vw + 'px viewport'});
    }
  }
  if (document.documentElement.scrollWidth > vw + 2) {
    issues.push({rule: 'horizontal-scroll', blocking: false, selector: 'html',
      description: 'Page scrolls horizontally: content is ' +
        (document.documentElement.scrollWidth - vw) + 'px wider than the viewport'});
  }

  // 4. tap targets too small to hit on a phone (WCAG 2.5.8 asks for 24px)
  for (const el of onscreen) {
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) {
      issues.push({rule: 'small-tap-target', blocking: false, selector: sel(el),
        description: Math.round(r.width) + 'x' + Math.round(r.height) + 'px is below the 24x24px minimum'});
    }
  }

  // 5. images that failed to load. A lazy image that hasn't started yet
  //    reports complete=false, so this only catches real failures.
  for (const img of document.querySelectorAll('img')) {
    if (!rendered(img)) continue;
    const src = img.getAttribute('src');
    if (src && img.complete && img.naturalWidth === 0) {
      issues.push({rule: 'broken-image', blocking: true, selector: sel(img),
        description: 'Image failed to load: ' + src.slice(0, 80)});
    }
  }

  return issues.slice(0, 80);
}
"""


# ── Helpers ─────────────────────────────────────────────────────────

def _make_test(
    name: str,
    area: TestArea,
    technique: TestTechnique,
    category: TestCategory,
    description: str,
    expected: str,
    step_description: str,
    severity: Severity = Severity.MEDIUM,
) -> TestCase:
    """A one-step TestCase used to carry a deterministic check's result."""
    return TestCase(
        name=name,
        category=category,
        area=area,
        technique=technique,
        description=description,
        expected=expected,
        severity_if_fail=severity,
        source="checks",
        steps=[Step(index=0, action=ActionType.SCREENSHOT, description=step_description)],
    )


def _finish(state: QAState, test: TestCase, ok: bool, error: str = "") -> None:
    """Record a check's verdict through the same bookkeeping an executed
    test uses, so it lands in completed/failed ids and therefore in the
    pass rate — setting test.status alone left these out of the count."""
    status = TestStatus.PASSED if ok else TestStatus.FAILED
    step = test.steps[0]
    step.status = status
    if not ok:
        step.error = error
    state.finish_test(test.id, status, error=error if not ok else None)


def _bug(
    state: QAState,
    test: TestCase,
    title: str,
    description: str,
    expected: str,
    actual: str,
    url: str,
    severity: Severity,
    category: TestCategory,
    repro: list[str],
) -> None:
    bug = Bug(
        test_id=test.id,
        title=title,
        description=description,
        severity=severity,
        category=category,
        expected=expected,
        actual=actual,
        url=url,
        steps_to_reproduce=repro,
        screenshot_ids=list(test.screenshot_ids),
        screenshot_id=test.screenshot_ids[0] if test.screenshot_ids else None,
        fingerprint=f"{title}|{url}",
    )
    state.add_bug(bug)


def _shoot(state: QAState, page, test: TestCase, label: str) -> None:
    """Evidence for a check — same treatment the executed tests get."""
    try:
        capture(state, page, kind="full_page", label=label, test_id=test.id)
    except Exception as e:
        state.add_warning(f"checks: screenshot failed for {test.id}: {e}")


def _registrable(host: str) -> str:
    """Rough registrable domain: last two labels of the hostname.

    Good enough to tell flipkart.com from a CDN, without shipping a public
    suffix list. It over-merges a few multi-part TLDs (foo.co.uk), which
    only ever makes this check more forgiving, never noisier.
    """
    host = (host or "").lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# "Access to fetch at 'https://cdn.example/x.js' from origin ..." — the
# browser logs a CORS or load failure against the PAGE, while the thing
# that actually failed is the resource named inside the message. Blaming
# the page for it marked every third-party asset failure as first-party.
_URL_IN_TEXT = re.compile(r"""https?://[^\s'"<>)]+""")


def _blamed_url(log) -> str:
    """The URL a console error is really about: the resource it names, if
    it names one, otherwise the script that logged it."""
    m = _URL_IN_TEXT.search(log.text or "")
    return m.group(0) if m else (log.location or "")


def _is_first_party(state: QAState, url: str) -> bool:
    """Is this URL the site under test, rather than a CDN or third party?"""
    try:
        own = _registrable(urlparse(state.url).netloc)
        other = _registrable(urlparse(url).netloc)
    except Exception:
        return True          # can't tell — don't silently drop it
    if not other:
        return True          # relative/inline: it's the page's own code
    return own == other


def _snaps(state: QAState) -> list[PageSnapshot]:
    return state.page_snapshots or (
        [state.initial_snapshot] if state.initial_snapshot else []
    )


def _goto(state: QAState, page, url: str) -> bool:
    if page.url == url:
        return True
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(400)
        return True
    except Exception as e:
        state.add_warning(f"checks: couldn't reach {url}: {e}")
        return False


# ── The checks ──────────────────────────────────────────────────────

def check_page_load(state: QAState, page) -> TestCase:
    """Every discovered page renders, has a title, and isn't glacially slow."""
    test = _make_test(
        "Pages load without errors",
        TestArea.PAGE_LOAD, TestTechnique.FUNCTIONAL, TestCategory.PERFORMANCE,
        "Every page the crawler reached responded, rendered a title and loaded in reasonable time",
        f"All pages load in under {SLOW_PAGE_MS}ms with a non-empty <title>",
        "Review load time and title of each discovered page",
        severity=Severity.HIGH,
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)
    _shoot(state, page, test, "Page load check")

    snaps = _snaps(state)
    if not snaps:
        _finish(state, test, False, "no pages were captured")
        return test

    problems = []
    for s in snaps:
        if not (s.title or "").strip():
            problems.append(f"{s.url} — rendered with no <title>")
        if s.load_time_ms > SLOW_PAGE_MS:
            problems.append(f"{s.url} — took {s.load_time_ms:.0f}ms to load")

    if problems:
        _finish(state, test, False, "; ".join(problems[:5]))
        _bug(
            state, test,
            title=f"{len(problems)} page load problem(s)",
            description="Pages that loaded slowly or rendered without a title.",
            expected=test.expected,
            actual="\n".join(problems[:15]),
            url=snaps[0].url,
            severity=Severity.MEDIUM,
            category=TestCategory.PERFORMANCE,
            repro=[f"Open {snaps[0].url}", "Measure load time and check the document title"],
        )
    else:
        _finish(state, test, True)
    return test


def check_console_errors(state: QAState, page) -> TestCase:
    """No JavaScript errors in the console while the agent used the site."""
    test = _make_test(
        "No JavaScript console errors",
        TestArea.CONSOLE, TestTechnique.VALIDATION, TestCategory.FUNCTIONAL,
        "Console output is captured for the whole run; any error-level entry or uncaught exception fails this check",
        "Console reports no errors and no uncaught exceptions",
        "Read console errors collected during the run",
        severity=Severity.HIGH,
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)
    _shoot(state, page, test, "Console check")

    # Uncaught exceptions are always the page's own code. Console errors get
    # attributed by the script that logged them: a blocked tracker or a CDN's
    # CORS complaint is noise the site owner cannot act on, and counting it
    # failed this check on essentially every large site.
    own, third = [], []
    for log in state.console_logs:
        if log.type != "error" or not log.text:
            continue
        (own if _is_first_party(state, _blamed_url(log)) else third).append(log.text)
    own = list(dict.fromkeys(own))
    third = list(dict.fromkeys(third))
    page_errors = list(state.page_errors)

    blocking = page_errors + own
    state.site_metadata["console_third_party"] = len(third)

    if blocking:
        _finish(state, test, False,
                f"{len(blocking)} first-party console error(s): {blocking[0][:150]}")
        _bug(
            state, test,
            title=f"{len(blocking)} JavaScript error(s) in the console",
            description=(
                "Uncaught exceptions and errors logged by the site's own scripts "
                "while the agent was driving it."
                + (f" {len(third)} further error(s) came from third-party scripts "
                   f"and are excluded." if third else "")
            ),
            expected="Console stays clean",
            actual="\n".join(f"- {e[:200]}" for e in blocking[:15]),
            url=state.url,
            severity=Severity.HIGH,
            category=TestCategory.FUNCTIONAL,
            repro=[f"Open {state.url}", "Open DevTools → Console", "Interact with the page"],
        )
    else:
        _finish(state, test, True)
        if third:
            test.description += (
                f" ({len(third)} third-party console error(s) seen and ignored)"
            )
    return test


def check_network_failures(state: QAState, page) -> TestCase:
    """No HTTP/API request failed while the agent used the site."""
    test = _make_test(
        "No failed HTTP or API requests",
        TestArea.NETWORK, TestTechnique.VALIDATION, TestCategory.FUNCTIONAL,
        "Every network request the browser made is watched; failures and 4xx/5xx responses fail this check",
        "All requests complete successfully",
        "Read failed network requests collected during the run",
        severity=Severity.HIGH,
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)
    _shoot(state, page, test, "Network check")

    # Same split as the console check: an ad network 404ing is not this
    # site's defect, and third-party beacons fail constantly on real pages.
    own, third = [], []
    for e in state.network_failures:
        line = f"{e.status or 'failed'} {e.method} {e.url}"
        (own if _is_first_party(state, e.url) else third).append(line)
    own = list(dict.fromkeys(own))
    third = list(dict.fromkeys(third))
    state.site_metadata["network_third_party"] = len(third)

    if own:
        _finish(state, test, False,
                f"{len(own)} failed first-party request(s): {own[0][:150]}")
        _bug(
            state, test,
            title=f"{len(own)} failed network request(s)",
            description=(
                "Requests to this site that errored out or returned 4xx/5xx."
                + (f" {len(third)} further third-party failure(s) excluded."
                   if third else "")
            ),
            expected="All requests to the site succeed",
            actual="\n".join(f"- {f[:200]}" for f in own[:20]),
            url=state.url,
            severity=Severity.HIGH,
            category=TestCategory.FUNCTIONAL,
            repro=[f"Open {state.url}", "Open DevTools → Network", "Look for failed or 4xx/5xx requests"],
        )
    else:
        _finish(state, test, True)
        if third:
            test.description += (
                f" ({len(third)} third-party request failure(s) seen and ignored)"
            )
    return test


def check_broken_links(state: QAState, page) -> TestCase:
    """Request every discovered link and report the ones that don't resolve."""
    test = _make_test(
        "No broken links",
        TestArea.LINKS, TestTechnique.NEGATIVE, TestCategory.FUNCTIONAL,
        f"Up to {MAX_LINKS_CHECKED} links found while crawling are requested directly; anything 4xx/5xx or unreachable is a broken link",
        "Every link resolves to a successful response",
        "Request each discovered link and record its status",
        severity=Severity.HIGH,
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)
    _shoot(state, page, test, "Broken link check")

    # Collect unique http(s) links across every page we snapshotted
    targets: dict[str, str] = {}   # url -> link text
    for snap in _snaps(state):
        for link in snap.links:
            href = (link.href or "").strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            full = urldefrag(urljoin(snap.url, href))[0]
            if not full.startswith(("http://", "https://")):
                continue
            targets.setdefault(full, link.text or "")

    if not targets:
        _finish(state, test, True)
        test.error = None
        state.site_metadata["links_checked"] = 0
        return test

    checked = list(targets.items())[:MAX_LINKS_CHECKED]
    broken: list[dict] = []

    unverified: list[dict] = []
    for url, text in checked:
        status, err = _probe(page, url, referer=state.url)
        if status in UNVERIFIABLE_STATUSES:
            # blocked, throttled or behind auth — report it, don't fail on it
            unverified.append({"url": url, "text": text[:80], "status": status})
            is_broken = False
        else:
            is_broken = err != "" or status >= 400
        if is_broken:
            broken.append({
                "url": url,
                "text": text[:80],
                "status": status,
                "error": err,
            })
        # write the verdict back onto the snapshot's own link entries
        for snap in _snaps(state):
            for link in snap.links:
                if link.href and urldefrag(urljoin(snap.url, link.href))[0] == url:
                    link.is_broken = is_broken

    state.broken_links = broken
    state.site_metadata["links_checked"] = len(checked)
    state.site_metadata["links_unverified"] = len(unverified)
    if unverified:
        state.add_warning(
            f"{len(unverified)} link(s) could not be verified (auth, rate limit "
            f"or bot protection) and were not counted as broken"
        )

    if broken:
        detail = "\n".join(
            f"- [{b['status'] or 'unreachable'}] {b['url']}"
            + (f" (link text: {b['text']!r})" if b["text"] else "")
            + (f" — {b['error']}" if b["error"] else "")
            for b in broken[:20]
        )
        _finish(state, test, False, f"{len(broken)} of {len(checked)} links are broken")
        _bug(
            state, test,
            title=f"{len(broken)} broken link(s)",
            description=f"Checked {len(checked)} links; these did not resolve.",
            expected="Every link returns a successful status",
            actual=detail,
            url=state.url,
            severity=Severity.HIGH if len(broken) > 2 else Severity.MEDIUM,
            category=TestCategory.FUNCTIONAL,
            repro=[f"Open {state.url}", "Follow each link", "Note the ones that 404 or fail to load"],
        )
    else:
        _finish(state, test, True)
    return test


# Statuses that mean "we were not allowed to look", not "this link is dead".
# Bot protection, rate limiting and auth walls all answer this way, and
# calling them broken links would blame the site for defending itself.
UNVERIFIABLE_STATUSES = {401, 403, 405, 407, 429, 451, 500, 502, 503, 504}

BROWSERISH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _probe(page, url: str, referer: str = "") -> tuple[int, str]:
    """HEAD the URL, falling back to GET for servers that reject HEAD.

    Uses the page's own request context so cookies and the session's user
    agent carry over — a logged-in area shouldn't read as broken just
    because we asked anonymously — and sends the headers a browser would,
    since plenty of sites answer a bare request differently.
    """
    headers = dict(BROWSERISH_HEADERS)
    if referer:
        headers["Referer"] = referer

    def get() -> int:
        return page.request.get(
            url, timeout=LINK_TIMEOUT_MS, max_redirects=5, headers=headers
        ).status

    try:
        status = page.request.head(
            url, timeout=LINK_TIMEOUT_MS, max_redirects=5, headers=headers
        ).status
        # HEAD is widely unimplemented or specially guarded — never trust a
        # failure from it alone, always confirm with a real GET
        if status >= 400:
            status = get()
        return status, ""
    except Exception as e:
        try:
            return get(), ""
        except Exception as e2:
            return 0, str(e2)[:120] or str(e)[:120]


def check_accessibility(state: QAState, page) -> TestCase:
    """Basic a11y rules: alt text, labels, control names, lang, headings, contrast."""
    test = _make_test(
        "Basic accessibility checks",
        TestArea.ACCESSIBILITY, TestTechnique.ACCESSIBILITY, TestCategory.ACCESSIBILITY,
        "Each page is audited for missing alt text, unlabelled form controls, controls with no accessible name, missing lang/title, heading order, duplicate ids and low text contrast",
        "No critical or serious accessibility issues",
        "Run the accessibility audit on each discovered page",
        severity=Severity.MEDIUM,
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)

    issues: list[A11yIssue] = []
    for snap in _snaps(state)[:8]:
        if not _goto(state, page, snap.url):
            continue
        try:
            raw = page.evaluate(A11Y_JS)
        except Exception as e:
            state.add_warning(f"a11y audit failed on {snap.url}: {e}")
            continue
        _shoot(state, page, test, f"Accessibility audit: {snap.title or snap.url}")
        for item in raw:
            issues.append(A11yIssue(
                rule=item.get("rule", ""),
                impact=item.get("impact", "") if item.get("impact") in
                    ("critical", "serious", "moderate", "minor") else "",
                selector=item.get("selector", ""),
                description=f"{item.get('description', '')}  [{snap.url}]",
                help_url="https://www.w3.org/WAI/WCAG21/quickref/",
            ))

    state.a11y_issues = issues[:200]
    serious = [i for i in issues if i.impact in ("critical", "serious")]

    if serious:
        by_rule: dict[str, int] = {}
        for i in issues:
            by_rule[i.rule] = by_rule.get(i.rule, 0) + 1
        detail = "\n".join(f"- [{i.impact}] {i.rule} @ {i.selector} — {i.description}"
                           for i in issues[:25])
        _finish(state, test, False,
                f"{len(serious)} serious/critical a11y issue(s) across {len(issues)} total "
                f"({', '.join(f'{k}×{v}' for k, v in sorted(by_rule.items()))})")
        _bug(
            state, test,
            title=f"{len(serious)} serious accessibility issue(s)",
            description="Basic WCAG checks run against the rendered DOM of each page.",
            expected="No critical or serious accessibility issues",
            actual=detail,
            url=state.url,
            severity=Severity.HIGH if any(i.impact == "critical" for i in issues) else Severity.MEDIUM,
            category=TestCategory.ACCESSIBILITY,
            repro=[f"Open {state.url}", "Inspect images, form controls and headings",
                   "Compare against WCAG 2.1 A/AA"],
        )
    else:
        _finish(state, test, True)
        if issues:
            test.error = None
    return test


def check_ui_sanity(state: QAState, page) -> TestCase:
    """Missing, overlapping, oversized or unclickable UI elements."""
    test = _make_test(
        "UI elements render correctly",
        TestArea.UI, TestTechnique.UI, TestCategory.UI,
        "Each page is measured for zero-size controls, controls overflowing the viewport, overlapping controls, tap targets under 24px and images that failed to load",
        "No overlapping, missing or unclickable UI elements",
        "Measure the rendered geometry of every interactive element",
        severity=Severity.MEDIUM,
    )
    test.started_at = datetime.now()
    state.test_plan.append(test)

    found: list[dict] = []
    for snap in _snaps(state)[:8]:
        if not _goto(state, page, snap.url):
            continue
        try:
            raw = page.evaluate(UI_JS)
        except Exception as e:
            state.add_warning(f"UI audit failed on {snap.url}: {e}")
            continue
        _shoot(state, page, test, f"UI audit: {snap.title or snap.url}")
        for item in raw:
            item["url"] = snap.url
            found.append(item)

    state.ui_issues = found[:150]

    # The audit marks each finding as blocking or advisory. Occlusion,
    # tap-target size and horizontal scroll depend on viewport and scroll
    # position, so they get reported but don't fail the test; only defects
    # that are wrong at any size do.
    blocking = [f for f in found if f.get("blocking")]

    if blocking:
        detail = "\n".join(f"- [{f['rule']}] {f['selector']} — {f['description']}  ({f['url']})"
                           for f in found[:25])
        _finish(state, test, False, f"{len(blocking)} UI defect(s) across {len(found)} findings")
        _bug(
            state, test,
            title=f"{len(blocking)} UI rendering defect(s)",
            description="Elements that overlap, render at zero size or fail to load.",
            expected="Every visible control is clickable and nothing overlaps",
            actual=detail,
            url=state.url,
            severity=Severity.MEDIUM,
            category=TestCategory.UI,
            repro=[f"Open {state.url}", "Inspect the flagged elements' bounding boxes"],
        )
    else:
        _finish(state, test, True)
    return test


# ── Entry point ─────────────────────────────────────────────────────

def run_all(state: QAState, page) -> list[TestCase]:
    """
    Runs every deterministic audit and appends the results to state.test_plan.

    Each check is isolated: one blowing up must not take the others (or
    the run) down with it, since these execute after the LLM tests have
    already produced results worth reporting.
    """
    results: list[TestCase] = []
    for fn in (
        check_page_load,
        check_console_errors,
        check_network_failures,
        check_broken_links,
        check_accessibility,
        check_ui_sanity,
    ):
        try:
            results.append(fn(state, page))
        except Exception as e:
            state.add_warning(f"check {fn.__name__} failed: {e}")
    return results
