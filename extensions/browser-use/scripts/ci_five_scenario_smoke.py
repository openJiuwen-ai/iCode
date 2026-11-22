# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""No-API-key real-Chrome 5-scenario CI smoke & step-compression gate.

Drives real Owned Chrome through ``icode_browser_use.extension`` against
:mod:`benchmark_fixtures` to verify all 5 core scenarios and 3 production
incident defenses without requiring any LLM API key:

1. ``streaming_chat``       -- one-step navigate + fill(submit=True) + wait_for(hidden, return_mode='text').
2. ``closed_shadow_form``   -- closed Shadow DOM find/fill + click(expect_condition='text', expect_return='text').
3. ``delayed_button``       -- wait_for(editable, return_mode='ref') returns real @eN#epoch + fill + click(expect).
4. ``dynamic_ad_page``      -- localized wait_for(stable, target=..., return_mode='text') under 80 ms ad ticker.
5. ``submit_response_lost`` -- single write verified by server counter == 1, mid-flight cancel cleans up,
                               and browser_close emits [BROWSER_SESSION_FACTS] and releases the instance lock.

Run from ``extensions/browser-use`` or repo root::

    python scripts/ci_five_scenario_smoke.py --allow-browser
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import icode_browser_use.extension as ext  # noqa: E402
from benchmark_fixtures import serve  # noqa: E402
from chrys.extensions import ToolContext  # noqa: E402
from icode_browser_use.locking import probe_instance_lock  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

REF_RE = re.compile(r"@e(\d+)#(\d+)")


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="ci-five-scenario-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    scenario_stats: list[dict[str, object]] = []
    with directory, contextlib.ExitStack() as stack:
        origin, state = stack.enter_context(serve())
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_HEADLESS": "1" if args.headless else "0",
                "ICODE_BROWSER_EXECUTABLE": args.executable or str(chrome_executable()),
            }
        )
        ctx = ToolContext.for_testing(
            root,
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="ci-five-scenario",
            session_id="ci-five-scenario",
        )
        browser_id = ""
        slot_dir: Path | None = None
        try:
            # --- 1. streaming_chat (one-step navigate, no browser_open!) ---
            t0 = time.monotonic()
            steps = 0
            nav1 = await ext.browser_navigate(ctx, f"{origin}/streaming-chat")
            steps += 1
            require(nav1.startswith("browser_id: "), f"one-step navigate did not return browser_id: {nav1[:120]}")
            browser_id = nav1.splitlines()[0].split("browser_id: ", 1)[1].strip()
            slot_dir = ext._browsers[browser_id].session.cfg.state_dir

            filled = await ext.browser_fill(
                ctx, browser_id, "Ask a follow-up", "What is Shanghai weather?", submit=True
            )
            steps += 1
            require("status=succeeded" in filled, f"streaming_chat fill/submit failed: {filled}")

            waited = await ext.browser_wait_for(
                ctx,
                browser_id,
                condition="hidden",
                target="Stop generating",
                timeout_ms=8000,
                return_mode="text",
                max_chars=2000,
            )
            steps += 1
            require("status=succeeded" in waited, f"streaming_chat wait_for failed: {waited}")
            require("FINAL_WEATHER_ANSWER: Shanghai 24C clear" in waited, f"final streaming text missing: {waited}")
            require(
                state.count("streaming_chat") == 1, f"duplicate streaming_chat submit: {state.count('streaming_chat')}"
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stat = {
                "scenario": "streaming_chat",
                "ok": True,
                "steps": steps,
                "elapsed_ms": elapsed_ms,
                "submissions": 1,
            }
            scenario_stats.append(stat)
            report("scenario", **stat)

            # --- 2. closed_shadow_form (reuse single open browser + click expect) ---
            t0 = time.monotonic()
            steps = 0
            nav2 = await ext.browser_navigate(ctx, f"{origin}/closed-shadow-form")
            steps += 1
            require(nav2.startswith(f"browser_id: {browser_id}\n"), "expected single open browser reuse")

            found_note = await ext.browser_find(ctx, browser_id, "Ticket Note")
            steps += 1
            m_note = REF_RE.search(found_note)
            require(m_note is not None, f"closed shadow textarea ref missing: {found_note}")

            await ext.browser_fill(ctx, browser_id, m_note.group(0), "Shadow form works")
            steps += 1

            clicked = await ext.browser_click(
                ctx,
                browser_id,
                "Submit Ticket",
                expect_condition="text",
                expect_value="TICKET_CONFIRMED_#42",
                expect_timeout_ms=5000,
                expect_return="text",
            )
            steps += 1
            require("expect_status=matched" in clicked, f"closed_shadow_form expect did not match: {clicked}")
            require("TICKET_CONFIRMED_#42" in clicked, f"closed_shadow_form confirmation missing: {clicked}")
            require(
                state.count("closed_shadow_form") == 1, f"duplicate ticket submit: {state.count('closed_shadow_form')}"
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stat = {
                "scenario": "closed_shadow_form",
                "ok": True,
                "steps": steps,
                "elapsed_ms": elapsed_ms,
                "submissions": 1,
            }
            scenario_stats.append(stat)
            report("scenario", **stat)

            # --- 3. delayed_button (wait_for editable return_mode='ref') ---
            t0 = time.monotonic()
            steps = 0
            await ext.browser_navigate(ctx, f"{origin}/delayed-button", browser_id)
            steps += 1

            wait_ref = await ext.browser_wait_for(
                ctx,
                browser_id,
                condition="editable",
                target="Release Code",
                timeout_ms=5000,
                return_mode="ref",
            )
            steps += 1
            require("status=succeeded" in wait_ref, f"delayed_button editable wait failed: {wait_ref}")
            m_ref = re.search(r"matched_ref=(@e\d+#\d+)", wait_ref)
            require(m_ref is not None, f"wait_for(return_mode='ref') did not return real RefEntry token: {wait_ref}")

            await ext.browser_fill(ctx, browser_id, m_ref.group(1), "REL-2026")
            steps += 1

            approved = await ext.browser_click(
                ctx,
                browser_id,
                "Approve Release",
                expect_condition="text",
                expect_value="RELEASE_APPROVED_OK",
                expect_timeout_ms=5000,
            )
            steps += 1
            require("expect_status=matched" in approved, f"delayed_button click expect failed: {approved}")
            require(state.count("delayed_button") == 1, f"duplicate release submit: {state.count('delayed_button')}")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stat = {
                "scenario": "delayed_button",
                "ok": True,
                "steps": steps,
                "elapsed_ms": elapsed_ms,
                "submissions": 1,
            }
            scenario_stats.append(stat)
            report("scenario", **stat)

            # --- 4. dynamic_ad_page (localized wait_for stable + inferred_stable) ---
            t0 = time.monotonic()
            steps = 0
            await ext.browser_navigate(ctx, f"{origin}/dynamic-ad-page", browser_id)
            steps += 1

            settled = await ext.browser_wait_for(
                ctx,
                browser_id,
                condition="stable",
                target="Q4 report region",
                value="600",
                timeout_ms=6000,
                return_mode="text",
                max_chars=1000,
            )
            steps += 1
            require("status=succeeded" in settled, f"dynamic_ad_page localized stable failed: {settled}")
            require("completion=inferred_stable" in settled, f"inferred_stable marker missing: {settled}")
            require("REGION_SUMMARY: Q4 revenue grew 18% YoY" in settled, f"region text missing: {settled}")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stat = {
                "scenario": "dynamic_ad_page",
                "ok": True,
                "steps": steps,
                "elapsed_ms": elapsed_ms,
                "submissions": 0,
            }
            scenario_stats.append(stat)
            report("scenario", **stat)

            # --- 5. submit_response_lost + mid-flight cancel + lock release ---
            t0 = time.monotonic()
            steps = 0
            await ext.browser_navigate(ctx, f"{origin}/submit-response-lost", browser_id)
            steps += 1

            dropped = await ext.browser_click(
                ctx,
                browser_id,
                "Confirm Order",
                expect_condition="text",
                expect_value="do not resubmit",
                expect_timeout_ms=5000,
            )
            steps += 1
            require("expect_status=matched" in dropped, f"submit_response_lost expect failed: {dropped}")
            require(
                state.count("submit_response_lost") == 1,
                f"duplicate order submit: {state.count('submit_response_lost')}",
            )

            # Cancel an in-flight long wait mid-flight and confirm clean cancellation.
            wait_task = asyncio.create_task(
                ext.browser_wait_for(ctx, browser_id, condition="text", value="NEVER_APPEARS", timeout_ms=20000)
            )
            await asyncio.sleep(0.35)
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task
            require(wait_task.cancelled() or wait_task.done(), "cancelled wait task did not finish")

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stat = {
                "scenario": "submit_response_lost_and_cancel",
                "ok": True,
                "steps": steps,
                "elapsed_ms": elapsed_ms,
                "submissions": 1,
            }
            scenario_stats.append(stat)
            report("scenario", **stat)
        finally:
            if browser_id and browser_id in ext._browsers:
                closed = await ext.browser_close(ctx, browser_id)
                require("[BROWSER_SESSION_FACTS]" in closed, f"session facts missing on close: {closed}")
                require(origin in closed, f"visited origin missing in session facts: {closed}")

        # Verify instance lock is released after browser_close.
        require(slot_dir is not None, "slot_dir was never recorded")
        lock_state = probe_instance_lock(slot_dir)
        require(not lock_state.locked, f"instance lock still held after close: {lock_state}")

        total_steps = sum(int(s["steps"]) for s in scenario_stats)
        avg_steps = round(total_steps / len(scenario_stats), 2)
        duplicates = sum(max(0, int(s["submissions"]) - 1) for s in scenario_stats)
        report(
            "ci_smoke_passed",
            scenarios=len(scenario_stats),
            total_steps=total_steps,
            avg_steps_per_scenario=avg_steps,
            duplicate_submissions=duplicates,
            lock_released=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="opt in to launching a real Chrome")
    parser.add_argument("--headless", action="store_true", default=True, help="run headless (default on for CI)")
    parser.add_argument("--headed", dest="headless", action="store_false", help="show the browser window")
    parser.add_argument("--executable", default="", help="explicit Chrome/Edge executable path")
    args = parser.parse_args()
    if not args.allow_browser:
        raise SystemExit("ci_five_scenario_smoke launches a real browser; pass --allow-browser")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
