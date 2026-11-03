"""Checks on the single-file dashboard that need no browser."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "openring" / "static" / "index.html").read_text(encoding="utf-8")


def run_js(code: str):
    """Run a script with Node.js and return what it prints as JSON (skips without Node.js)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js")
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=60, check=True).stdout
    return json.loads(out)


def test_campaigns_warn_about_calls_outside_8am_to_9pm_where_the_person_is():
    start = PAGE.index("const US_ZONES")
    code = PAGE[start:PAGE.index("\n}\n", PAGE.index("function callingHours")) + 2]
    checks = """
    const at = (numbers, start, end) => callingHours(numbers, new Date(start), end && new Date(end), "Europe/Lisbon")
      .map(x => x.zone + " " + x.at.toISOString());
    console.log(JSON.stringify({
      // 8 AM in New York on 2 Nov 2026 is 5 AM in Los Angeles, and 6 AM in Denver, 7 AM in Chicago.
      us: at(["+12125550100"], "2026-11-02T13:00:00Z"),
      hawaii: at(["+18085550100"], "2026-11-02T17:00:00Z"),
      saoPaulo: at(["+5511912345678"], "2026-11-02T12:00:00Z"),
      acre: at(["+5568912345678"], "2026-11-02T12:00:00Z"),
      lateWindow: at(["+5511912345678"], "2026-11-02T23:30:00Z", "2026-11-03T01:00:00Z"),
      unknownCode: at(["+999123456789"], "2026-11-02T06:00:00Z"),
      london: at(["+447700900123"], "2026-11-02T10:00:00Z"),
    }));
    """
    got = run_js(code + checks)
    assert got["us"] == ["America/Chicago 2026-11-02T13:00:00.000Z", "America/Denver 2026-11-02T13:00:00.000Z",
                         "America/Los_Angeles 2026-11-02T13:00:00.000Z"]
    # 7 AM in Honolulu, past 9 AM on the whole mainland: the area code says Hawaii.
    assert got["hawaii"] == ["Pacific/Honolulu 2026-11-02T17:00:00.000Z"]
    assert got["saoPaulo"] == []  # 9 AM
    assert got["acre"] == ["America/Rio_Branco 2026-11-02T12:00:00.000Z"]  # 7 AM: Acre is two hours behind Brasília
    # 8:30 PM to 10 PM in São Paulo: the first check past 9 PM is at 9:15 PM.
    assert got["lateWindow"] == ["America/Sao_Paulo 2026-11-03T00:15:00.000Z"]
    assert got["unknownCode"] == ["Europe/Lisbon 2026-11-02T06:00:00.000Z"]  # this computer's zone: 6 AM
    assert got["london"] == []


def test_the_assistant_editor_escapes_call_limits_from_an_imported_assistant():
    """An import saves what the other account has, even a value OpenRing would refuse: it must not run as HTML."""
    assert not re.findall(r"\$\{a\.messagePlan\?\.idle(?:TimeoutSeconds|MessageMaxSpokenCount)", PAGE)
    assert 'value="${limitsShown.silence}"' not in PAGE and 'value="${esc(limitsShown.silence)}"' in PAGE


def test_the_assistant_editor_keeps_a_limit_left_as_shown():
    line = next(x for x in PAGE.splitlines() if "const limitTyped" in x)
    got = run_js(line + """
    console.log(JSON.stringify({
      rounded: limitTyped("2", 2, 60, 90),  // 90 s, shown as 2 minutes and left alone
      unset: limitTyped("10", 10, 60, undefined) ?? "unset",
      emptied: limitTyped("", 2, 60, 90),
      typed: limitTyped("5", 2, 60, 90),
      seconds: limitTyped("20", 30, 1, undefined),
    }));""")
    assert got == {"rounded": 90, "unset": "unset", "emptied": 90, "typed": 300, "seconds": 20}
    # A limit under 30 s would round to 0 minutes, which the field refuses: nothing on the page could be saved.
    assert "minutes: Math.max(1, Math.round(" in PAGE


def test_dates_use_the_interface_language():
    # The page is in English; dates in the browser's language read "30 de out." among English words.
    assert not re.search(r"toLocale(Date|Time)?String\(\s*(\[\]|undefined)?\s*[,)]", PAGE)
    assert 'const LOCALE = "en-US"' in PAGE


def test_provider_disconnect_requires_a_second_click_before_expiry():
    helper_start = PAGE.find("const disconnectConfirmations")
    helper = PAGE[helper_start:PAGE.index("async function viewProviders")] if helper_start >= 0 else ""
    handlers = PAGE[PAGE.index("  const run = {", PAGE.index("async function viewProviders")):
                    PAGE.index('  v.querySelectorAll("[data-action]")', PAGE.index("async function viewProviders"))]
    got = run_js("""
    const assert = require('node:assert/strict');
    let now = 0, nextTimer = 0, requests = 0, refreshed = 0, failed = false;
    const timers = new Map();
    Date.now = () => now;
    const setTimeout = (fn) => { timers.set(++nextTimer, fn); return nextTimer; };
    const clearTimeout = id => timers.delete(id);
    const api = async () => { requests++; if (failed) throw Error('test failure'); };
    const viewProviders = () => { refreshed++; };
    let error;
    const status = (p, text) => { error = text; };
    const sentence = text => text;
    """ + helper + handlers + """
    (async () => {
      const p = {id: 'fake-provider'};
      const first = {textContent: 'Disconnect'}, other = {textContent: 'Disconnect'};
      await run.disconnect(p, first);
      assert.equal(requests, 0);
      assert.equal(first.textContent, 'Confirm disconnect');
      assert.equal(first.disabled, undefined);
      // Arming one card must not confirm another card.
      await run.disconnect(p, other);
      assert.equal(requests, 0);
      now = 1000;
      await run.disconnect(p, first);
      assert.equal(requests, 1);
      assert.equal(refreshed, 1);
      assert.equal(first.disabled, true);
      assert.equal(timers.size, 1); // The confirmed button's timer was cleared.
      // A late click must re-arm, even if the browser delayed the timeout callback.
      now = 4000;
      await run.disconnect(p, other);
      assert.equal(requests, 1);
      assert.equal(other.textContent, 'Confirm disconnect');
      for (const fn of timers.values()) fn();
      assert.equal(other.textContent, 'Disconnect');
      // After expiry a fresh click only asks for confirmation.
      await run.disconnect(p, other);
      assert.equal(requests, 1);
      failed = true;
      await run.disconnect(p, other);
      assert.equal(requests, 2);
      assert.equal(other.disabled, false);
      assert.equal(other.textContent, 'Disconnect');
      assert.equal(error, 'test failure');
      await run.disconnect(p, other);
      assert.equal(requests, 2); // A failed request also needs fresh confirmation.
      console.log(JSON.stringify({ok: true}));
    })().catch(err => { console.error(err); process.exitCode = 1; });
    """)
    assert got == {"ok": True}
    assert 'run[b.dataset.action](byCard(b.dataset.card), b)' in PAGE
