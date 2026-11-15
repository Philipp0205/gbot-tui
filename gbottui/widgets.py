"""The bot's answer widgets: options, steps, and digit keys."""

from . import transcript


def widget_message(entry):
    if not isinstance(entry, dict):
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("type") != "widget":
        return None
    widget = message.get("widget")
    return widget if isinstance(widget, dict) else None


def _option_pairs(raw):
    """(label, value) in the order the bot sent them. The value is what gets sent."""
    if not isinstance(raw, list):
        return []
    options = []
    for opt in raw:
        if not isinstance(opt, dict):
            continue
        label = opt.get("label") if isinstance(opt.get("label"), str) else ""
        value = opt.get("value") if isinstance(opt.get("value"), str) else ""
        label = label.strip() or value.strip()
        value = value.strip() or label
        if label and value:
            options.append((label, value))
    return options


def widget_options(entry):
    """(label, value) on a single-question widget."""
    widget = widget_message(entry)
    if not widget:
        return []
    return _option_pairs(widget.get("options"))


def step_questions(entry):
    """Questions on one card. Each has prompt, options, multi, and custom.

    A widget without `steps` is one question, handled by widget_options.
    """
    widget = widget_message(entry)
    raw = widget.get("steps") if widget else None
    if not isinstance(raw, list):
        return []
    steps = []
    for step in raw:
        if not isinstance(step, dict):
            continue
        prompt = step.get("prompt") if isinstance(step.get("prompt"), str) else ""
        prompt = prompt.strip()
        options = _option_pairs(step.get("options"))
        multi = step.get("multiSelect") is True
        custom = step.get("allowCustom") is True
        if prompt or options or custom:
            steps.append({"prompt": prompt, "options": options, "multi": multi, "custom": custom})
    return steps


def widget_answered(entry):
    value = entry.get("respondedValue") if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value.strip() else ""


def widget_skipped(entry):
    return isinstance(entry, dict) and (
        entry.get("widgetSkipped") is True or entry.get("widgetDismissed") is True
    )


def _question_answerable(step, widget):
    if step["options"] or step["custom"]:
        return True
    return bool(widget and widget.get("allowCustom") is True)


def widget_open(entry):
    if widget_answered(entry) or widget_skipped(entry) or not widget_message(entry):
        return False
    steps = step_questions(entry)
    if steps:
        widget = widget_message(entry)
        return any(_question_answerable(step, widget) for step in steps)
    return bool(widget_options(entry))


def _open_after_user(entries):
    """Open widgets after the last user message, oldest first.

    A widget behind a later user message is stale. Digits belong to the
    composer then, because the user already replied.
    """
    last_user = -1
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and transcript.is_user(entry):
            last_user = index
    return [entry for index, entry in enumerate(entries) if index > last_user and widget_open(entry)]


def latest_widget(entries):
    """The newest choice the user can still answer."""
    found = _open_after_user(entries)
    return found[-1] if found else None


def pages(entries):
    """The questions the open card pages through, in order. [] when nothing is open.

    A `steps` widget gives one page per answerable step. Several single-question
    widgets in a row after the last user message give one page each. One page
    means a plain card with no paging. A page has entry, step (index or None),
    key, prompt, options, multi, and custom. The key is the entry id, plus
    `#<step>` for a step.
    """
    opened = _open_after_user(entries)
    if not opened:
        return []
    newest = opened[-1]
    steps = step_questions(newest)
    if steps:
        widget = widget_message(newest)
        group = [(newest, index) for index, step in enumerate(steps) if _question_answerable(step, widget)]
    else:
        group = [(entry, None) for entry in opened if not step_questions(entry)]
    out = []
    for entry, step in group:
        prompt, options, multi, custom = current_question(entry, step or 0)
        key = str(entry.get("id")) + ("" if step is None else f"#{step}")
        out.append(
            {"entry": entry, "step": step, "key": key, "prompt": prompt, "options": options, "multi": multi, "custom": custom}
        )
    return out


def card_view(entries, state):
    """(pages, current page index, answers so far) for the open card.

    `state` is {"first": key of page 0, "at": index, "answers": {key: value}}.
    A state from another card reads as a fresh one.
    """
    found = pages(entries)
    if not found:
        return [], 0, {}
    state = state if isinstance(state, dict) else {}
    if state.get("first") != found[0]["key"]:
        return found, 0, {}
    at = min(max(int(state.get("at") or 0), 0), len(found) - 1)
    return found, at, state.get("answers") or {}


def page_answer(found, answers):
    """The one message a finished card sends: an answer per question, a blank line between.

    Separate widgets carry their prompt too, so the bot can tell them apart.
    """
    parts = []
    for page in found:
        answer = answers.get(page["key"], "")
        if page["step"] is None and len(found) > 1 and page["prompt"]:
            answer = f"{page['prompt']}: {answer}"
        parts.append(answer)
    return "\n\n".join(parts)


PAGE_HINT = "  ·  ← → or [ ] page"


def page_picks(page, picks, answers):
    """Toggled values on a multi-select page. A saved answer counts until the first toggle."""
    key = page["key"]
    if key in (picks or {}):
        return list(picks[key])
    saved = {part for part in str((answers or {}).get(key, "")).split("\n") if part}
    return [value for _label, value in page["options"] if value in saved]


def page_text(page, at, count, picks=(), answer=""):
    """One question of a paged card under `question 2/3`.

    Options are numbered with a mark column, so an answered one reads `● 2  Paris`
    and the rows do not shift when the mark appears. `picks` are the toggled
    values of a multi-select.
    """
    lines = [f"question {at + 1}/{count}{PAGE_HINT}"]
    if page["prompt"]:
        lines.append(page["prompt"])
    parts = {part for part in str(answer).split("\n") if part}
    known = {value for _label, value in page["options"]}
    chosen = set() if page["multi"] else parts
    option_lines, _matched, extra = _option_lines(page["options"], True, True, chosen, set(picks or ()))
    lines.extend(option_lines)
    if extra:
        lines.append("answer the rest in the app")
    if answer and not page["multi"] and not parts & known:
        lines.append("●  " + str(answer))
    return "\n".join(lines)


# Up to 9 options, one digit selects. Past that, two digits (`10`, or `02`)
# within DIGIT_WINDOW. A letter is never a choice: a message may start with one.
OPTION_DIGITS = 9
OPTION_PAIR = 99
DIGIT_WINDOW = 0.8


def option_key(index, wide=False):
    """1-based position as a key. Empty when this option has no key."""
    if index < 1:
        return ""
    if wide:
        if index > OPTION_PAIR:
            return ""
        return f"{index:02d}"
    if index <= OPTION_DIGITS:
        return str(index)
    return ""


def key_to_index(key, count):
    """0-based option for one digit, "ignore", or None when the key is text.

    Only used when there are at most 9 options. `0` does not fall into the
    composer. A letter is never an option.
    """
    if not isinstance(key, str) or len(key) != 1 or count <= 0 or count > OPTION_DIGITS:
        return None
    if not key.isdigit() or key == "0":
        return "ignore" if key.isdigit() else None
    index = int(key) - 1
    if index < count:
        return index
    return "ignore"


def _key_span(keys):
    """`1`, `1-2`, or `1-9,a-c`."""
    digits = [key for key in keys if key.isdigit()]
    letters = [key for key in keys if key.isalpha()]
    parts = []
    if digits:
        parts.append(digits[0] if len(digits) == 1 else f"{digits[0]}-{digits[-1]}")
    if letters:
        parts.append(letters[0] if len(letters) == 1 else f"{letters[0]}-{letters[-1]}")
    return ",".join(parts)


def current_question(entry, step_at=0):
    """Prompt, options, multi-select, and allowCustom for the question keys answer."""
    widget = widget_message(entry)
    if not widget:
        return "", [], False, False
    steps = step_questions(entry)
    if steps:
        at = min(max(int(step_at or 0), 0), len(steps) - 1)
        step = steps[at]
        custom = step["custom"] or widget.get("allowCustom") is True
        return step["prompt"], step["options"], step["multi"], custom
    prompt = widget.get("prompt") if isinstance(widget.get("prompt"), str) else ""
    return prompt.strip(), widget_options(entry), widget.get("multiSelect") is True, widget.get("allowCustom") is True


def _option_lines(options, open_, multi, chosen, picked):
    lines = []
    matched = False
    uncovered = False
    wide = len(options) > OPTION_DIGITS
    for index, (label, value) in enumerate(options, 1):
        marked = value in chosen or value in picked
        matched = matched or value in chosen
        key = option_key(index, wide) if open_ else ""
        if open_ and not key:
            uncovered = True
        if open_ and key and multi:
            mark = "●" if marked else " "
            lines.append(f"{mark} {key}  {label}")
        elif open_ and key:
            lines.append(f"{key}  {label}")
        elif marked:
            lines.append(f"●  {label}")
        else:
            lines.append(f"   {label}")
    return lines, matched, uncovered


def _steps_text(entry, steps, picks, numbered):
    """A `steps` card as one block. An open multi-question card is paged instead (page_text)."""
    widget = widget_message(entry)
    answered = widget_answered(entry)
    # A finished card stores one answer per step, with a blank line between them.
    answers = answered.split("\n\n") if answered else []
    open_ = widget_open(entry) and numbered
    # The question the number keys answer: the first one that can be answered.
    current_at = next((i for i, step in enumerate(steps) if _question_answerable(step, widget)), 0)
    lines = []
    head = widget.get("prompt") if isinstance(widget.get("prompt"), str) else ""
    head = head.strip()
    if head and head not in {step["prompt"] for step in steps}:
        lines.append(head)
    if widget_skipped(entry) and not answered:
        if lines:
            lines[-1] += "  · skipped"
        elif steps and steps[0]["prompt"]:
            steps = [dict(steps[0], prompt=steps[0]["prompt"] + "  · skipped"), *steps[1:]]
        else:
            lines.append("skipped")
    for index, step in enumerate(steps):
        if step["prompt"] and not (index == 0 and lines and lines[-1].startswith(step["prompt"])):
            lines.append(step["prompt"])
        if index < len(answers) and str(answers[index]).strip():
            chosen = {part for part in str(answers[index]).split("\n") if part}
            option_lines, matched, _extra = _option_lines(step["options"], False, False, chosen, ())
            lines.extend(option_lines)
            if not matched:
                lines.append("●  " + str(answers[index]).strip())
            continue
        current = open_ and index == current_at
        option_lines, _matched, extra = _option_lines(
            step["options"], current, step["multi"], set(), set(picks or ()) if current else ()
        )
        lines.extend(option_lines)
        if current and extra:
            lines.append("answer the rest in the app")
        elif current and not step["options"] and not _question_answerable(step, widget):
            lines.append("answer in the app")
    if not answered and not widget_skipped(entry) and not any(_question_answerable(step, widget) for step in steps):
        lines.append("answer in the app")
    return "\n".join(line for line in lines if line is not None)


def widget_text(entry, picks=(), numbered=True):
    """The prompt and its options. The open choice is numbered. A picked one is marked."""
    widget = widget_message(entry)
    if widget is None:
        return ""
    steps = step_questions(entry)
    if steps:
        return _steps_text(entry, steps, picks, numbered)
    prompt = widget.get("prompt")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    if widget_skipped(entry) and not widget_answered(entry) and prompt:
        prompt += "  · skipped"
    options = widget_options(entry)
    answered = widget_answered(entry)
    chosen = {part for part in answered.split("\n") if part} if answered else set()
    picked = set(picks or ())
    multi = widget.get("multiSelect") is True
    # Only the newest open widget takes number keys, so older ones are not numbered.
    open_ = widget_open(entry) and numbered
    lines = [prompt] if prompt else []
    option_lines, matched, extra = _option_lines(options, open_, multi, chosen, picked)
    lines.extend(option_lines)
    if open_ and extra:
        lines.append("answer the rest in the app")
    if answered and not matched:
        lines.append(f"●  {answered}")
    return "\n".join(lines)


def _hint(options, multi, custom):
    if not options:
        return "type an answer" if custom else ""
    wide = len(options) > OPTION_DIGITS
    keys = [option_key(index, wide) for index in range(1, len(options) + 1)]
    keys = [key for key in keys if key]
    if not keys:
        return "answer in the app"
    span = _key_span(keys)
    if wide:
        span = f"{span} two digits"
    if multi:
        return f"{span} choose · enter sends"
    return f"{span} answer"


def choice_hint(entry, step_at=0):
    """Footer keys for one widget entry. A paged card uses page_hint."""
    if not entry or not widget_open(entry):
        return ""
    _prompt, options, multi, custom = current_question(entry, step_at)
    return _hint(options, multi, custom)


def page_hint(page):
    """Footer keys for the open page of a card."""
    return _hint(page["options"], page["multi"], page["custom"]) if page else ""
