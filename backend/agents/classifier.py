"""Agent #1 — Site Classifier.

Input:  state.url, state.initial_snapshot
Output: state.site_type, site_type_confidence, site_metadata
"""

from state import QAState, SiteType, PageSnapshot
from agents.base import Agent


SYSTEM = """You are a website classification expert.
Classify sites into exactly ONE category.
Reply with ONLY valid JSON. No prose, no markdown fences."""


PROMPT = """Classify this website.

URL: {url}
Title: {title}

Interactive elements (first 40):
{elements}

Visible text (first 1500 chars):
{text}

Categories:
- ecommerce      : sells products (cart, checkout, "add to cart", price)
- blog           : articles, posts, comments, authors, dates
- saas_dashboard : logged-in app UI, tables, filters, settings, billing
- social         : profiles, feeds, likes, follows, messaging
- form           : primarily a form (contact, survey, application, signup)
- search         : search-first interface with results list
- portfolio      : personal/project showcase, minimal interactivity
- docs           : documentation, API refs, guides, sidebar nav
- other          : none of the above

Reply with ONLY:
{{
  "site_type": "ecommerce|blog|saas_dashboard|social|form|search|portfolio|docs|other",
  "confidence": 0.85,
  "signals": ["cart button", "product grid"],
  "metadata": {{
    "has_login": false,
    "has_search": false,
    "has_cart": false,
    "framework": "react|vue|angular|wordpress|shopify|unknown"
  }}
}}"""


class ClassifierAgent(Agent):
    name = "classifier"
    purpose = "classify"

    def run(self, state: QAState) -> QAState:
        snap = state.initial_snapshot
        if not snap:
            state.add_error("classifier", "no initial_snapshot")
            state.site_type = SiteType.OTHER
            return state

        prompt = PROMPT.format(
            url=state.url,
            title=snap.title or "(untitled)",
            elements=self._fmt_elements(snap),
            text=(snap.text_content or "")[:1500],
        )

        raw = self.ask(prompt, state, system=SYSTEM)
        data = self.extract_json(raw)

        if not isinstance(data, dict):
            self.log(state, "classifier output not a dict; defaulting to other")
            state.site_type = SiteType.OTHER
            return state

        # Site type (safe parse)
        raw_type = str(data.get("site_type", "other")).lower().strip()
        try:
            state.site_type = SiteType(raw_type)
        except ValueError:
            state.site_type = SiteType.OTHER
            self.log(state, f"unknown site_type {raw_type!r}")

        # Confidence
        try:
            state.site_type_confidence = max(
                0.0, min(1.0, float(data.get("confidence", 0.0)))
            )
        except (TypeError, ValueError):
            state.site_type_confidence = 0.0

        # Metadata — merge rather than replace, since crawling may have
        # already stashed pages_discovered/discovered_urls in here
        meta = data.get("metadata", {}) or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["signals"] = data.get("signals", []) or []
        state.site_metadata.update(meta)

        self.log(
            state,
            f"classified as {state.site_type.value} "
            f"(conf={state.site_type_confidence:.2f})",
        )
        return state

    # ── Helpers ───────────────────────────────────────────────

    def _fmt_elements(self, snap: PageSnapshot) -> str:
        if not snap.interactive_elements:
            return "(none)"
        lines = []
        for el in snap.interactive_elements[:40]:
            href = (el.href or "")[:60]
            lines.append(
                f"- <{el.tag}> text={el.text!r} type={el.input_type} href={href!r}"
            )
        return "\n".join(lines)