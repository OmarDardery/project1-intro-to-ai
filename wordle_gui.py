"""

THIS IS MADE BY AGENTIC AI, NOT BY US


wordle_gui.py
==============
ipywidgets GUI for the Wordle Human-vs-AI notebook.

Provides a single public function::

    make_game(*, words, allowed_words, max_turns,
              wordle_feedback, feedback_to_emoji,
              choose_greedy_guess, prune_candidates) -> widgets.VBox

All engine logic is injected as keyword arguments so this module has
no dependency on notebook globals and can be imported from any context.

Example (in the notebook launch cell)::

    from wordle_gui import make_game
    display(make_game(
        words=WORDS,
        allowed_words=ALLOWED_WORDS,
        max_turns=MAX_TURNS,
        wordle_feedback=wordle_feedback,
        feedback_to_emoji=feedback_to_emoji,
        choose_greedy_guess=choose_greedy_guess,
        prune_candidates=prune_candidates,
    ))
"""
import ipywidgets as widgets
import random
import time
import threading
from collections import Counter

# ── Palette ───────────────────────────────────────────────────────────────────
# Maps G/Y/B/"" feedback codes to hex colours for tiles and keyboard keys.
# The "" key is the neutral (pre-guess) state.
# _TILE_BDR sets filled tiles' border to match their background so the border
# is invisible but still present in the DOM — this is what keeps all tiles the
# same rendered size with box-sizing:border-box.
_TILE_BG  = {"G": "#6aaa64", "Y": "#c9b458", "B": "#787c7e", "": "#ffffff"}
_TILE_FG  = {"G": "#ffffff", "Y": "#ffffff", "B": "#ffffff", "": "#1a1a1b"}
_TILE_BDR = {"G": "#6aaa64", "Y": "#c9b458", "B": "#787c7e", "": "#d3d6da"}
_KEY_BG   = {"G": "#6aaa64", "Y": "#c9b458", "B": "#3a3a3c", "": "#d3d6da"}
_PRIORITY = {"G": 3, "Y": 2, "B": 1, "": 0}   # key-colour update priority

_QWERTY = ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]

# Shared CSS for every tile <div>.
# box-sizing:border-box: border counts inside width/height so every tile —
# filled, active, or empty — renders at exactly the declared pixel size.
_TILE_BASE = (
    "display:inline-flex;align-items:center;justify-content:center;"
    "border-radius:3px;margin:2px;box-sizing:border-box;font-weight:bold;"
)
_LOG_STYLE = "font-size:11px;min-height:155px;width:295px;margin:4px 0;line-height:1.5"


# ── Internal helpers ──────────────────────────────────────────────────────────
def _validate(guess, allowed_set):
    """Return (True, normalised) or (False, error_message)."""
    g = guess.strip().lower()
    if len(g) != 5 or not g.isalpha():
        return False, "Guess must be exactly 5 letters."
    if g not in allowed_set:
        return False, "Guess is not in the word list."
    return True, g


def _tile_html(ch, fb, size):
    """Render a single coloured tile as an HTML string."""
    fs = max(size // 3, 14)
    return (
        f'<div style="{_TILE_BASE}width:{size}px;height:{size}px;font-size:{fs}px;'
        f'background:{_TILE_BG[fb]};color:{_TILE_FG[fb]};'
        f'border:2px solid {_TILE_BDR[fb]}">{ch}</div>'
    )


def _render_grid(history, cur="", max_turns=6, size=52):
    """
    Build the full N×5 HTML tile grid.

    - Completed rows: coloured tiles from ``history``.
    - Active row: neutral tiles showing ``cur`` (darker border when a letter
      is present).
    - Remaining rows: blank bordered squares.
    """
    rows = []
    for i in range(max_turns):
        if i < len(history):
            g, fb = history[i]
            cells = [_tile_html(ch, f, size) for ch, f in zip(g.upper(), fb)]
        elif i == len(history):
            fs = max(size // 3, 14)
            cells = []
            for j in range(5):
                ch  = cur[j].upper() if j < len(cur) else ""
                bdr = "#878a8c" if ch else "#d3d6da"
                cells.append(
                    f'<div style="{_TILE_BASE}width:{size}px;height:{size}px;font-size:{fs}px;'
                    f'background:#ffffff;color:#1a1a1b;border:2px solid {bdr}">{ch}</div>'
                )
        else:
            cells = [
                f'<div style="{_TILE_BASE}width:{size}px;height:{size}px;'
                f'background:#ffffff;border:2px solid #d3d6da"></div>'
            ] * 5
        rows.append(f'<div style="display:flex">{"".join(cells)}</div>')
    return "".join(rows)


def _score_html(score):
    """Render the Human / AI / Ties tally line."""
    return (
        '<div style="font-family:sans-serif;font-size:14px;text-align:center;margin:6px 0">'
        f'Human: <b>{score["human"]}</b> &nbsp;│&nbsp; '
        f'AI: <b>{score["ai"]}</b> &nbsp;│&nbsp; '
        f'Ties: <b>{score["tie"]}</b></div>'
    )


# ── Public API ────────────────────────────────────────────────────────────────
def make_game(*, words, allowed_words, max_turns,
              wordle_feedback, feedback_to_emoji,
              choose_greedy_guess, prune_candidates):
    """
    Build and return the complete Wordle ipywidgets UI as a single ``VBox``.

    Parameters
    ----------
    words               : list[str]   candidate / answer word pool
    allowed_words       : list[str]   valid guess words (used for validation)
    max_turns           : int         maximum guesses per round
    wordle_feedback     : callable    ``(secret, guess) -> tuple[str, ...]``
    feedback_to_emoji   : callable    ``tuple[str, ...] -> str``
    choose_greedy_guess : callable    ``(candidates) -> (guess, score, freq)``
    prune_candidates    : callable    ``(candidates, guess, feedback) -> list``
    """
    allowed_set = set(allowed_words)

    s = {
        "secret":  random.choice(words),
        "h_hist":  [],            # [(guess, feedback), ...]
        "ai_hist": [],            # filled during AI animation
        "cur":     "",            # letters typed this turn (max 5)
        "key_st":  {},            # letter -> best feedback colour seen
        "done":    False,         # human round over?
        "score":   {"human": 0, "ai": 0, "tie": 0},
        "tile_sz": 52,
    }

    # ── Widgets ───────────────────────────────────────────────────────────────
    title   = widgets.HTML(
        '<h2 style="font-family:\'Clear Sans\',sans-serif;letter-spacing:3px;'
        'text-align:center;margin:8px 0">WORDLE: Human vs Greedy AI</h2>'
    )
    h_lbl   = widgets.HTML('<b style="font-family:sans-serif;font-size:14px">YOU</b>')
    ai_lbl  = widgets.HTML('<b style="font-family:sans-serif;font-size:14px">GREEDY AI</b>')
    h_grid  = widgets.HTML(_render_grid([], max_turns=max_turns, size=s["tile_sz"]))
    a_grid  = widgets.HTML(_render_grid([], max_turns=max_turns, size=s["tile_sz"]))
    a_log   = widgets.HTML(f'<pre style="{_LOG_STYLE};color:#666">AI log appears here…</pre>')
    score_w = widgets.HTML(_score_html(s["score"]))
    status  = widgets.HTML(
        '<div style="height:22px;font-family:sans-serif;font-size:13px;text-align:center"></div>'
    )

    # Physical keyboard: user types here and presses Enter to submit.
    # On-screen keyboard clicks also update this field via _set_cur().
    text_in = widgets.Text(
        placeholder="type a word + Enter",
        layout=widgets.Layout(width="180px", height="36px"),
    )

    # On-screen keyboard
    kbtns = {}
    for row in _QWERTY:
        for ch in row:
            b = widgets.Button(
                description=ch,
                layout=widgets.Layout(width="34px", height="38px", margin="2px"),
            )
            b.style.button_color = _KEY_BG[""]
            b.style.font_weight  = "bold"
            b.on_click(lambda e, c=ch: _key(c))
            kbtns[ch] = b

    back_btn   = widgets.Button(description="←",        layout=widgets.Layout(width="46px", height="38px", margin="2px"))
    sub_btn    = widgets.Button(description="SUBMIT",   button_style="success", layout=widgets.Layout(width="90px", height="38px", margin="4px"))
    ng_btn     = widgets.Button(description="New Game", button_style="info",    layout=widgets.Layout(width="90px", height="38px", margin="4px"))
    expand_btn = widgets.Button(description="⤢ Expand",                         layout=widgets.Layout(width="90px", height="38px", margin="4px"))

    back_btn.style.button_color = _KEY_BG[""]
    back_btn.on_click(lambda _: _back())
    sub_btn.on_click(lambda _: _submit())
    ng_btn.on_click(lambda _: _new_game())
    expand_btn.on_click(lambda _: _toggle_expand())

    def _kb_row(chars, extra=None):
        btns = [kbtns[c] for c in chars] + ([extra] if extra else [])
        return widgets.HBox(btns, layout=widgets.Layout(justify_content="center"))

    # ── State helpers ─────────────────────────────────────────────────────────
    def _refresh_human():
        h_grid.value = _render_grid(
            s["h_hist"], s["cur"], max_turns=max_turns, size=s["tile_sz"]
        )

    def _apply_key_colors(guess, feedback):
        """Update keyboard colours; green beats yellow beats grey (never downgrade)."""
        for ch, fb in zip(guess, feedback):
            if _PRIORITY.get(fb, 0) > _PRIORITY.get(s["key_st"].get(ch, ""), 0):
                s["key_st"][ch] = fb
        for ch, fb in s["key_st"].items():
            if ch.upper() in kbtns:
                kbtns[ch.upper()].style.button_color = _KEY_BG[fb]

    def _set_cur(val):
        """Single source of truth for the current guess — keeps text widget in sync."""
        s["cur"] = val
        if text_in.value != val:
            text_in.value = val
        _refresh_human()

    # ── Event handlers ────────────────────────────────────────────────────────
    def _key(c):
        if not s["done"] and len(s["cur"]) < 5:
            _set_cur(s["cur"] + c.lower())

    def _back():
        if not s["done"]:
            _set_cur(s["cur"][:-1])

    def _on_text_change(change):
        """Strip non-alpha chars and enforce 5-char max on physical keyboard input."""
        raw      = change["new"]
        filtered = "".join(c for c in raw if c.isalpha())[:5].lower()
        if filtered != raw:
            text_in.value = filtered   # corrects value; re-fires but no-ops next pass
            return
        s["cur"] = filtered
        _refresh_human()

    def _submit():
        if s["done"]:
            return
        ok, val = _validate(s["cur"], allowed_set)
        if not ok:
            status.value = (
                f'<div style="height:22px;font-family:sans-serif;font-size:13px;'
                f'text-align:center;color:#c0392b">{val}</div>'
            )
            return
        status.value = '<div style="height:22px"></div>'
        fb = wordle_feedback(s["secret"], val)
        s["h_hist"].append((val, fb))
        _apply_key_colors(val, fb)
        _set_cur("")

        won  = val == s["secret"]
        lost = not won and len(s["h_hist"]) >= max_turns
        if won or lost:
            s["done"] = True
            msg = "You solved it! " if won else "Out of turns! "
            status.value = (
                f'<div style="height:22px;font-family:sans-serif;font-size:13px;'
                f'text-align:center;color:#2c3e50">{msg}Watching AI…</div>'
            )
            threading.Thread(target=_run_ai, daemon=True).start()

    def _run_ai():
        """Greedy solver in a daemon thread — pushes widget updates after each turn."""
        candidates   = list(words)
        s["ai_hist"] = []
        lines        = []
        ai_solved    = False
        ai_turns     = max_turns + 1

        for turn in range(1, max_turns + 1):
            guess, guess_score, freq = choose_greedy_guess(candidates)
            fb     = wordle_feedback(s["secret"], guess)
            before = len(candidates)
            candidates = prune_candidates(candidates, guess, fb)
            after  = len(candidates)

            combined = Counter()
            for pf in freq:
                combined.update(pf)
            top     = sorted(combined.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            top_str = "  ".join(f"{c}:{n}" for c, n in top)
            lines.append(
                f"<b>Turn {turn}</b> | {before} candidates\n"
                f"Top: {top_str}\n"
                f"Chose: <b>{guess.upper()}</b>  Score: {guess_score}\n"
                f"Result: {feedback_to_emoji(fb)}  → {after} left\n"
                f"{'─' * 32}\n"
            )
            s["ai_hist"].append((guess, fb))
            a_grid.value = _render_grid(s["ai_hist"], max_turns=max_turns, size=s["tile_sz"])
            a_log.value  = f'<pre style="{_LOG_STYLE};color:#333">{"".join(lines)}</pre>'
            time.sleep(0.6)

            if guess == s["secret"]:
                ai_solved = True
                ai_turns  = turn
                break
            if not candidates:
                break

        h_won   = any(g == s["secret"] for g, _ in s["h_hist"])
        h_turns = len(s["h_hist"]) if h_won else max_turns + 1

        if h_won and (not ai_solved or h_turns < ai_turns):
            s["score"]["human"] += 1
            result = "You win this round!"
        elif ai_solved and (not h_won or ai_turns < h_turns):
            s["score"]["ai"] += 1
            result = "AI wins this round!"
        else:
            s["score"]["tie"] += 1
            result = "It's a tie!"

        score_w.value = _score_html(s["score"])
        status.value  = (
            f'<div style="font-family:sans-serif;font-size:13px;text-align:center;color:#2c3e50">'
            f'Secret: <b>{s["secret"].upper()}</b> — {result}</div>'
        )

    def _toggle_expand():
        s["tile_sz"]           = 68 if s["tile_sz"] == 52 else 52
        expand_btn.description = "⤡ Shrink" if s["tile_sz"] == 68 else "⤢ Expand"
        _refresh_human()
        a_grid.value = _render_grid(s["ai_hist"], max_turns=max_turns, size=s["tile_sz"])

    def _new_game():
        s.update({"secret": random.choice(words), "h_hist": [], "ai_hist": [],
                  "cur": "", "key_st": {}, "done": False})
        text_in.value = ""
        _refresh_human()
        a_grid.value  = _render_grid([], max_turns=max_turns, size=s["tile_sz"])
        a_log.value   = f'<pre style="{_LOG_STYLE};color:#666">AI log appears here…</pre>'
        status.value  = '<div style="height:22px"></div>'
        for b in kbtns.values():
            b.style.button_color = _KEY_BG[""]

    # Wire text input after all handlers are defined
    text_in.observe(_on_text_change, names="value")
    text_in.on_submit(lambda _: _submit())

    # ── Layout ────────────────────────────────────────────────────────────────
    left_col  = widgets.VBox([h_lbl, h_grid],         layout=widgets.Layout(align_items="center",     margin="0 18px"))
    right_col = widgets.VBox([ai_lbl, a_grid, a_log], layout=widgets.Layout(align_items="flex-start", margin="0 18px"))
    keyboard  = widgets.VBox(
        [
            _kb_row("QWERTYUIOP"),
            _kb_row("ASDFGHJKL"),
            _kb_row("ZXCVBNM", back_btn),
            widgets.HBox([text_in, sub_btn],   layout=widgets.Layout(justify_content="center", margin="4px 0")),
            widgets.HBox([ng_btn, expand_btn], layout=widgets.Layout(justify_content="center")),
        ],
        layout=widgets.Layout(align_items="center", margin="8px 0"),
    )
    return widgets.VBox(
        [title, widgets.HBox([left_col, right_col], layout=widgets.Layout(justify_content="center")),
         score_w, keyboard, status],
        layout=widgets.Layout(align_items="center", width="100%"),
    )
