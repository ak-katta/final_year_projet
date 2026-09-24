"""Agent #6 — Healer.

Repairs broken JSON and broken selectors. Called by Planner, Executor, base.
"""

import json

from state import QAState
from agents.base import Agent


SYSTEM = """You fix broken output from other agents.
Reply with ONLY the corrected value. No prose. No markdown fences."""


JSON_PROMPT = """The following output was supposed to be valid JSON ({hint})
but could not be parsed.

Broken output:
{raw}

Reply with ONLY the corrected JSON."""


SELECTOR_PROMPT = """A Playwright action failed on this page.

Failed action:
{action}

Error: {error}

Available elements on the current page:
{elements}

Reply with ONLY:
{{"action": "click|fill|select|press|hover", "selector": "...", "value": "..."}}"""


class HealerAgent(Agent):
    name = "healer"
    purpose = "repair"

    def run(self, state: QAState, **kwargs) -> QAState:
        # not part of the normal pipeline — other agents call repair_json()
        # or repair_selector() on this directly when they need it
        return state

    # ── JSON repair ───────────────────────────────────────────

    def repair_json(
        self, state: QAState, raw: str, schema_hint: str = "JSON"
    ) -> str:
        prompt = JSON_PROMPT.format(
            raw=(raw or "")[:4000],
            hint=schema_hint,
        )
        fixed = self.ask(prompt, state, system=SYSTEM)
        self.log(state, "attempted JSON repair")
        return fixed

    # ── Selector repair ───────────────────────────────────────

    def repair_selector(
        self,
        state: QAState,
        action: dict,
        error: str,
        page,
    ) -> dict | None:
        elements = self._live_elements(page)
        prompt = SELECTOR_PROMPT.format(
            action=json.dumps(action),
            error=(error or "")[:300],
            elements=elements,
        )
        raw = self.ask(prompt, state, system=SYSTEM)
        fixed = self.extract_json(raw)
        if not isinstance(fixed, dict):
            return None
        if not fixed.get("selector"):
            return None
        return fixed

    # ── Live DOM snapshot for repair ──────────────────────────

    def _live_elements(self, page, limit: int = 60) -> str:
        try:
            rows = page.eval_on_selector_all(
                "button, a, input, select, textarea, [role]",
                """(els, lim) => {
                    const isUnique = (s) => {
                        try { return document.querySelectorAll(s).length === 1; }
                        catch (err) { return false; }
                    };
                    // Only offer elements the user could actually interact with.
                    // Repairing a hidden element's selector just produces a
                    // different selector for the same unclickable node, which is
                    // why repairs kept failing with the identical timeout.
                    const visible = els.filter(e => {
                        const r = e.getBoundingClientRect();
                        const st = getComputedStyle(e);
                        return r.width > 0 && r.height > 0
                            && st.visibility !== 'hidden' && st.display !== 'none'
                            && !e.disabled;
                    });
                    return visible.slice(0, lim).map(e => {
                        const tag = e.tagName.toLowerCase();
                        const aria = e.getAttribute('aria-label');
                        const role = e.getAttribute('role');
                        const placeholder = e.getAttribute('placeholder');
                        const cands = [];
                        if (e.id) cands.push('#' + e.id);
                        if (e.getAttribute('data-testid'))
                            cands.push(`[data-testid="${e.getAttribute('data-testid')}"]`);
                        if (e.name) cands.push(`${tag}[name="${e.name}"]`);
                        if (aria) cands.push(`${tag}[aria-label="${aria}"]`);
                        if (placeholder) cands.push(`${tag}[placeholder="${placeholder}"]`);
                        if (role) cands.push(`${tag}[role="${role}"]`);
                        if (e.type) cands.push(`${tag}[type="${e.type}"]`);
                        const sel = cands.find(isUnique) || cands[0] || tag;
                        return {
                            sel,
                            text: (e.innerText || e.value || placeholder || aria || '').slice(0, 40),
                        };
                    });
                }""",
                limit,
            )
            if not rows:
                return "(none)"
            return "\n".join(
                f"- {r['sel']}   # {r['text']!r}" for r in rows
            )
        except Exception:
            return "(unavailable)"