"""
Free Browser Agent — autonomous, multi-tab, multi-step browsing using Groq
(free tier) + Termux Browser Pilot's persistent daemon.

Two layers:
  1. PLANNER (one LLM call): reads your plain-language instruction and
     breaks it into an ordered list of steps. Each step says whether it
     needs a fresh tab, what its specific goal is, and which earlier
     step's output (if any) it depends on.
  2. EXECUTOR (the per-step agent loop): for each planned step, gives Groq
     real tool-calling access to nearly every tbp command (~90 tools,
     loaded straight from src/mcp_server.py — drag, iframe, upload, geo,
     cookies, sessions, shadow DOM, all of it) and lets the model call
     whichever tools it needs, observing results, until the step's goal
     is met. Its result is stored in a shared blackboard dict that later
     steps can reference.

This works for ANY task you describe, not just one fixed workflow — the
planner decides how many tabs/steps are needed, and the executor has the
full command surface available rather than a hand-picked subset.

Usage:
    python -m src.agent "go to https://example.com, type the video link
    into the input field, then click Get Transcript"

    # multi-tab example:
    python -m src.agent "in tab 1 (already open, logged into YouTube)
    search 'বাংলা নাটক', pick the most viral recent result and copy its
    link. open a new tab, go to mytranscript.site, paste that link and
    get the transcript. open another new tab, go to cleanup.site, paste
    the transcript and remove timestamps/junk, give me the final clean text."

Requires:
    pip install groq --break-system-packages
    export GROQ_API_KEY=gsk_xxx
"""

import asyncio
import inspect
import json
import os
import sys

from .client import send_command
from .tool_registry import load_tool_schemas

# ── Config ──────────────────────────────────────────────

# Groq deprecated llama-3.1-8b-instant / llama-3.3-70b-versatile on 2026-06-17.
# gpt-oss-20b is the recommended free-tier replacement: fast, cheap, and
# supports tool calling, which is what this agent relies on (no vision
# needed — page state is read as text via browser_text/browser_find/etc).
MODEL = os.environ.get("TBP_AGENT_MODEL", "openai/gpt-oss-20b")
MAX_STEPS = int(os.environ.get("TBP_AGENT_MAX_STEPS", "20"))
PAGE_TEXT_LIMIT = 1500  # keep prompts small/fast/cheap

SYSTEM_PROMPT = """You are a browser automation agent. You control a real \
Firefox/Chromium browser through tool calls, working on ONE step of a larger \
plan — focus only on the step goal given to you, not the whole plan.

You have access to the full Termux Browser Pilot toolset: navigation \
(browser_goto, browser_back, ...), reading (browser_text, browser_html, \
browser_links, browser_a11y), smart element discovery (browser_find — search \
by visible text instead of guessing CSS selectors; browser_elements — list \
all links/buttons/inputs/forms with their selectors), interaction \
(browser_click, browser_type, browser_press, browser_scroll, browser_select, \
browser_check, ...), tabs (browser_tab_new, browser_tab_to, ...), and many \
more specialized tools (cookies, storage, iframes, file upload, screenshots, \
etc) — use whichever fits the step.

Workflow per step:
1. If you don't know what's on the page yet, call browser_text and/or \
browser_find/browser_elements first to see what's there. Prefer \
browser_find(text="...") to locate something by what it says, rather than \
guessing a CSS selector.
2. Take ONE tool action, look at the result, then decide the next action.
3. When the step's goal is fully accomplished, respond with plain text \
(no tool call) starting with "DONE:" followed by the actual output data \
this step produced — the real extracted link/text/decision, not just a \
summary, since later steps depend on this exact content.
4. If you get stuck after reasonable attempts, respond with plain text \
starting with "FAILED:" followed by the reason.

Rules:
- Never invent a CSS selector — get it from browser_find or browser_elements
  results, or from a prior tool call's output.
- If INPUT DATA from a previous step is given below, use it exactly as
  provided (e.g. paste a link/text verbatim) — don't paraphrase it.
- Don't call more tools after you have enough info to finish the step —
  respond with DONE: as soon as the goal is met.
"""

PLANNER_SYSTEM_PROMPT = """You break a user's plain-language browsing task into an \
ordered list of steps for a browser automation pipeline. Each step is executed by a \
separate agent that can only see one page at a time and follow simple instructions —
so each step's goal must be self-contained and concrete.

Reply with ONLY a JSON object: {"steps": [...]}. Each step object has:
  "id": short identifier, e.g. "step1"
  "tab": "current" (continue in the tab already open/used by the previous step,
          or the tab that was already open before the pipeline started, if this
          is the first step) or "new" (open a brand new tab for this step)
  "goal": a clear, self-contained instruction for what this step must accomplish
          on its page(s) — include any URL to visit if relevant
  "uses_input_from": id of an earlier step whose "result" this step needs as
          input, or null if this step doesn't depend on prior output
  "produces_output": true if this step should extract/return data (a link, a
          piece of text, a decision) that a later step or the user will need,
          false if it's purely an action with no data to carry forward

Rules:
- The very first step's tab is normally "current" (use the tab the user already
  has open), unless the user clearly says to start somewhere new.
- Steps that operate on a clearly different website/purpose than the previous
  step should usually open a "new" tab, so earlier tabs/state aren't disturbed.
- Keep goals concrete and scoped to ONE page/site's worth of work each. Split
  multi-site tasks into multiple steps.
- If the user's request is already a single simple action, return just one step.
- Output ONLY the JSON object, nothing else.
"""


def _groq_client():
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError(
            "Missing dependency. Run: pip install groq --break-system-packages"
        )
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY environment variable first.")
    return Groq(api_key=api_key)


def _extract_json(raw):
    """Groq sometimes wraps JSON in ```json fences despite instructions — strip them."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model output: {raw[:200]}")
    return json.loads(raw[start:end + 1])


async def _call_tool(name_to_fn, tool_name, kwargs):
    """Invoke one tool's underlying function (sync or async) and return a
    JSON-serializable result for feeding back to the model.
    """
    fn = name_to_fn.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as e:
        return {"error": str(e)}


def _trim_for_prompt(value, limit=PAGE_TEXT_LIMIT):
    """Tool results can be large (full page text, big legends) — keep the
    free-tier prompt small by truncating what goes back to the model.
    """
    s = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if len(s) > limit:
        return s[:limit] + f"... [truncated, {len(s)} chars total]"
    return s


async def run_step(client, tool_schemas, name_to_fn, goal, input_data=None,
                    max_steps=MAX_STEPS, verbose=True):
    """Run a real tool-calling loop for ONE step's goal, in whichever tab is
    currently active. The model calls tools directly (browser_click,
    browser_type, browser_find, etc.) until it responds with plain text
    starting "DONE:" or "FAILED:".
    Returns {"status": ..., "result"/"reason": ..., "steps": n}.
    """
    user_intro = f"STEP GOAL: {goal}\n"
    if input_data:
        user_intro += f"\nINPUT DATA FROM PREVIOUS STEP:\n{_trim_for_prompt(input_data)}\n"
    user_intro += "\nStart working on this step now."

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_intro},
    ]

    for step in range(1, max_steps + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tool_schemas,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=800,
            )
        except Exception as e:
            if verbose:
                print(f"    -> Groq API error: {type(e).__name__}: {e}")
            return {"status": "fail", "reason": f"LLM call error: {e}", "steps": step}

        msg = resp.choices[0].message

        # Model decided it's done/failed (plain text, no tool call)
        if not msg.tool_calls:
            text = (msg.content or "").strip()
            if verbose:
                print(f"    [substep {step}] (no tool call) {text[:150]}")

            if text.upper().startswith("DONE:"):
                result = text[5:].strip()
                if verbose:
                    print(f"    -> step done: {result[:120]}")
                return {"status": "done", "result": result, "steps": step}

            if text.upper().startswith("FAILED:"):
                reason = text[7:].strip()
                if verbose:
                    print(f"    -> step failed: {reason}")
                return {"status": "fail", "reason": reason, "steps": step}

            # Model said something without a clear DONE/FAILED marker —
            # nudge it back on track rather than guessing.
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": (
                "Please either call a tool to make progress, or respond "
                "with 'DONE: <result>' or 'FAILED: <reason>'."
            )})
            continue

        # Model wants to call one or more tools — execute each, feed results back.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                } for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            tool_name = tc.function.name
            try:
                kwargs = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                kwargs = {}

            if verbose:
                print(f"    [substep {step}] tool: {tool_name}({kwargs})")

            result = await _call_tool(name_to_fn, tool_name, kwargs)

            if verbose:
                preview = _trim_for_prompt(result, 200)
                print(f"      -> {preview}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": _trim_for_prompt(result),
            })

        await asyncio.sleep(0.4)

    if verbose:
        print(f"    -> step stopped after {max_steps} substeps without DONE/FAILED")
    return {"status": "max_steps", "steps": max_steps}


# ── Planner ─────────────────────────────────────────────

async def plan_task(client, instruction):
    """One LLM call: turn a plain-language multi-step/multi-tab instruction
    into an ordered list of step dicts. See PLANNER_SYSTEM_PROMPT for shape.
    """
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ],
        temperature=0.2,
        max_tokens=1200,
    )
    raw = resp.choices[0].message.content
    parsed = _extract_json(raw)
    steps = parsed.get("steps", [])
    if not steps:
        raise ValueError(f"Planner returned no steps: {raw[:300]}")
    return steps


# ── Pipeline orchestrator ───────────────────────────────

async def run_pipeline(instruction, max_steps_per_stage=MAX_STEPS, verbose=True):
    """Plan the instruction into steps, then execute each step in the right
    tab, passing each step's output to whichever later step needs it.
    """
    client = _groq_client()
    tool_schemas, name_to_fn = load_tool_schemas()
    if verbose:
        print(f"Loaded {len(tool_schemas)} tools for the agent to use.")

    if verbose:
        print("Planning...")
    steps = await plan_task(client, instruction)
    if verbose:
        print(f"Plan has {len(steps)} step(s):")
        for s in steps:
            print(f"  - {s.get('id', '?')} [{s.get('tab', 'current')}]: "
                  f"{s.get('goal', '')[:100]}")

    results = {}  # step_id -> result value produced by that step

    for i, step in enumerate(steps):
        step_id = step.get("id", f"step{i+1}")
        if verbose:
            print(f"\n=== {step_id} ===")

        # Tab routing: open a new tab if the plan calls for it, otherwise
        # stay in whatever tab the previous step left active.
        if step.get("tab") == "new":
            r = await send_command("tab_new")
            if verbose:
                ok = r.get("success") if isinstance(r, dict) else r
                print(f"  opened new tab -> {ok}")

        input_data = None
        dep = step.get("uses_input_from")
        if dep and dep in results:
            input_data = results[dep]

        outcome = await run_step(
            client,
            tool_schemas,
            name_to_fn,
            goal=step.get("goal", ""),
            input_data=input_data,
            max_steps=max_steps_per_stage,
            verbose=verbose,
        )

        if outcome["status"] == "done" and step.get("produces_output"):
            results[step_id] = outcome.get("result", "")

        if outcome["status"] in ("fail", "max_steps"):
            if verbose:
                print(f"\n⚠️  Pipeline stopped at {step_id}: {outcome}")
            return {"status": "stopped_at", "step": step_id, "outcome": outcome,
                    "results_so_far": results}

    if verbose:
        print("\n✅ Pipeline complete.")
    return {"status": "done", "results": results}


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.agent \"<task in plain language>\"")
        sys.exit(1)
    instruction = " ".join(sys.argv[1:])
    result = asyncio.run(run_pipeline(instruction))
    print("\n--- final result ---")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
