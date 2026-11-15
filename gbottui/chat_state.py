"""One chat's state: the transcript, the draft, the phase, and pending decisions."""

import math

from . import activity, backend, colors, draft, herdr, icons, mentions, roster, tabs, transcript, widgets


# Poll cadence in seconds. Each read is one gbot process, so idle backs off.
WAITING_DELAY = 0.3
STREAM_DELAY = 1.0
SETTLE_DELAY = 2.0
BLOCKED_DELAY = 1.0
IDLE_DELAYS = (2.0, 2.0, 3.0, 5.0, 8.0)
# A thread read returns the newest READ_LIMIT entries. Scrolling to the top
# asks for HISTORY_STEP more with a larger `--limit`, up to HISTORY_MAX.
READ_LIMIT = 80
HISTORY_STEP = 80
HISTORY_MAX = 800
# Seconds before the next read after 1, 2, 3, … failed reads in a row.
RETRY_DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0)
# A notice (copied, sent elsewhere) stays this long, or until the next key.
NOTICE_SECONDS = 4.0


class Chat:
    def __init__(self, target):
        self.target = target
        self.title = target
        self.session_id = ""
        self.agent_id = ""
        self.viewer_session = ""
        self.messages = []
        self.cursor = ""
        self.phase = "idle"
        self.quiet = 0
        self.saw_other = False
        self.scroll = 0
        self.follow = True
        self.help = False
        # Transcript search: None is closed, a string is the query being typed.
        # `search_at` indexes `grid.search_hits`; `search_jump` asks the next
        # draw to scroll that match into view.
        self.search = None
        self.search_at = None
        self.search_jump = False
        self.typed = ""
        # Cursor index into `typed`. None is the end, so a draft set from
        # outside (a test, a widget reset) keeps typing at the end.
        self.pos = None
        # Input history: index into `sent_history()` while Up/Down browse it,
        # and the draft that was there before the first Up.
        self.hist_at = None
        self.hist_draft = ("", None)
        # The @-mention list: open flag, selected row, and the query it was
        # last filtered by (a new query puts the selection back on the top row).
        self.mention_open = False
        self.mention_sel = 0
        self.mention_query = ""
        self.error = ""
        # A neutral or success line for the footer, not drawn as an error.
        self.notice = ""
        self.notice_at = None
        # Failed thread reads in a row, when the next one starts, and its error.
        self.read_failures = 0
        self.retry_at = None
        self.read_error = ""
        # Entries a full read asks for, whether the last one filled it (so more
        # may sit before it), and whether an older read is out.
        self.limit = READ_LIMIT
        self.more_history = False
        self.loading_older = False
        self.resume = herdr.resume_arg(target)
        bot = roster.ROSTER.get(target) or {}
        # An unknown bot stays unset until its thread names it.
        self.avatar_color = colors.bot_color(bot) if bot else ""
        self.avatar_shape = bot.get("avatarShape") or bot.get("avatar_shape") or ""
        self.label = (bot.get("title") or bot.get("tagline") or "").strip()
        self.deciding = False
        self.answered = {}
        self.pending_request = ""
        self.widget_picks = {}
        self.widget_sent = {}
        # The open card's paging: {"first": key of page 1, "at": page, "answers": {key: value}}.
        self.widget_card = {}
        self.widget_busy = False
        self.widget_entry = ""
        self.widget_group = []
        self.widget_phase = "idle"
        self.choice_wait = False
        self.content_token = ""
        self.content_at = None
        self.digit_buf = ""
        self.digit_at = None
        self.full_reload = False
        self.replacing = False
        self.rows_rev = 0
        self.loaded = False
        self.idle_polls = 0
        self.app_seen = herdr.APP

    def show_cached(self, entries):
        """Paint the last thread seen while the first read is still out.

        Phase stays idle, so y/n cannot answer a card from the cache.
        """
        if entries and not self.messages:
            self.messages = list(entries)
            self.touch_rows()

    def poll_delay(self):
        """Seconds until the next thread read.

        Fast while a reply is coming, slower once it settles, so two quiet
        polls still span about four seconds. An idle chat backs off to
        IDLE_DELAYS[-1]; a key press brings it back to the first step.
        """
        if self.phase == "blocked":
            return BLOCKED_DELAY
        if self.phase == "working":
            if not self.saw_other:
                return WAITING_DELAY
            return STREAM_DELAY if self.quiet == 0 else SETTLE_DELAY
        return IDLE_DELAYS[min(self.idle_polls, len(IDLE_DELAYS) - 1)]

    def show_notice(self, text):
        self.notice = text
        self.notice_at = activity.clock()

    def expire_notice(self, any_key=False):
        """Drop the notice after NOTICE_SECONDS, or at once on a key. True if it went."""
        if not self.notice:
            return False
        if any_key or self.notice_at is None or activity.clock() - self.notice_at >= NOTICE_SECONDS:
            self.notice = ""
            self.notice_at = None
            return True
        return False

    def nudge(self):
        self.idle_polls = 0

    def note(self, phase):
        if phase != self.phase:
            self.phase = phase
            herdr.publish(phase, self.title, self.session_id, self.resume)

    def apply(self, page, replace):
        if page["name"]:
            self.title = page["name"]
            self.resume = herdr.resume_arg(page["name"]) or herdr.resume_arg(page["id"]) or self.resume
            herdr.bind_app(self.title)
        if page.get("agent_id"):
            self.agent_id = page["agent_id"]
        elif page.get("id") and not page.get("viewer_session"):
            # target.id is the agent id when the thread names no other session.
            self.agent_id = page["id"]
        elif "agent_id" in page and page.get("viewer_session"):
            self.agent_id = page.get("agent_id") or ""
        if page.get("viewer_session"):
            self.viewer_session = page["viewer_session"]
        if page["id"]:
            self.session_id = page["id"]
        if page.get("avatar_shape"):
            self.avatar_shape = page["avatar_shape"]
        if page.get("avatar_color"):
            self.avatar_color = page["avatar_color"]
        if replace or page["gap_reset"]:
            pending = [entry for entry in self.messages if entry.get("local")]
            self.messages = list(page["entries"])
            for entry in pending:
                if not self._covers(entry):
                    self.messages.append(entry)
            self.cursor = page["cursor"]
            fresh = []
        else:
            # Only new or changed entries count. A gateway that returns the
            # cursor entry again would otherwise keep the turn `working`.
            fresh = [entry for entry in page["entries"] if self._absorb(entry)]
            if page["cursor"]:
                self.cursor = page["cursor"]
        self._stamp_answered()
        self._stamp_choices()
        self.note_content()
        self.touch_rows()
        return fresh

    def _covers(self, local):
        text = transcript.entry_text(local).strip()
        return any(
            transcript.is_user(entry) and not entry.get("local") and transcript.entry_text(entry).strip() == text
            for entry in self.messages
        )

    def _take_local(self, entry):
        if not transcript.is_user(entry):
            return False
        text = transcript.entry_text(entry).strip()
        if not text:
            return False
        for index, local in enumerate(self.messages):
            if local.get("local") and transcript.entry_text(local).strip() == text:
                self.messages[index] = entry
                return True
        return False

    def _absorb(self, entry):
        """Merge one polled entry. False when it was already on screen unchanged."""
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(entry_id, str) and entry_id:
            for index, existing in enumerate(self.messages):
                if existing.get("id") == entry_id:
                    if existing == entry:
                        return False
                    self.messages[index] = entry
                    return True
        if self._take_local(entry):
            return True
        index = next((i for i, local in enumerate(self.messages) if local.get("local")), None)
        if index is None:
            self.messages.append(entry)
        else:
            self.messages.insert(index, entry)
        return True

    def _stamp_answered(self):
        """A poll that still says pending must not undo a decision just sent."""
        for entry in self.messages:
            decision = self.answered.get(transcript.request_of(entry))
            if not decision:
                continue
            card = transcript.message_card(entry)
            if card is not None and card.get("status") == "pending":
                transcript.set_card_status(entry, transcript.DECISION_STATUS[decision])

    def _stamp_choices(self):
        """A poll that has not caught up must not wipe a choice just sent."""
        for entry in self.messages:
            value = self.widget_sent.get(entry.get("id"))
            if value and not widgets.widget_answered(entry):
                entry["respondedValue"] = value

    def touch_rows(self):
        self.rows_rev += 1
        self._rows_key = None

    def list_id(self):
        """The id the bot list knows this bot by.

        The thread's own id can be a session. The list reads seen, activity and
        server state by the roster id first, so a row saved under another id
        would never replace what the list already has.
        """
        bot = roster.ROSTER.get(self.target) or {}
        ident = bot.get("id")
        return ident if isinstance(ident, str) and ident else self.session_id

    def sync_activity(self):
        """The open chat is what the list shows on the way back.

        A reply on screen is not new. `working` stays running while it arrives.
        """
        entries = [entry for entry in self.messages if isinstance(entry, dict)]
        cursor = self.cursor or transcript.last_id([entry for entry in entries if not entry.get("local")])
        bot_id = self.list_id()
        activity.remember_seen(self.target, bot_id, cursor)
        backend.mark_read_later(bot_id)
        shown = [entry for entry in entries if not entry.get("local")]
        # An open widget is needs-you on the list even while this chat is idle.
        if self.phase == "blocked" or (self.phase != "working" and widgets.latest_widget(shown)):
            state = "blocked"
        elif self.phase == "working":
            state = "working"
        else:
            state = "idle"
        activity.store_activity(
            {"id": bot_id, "name": self.target},
            {
                "state": state,
                "unread": 0,
                "cursor": cursor,
                "preview": activity.activity_preview(shown, state),
                "latest": max(
                    (e["timestampMs"] for e in shown if isinstance(e.get("timestampMs"), (int, float))),
                    default=0,
                ),
                "token": activity.content_token(shown),
                "since": 0 if self.content_at is None else self.content_at,
            },
        )

    def begin_decision(self, decision):
        """Mark the newest card decided and return the gbot arguments.

        None means this request was already answered, or there is nothing to answer.
        """
        card = transcript.latest_decision(self.messages, self.answered)
        if not card or decision not in transcript.DECISION_STATUS:
            return None
        request_id = card["request_id"]
        self.answered[request_id] = decision
        status = transcript.DECISION_STATUS[decision]
        for entry in self.messages:
            if entry.get("id") == card["entry_id"] or transcript.request_of(entry) == request_id:
                transcript.set_card_status(entry, status)
        self.pending_request = request_id
        self.deciding = True
        self.touch_rows()
        if self.phase == "blocked" and not transcript.pending_approval(self.messages):
            self.note("idle")
        self.sync_activity()
        return (self.target, card["entry_id"], request_id, decision)

    def finish_decision(self):
        """The respond call returned. The next read is the whole thread."""
        self.deciding = False
        self.pending_request = ""
        self.full_reload = True

    def fail_decision(self):
        """The respond call failed. Put the card back so it can be retried."""
        request_id = self.pending_request
        self.deciding = False
        self.pending_request = ""
        if request_id:
            self.answered.pop(request_id, None)
            for entry in self.messages:
                if transcript.request_of(entry) == request_id:
                    transcript.set_card_status(entry, "pending")
            self.touch_rows()
            if transcript.pending_approval(self.messages) and self.phase != "blocked":
                self.note("blocked")
        self.sync_activity()

    def choice_agent_id(self):
        """Agent id for the optional hook. A viewer session is never that id."""
        if self.agent_id:
            return self.agent_id
        if self.viewer_session:
            return ""
        return self.session_id

    def card(self):
        """(pages, current index, answers) of the open card. See widgets.card_view."""
        return widgets.card_view(self.messages, self.widget_card)

    def page(self):
        """The question the number keys answer, or None when nothing is open."""
        found, at, _answers = self.card()
        return found[at] if found else None

    def _card_state(self, found):
        """The paging state for this card. A different card starts over."""
        if self.widget_card.get("first") != found[0]["key"]:
            self.widget_card = {"first": found[0]["key"], "at": 0, "answers": {}}
        return self.widget_card

    def page_picks(self, page):
        """The toggled values on a multi-select page. A saved answer counts until toggled."""
        _found, _at, answers = self.card()
        return widgets.page_picks(page, self.widget_picks, answers)

    def turn_page(self, delta):
        """Move a paged card back or forward. False when the card is one question."""
        found, at, _answers = self.card()
        if len(found) < 2:
            return False
        state = self._card_state(found)
        state["at"] = min(max(at + delta, 0), len(found) - 1)
        self.error = ""
        self.touch_rows()
        return True

    def note_content(self):
        """Restart the quiet-turn clock when anything in the thread changes.

        The token covers hidden thinking, progress, and tool entries too.
        """
        token = activity.content_token(self.messages)
        now = activity.clock()
        if token != self.content_token or self.content_at is None:
            self.content_token = token
            self.content_at = now

    def quiet_for(self):
        """True when the thread has not changed for the limit.

        That is WORKING_TIMEOUT, or the long safety cap while the gateway
        marks the turn live.
        """
        now = activity.clock()
        if self.content_at is None:
            self.content_at = now
            return False
        return now - self.content_at >= activity.quiet_limit(self.messages)

    def content_expired(self):
        return self.phase == "working" and self.quiet_for()

    def settle_working(self, phase):
        """Keep a live reply in `working`, and give up when nothing changes."""
        if phase == "done" and activity.entry_in_progress(self.messages):
            phase = "working"
        # A turn whose last line is the user's gives up after WORKING_TIMEOUT.
        # One the gateway marks live gives up after the safety cap.
        if phase == "working" and self.quiet_for():
            return "idle"
        if phase in {"idle", "done"} and activity.entry_in_progress(self.messages) and not self.quiet_for():
            return "working"
        return phase

    def expire_working(self):
        """Drop a turn that has had no change for its limit."""
        if not self.content_expired():
            return False
        self.choice_wait = False
        self.note("idle")
        self.sync_activity()
        return True

    def begin_choice(self, text, marks=None):
        """Mark the open card answered and return the gateway arguments.

        `marks` is [(entry, answer)] for every widget the message answers; it
        defaults to the newest open widget with `text`. The mark shows
        immediately. `text` is what `gbot send` will carry. `agent_id` is empty
        when the thread only had a session id.
        """
        if self.widget_busy or not self.loaded or not (self.agent_id or self.session_id):
            return None
        if marks is None:
            entry = widgets.latest_widget(self.messages)
            marks = [(entry, text)] if entry is not None else []
        ids = [entry.get("id") for entry, _answer in marks]
        if not marks or not text or not all(isinstance(ident, str) and ident for ident in ids):
            return None
        entry_id = ids[-1]
        for page in self.card()[0]:
            self.widget_picks.pop(page["key"], None)
        for entry, answer in marks:
            self.widget_sent[entry["id"]] = answer
            entry["respondedValue"] = answer
        self.messages.append({"role": "user", "text": text, "local": True, "widget_answer": entry_id})
        self.widget_entry = entry_id
        self.widget_group = ids
        self.widget_busy = True
        self.widget_phase = self.phase
        self.choice_wait = True
        self.saw_other = False
        self.quiet = 0
        self.touch_rows()
        self.note_content()
        self.note("working")
        self.sync_activity()
        return (self.target, self.choice_agent_id(), entry_id, text)

    def commit_choice(self, value):
        """Record the answer to the open question. A finished card returns the gateway arguments.

        A card of several questions (one widget with `steps`, or widgets in a
        row) keeps every answer and moves to the next unanswered question. The
        last answer sends one message, an answer per question with a blank line
        between. "step" means the card is still open.
        """
        if not value or self.widget_busy:
            return None
        found, at, _answers = self.card()
        if not found:
            return None
        if len(found) == 1:
            return self.begin_choice(value)
        state = self._card_state(found)
        answers = state["answers"]
        answers[found[at]["key"]] = value
        self.widget_picks.pop(found[at]["key"], None)
        if all(page["key"] in answers for page in found):
            return self._send_card(found, answers)
        # On to the next question without an answer, wrapping to an earlier one.
        order = list(range(at + 1, len(found))) + list(range(at))
        state["at"] = next(i for i in order if found[i]["key"] not in answers)
        self.touch_rows()
        return "step"

    def _send_card(self, found, answers):
        entries = []
        for page in found:
            if page["entry"] not in entries:
                entries.append(page["entry"])
        text = widgets.page_answer(found, answers)
        if found[0]["step"] is not None:
            marks = [(entries[0], text)]
        else:
            marks = [(page["entry"], answers[page["key"]]) for page in found]
        return self.begin_choice(text, marks)

    def saved_answer(self, page):
        """What this question was answered with so far, or ""."""
        return str(self.card()[2].get(page["key"], ""))

    def toggle_choice(self, index):
        page = self.page()
        if page is None or not page["multi"]:
            return False
        if index < 0 or index >= len(page["options"]):
            return False
        value = page["options"][index][1]
        picks = self.page_picks(page)
        if value in picks:
            picks.remove(value)
        else:
            picks.append(value)
        # An empty list stays: all unticked is not the saved answer.
        self.widget_picks[page["key"]] = picks
        self.touch_rows()
        return True

    def finish_choice(self):
        """The widget call returned. The next read is the whole thread."""
        self.widget_busy = False
        self.widget_entry = ""
        self.widget_group = []
        self.full_reload = True

    def fail_choice(self):
        """The widget call failed. Put the options back so they can be picked again.

        The card keeps its answers, on its last question, so one more answer sends again.
        """
        entry_id = self.widget_entry
        group = self.widget_group or [entry_id]
        values = {ident: self.widget_sent.pop(ident, None) for ident in group}
        self.widget_busy = False
        self.widget_entry = ""
        self.widget_group = []
        self.choice_wait = False
        self.messages = [
            entry
            for entry in self.messages
            if not (entry.get("local") and entry.get("widget_answer") == entry_id)
        ]
        for entry in self.messages:
            if entry.get("id") in values and entry.get("respondedValue") == values[entry.get("id")]:
                entry.pop("respondedValue", None)
        found = self.card()[0]
        if len(found) > 1:
            self._card_state(found)["at"] = len(found) - 1
        self.touch_rows()
        self.note_content()
        phase = self.widget_phase if self.widget_phase in {"working", "blocked"} else "idle"
        self.note(phase)
        self.sync_activity()

    def take_read(self):
        """Arguments for the next `gbot thread` call.

        After a decision the following read drops `--after` once, so the
        gateway can return the card with its new status. Later polls resume
        from the cursor.
        """
        if self.full_reload:
            self.full_reload = False
            self.replacing = True
            return self.full_read()
        if self.cursor:
            return (self.target, self.cursor)
        return self.full_read()

    def full_read(self):
        # A full read keeps the older entries already loaded.
        if self.limit > READ_LIMIT:
            return (self.target, None, self.limit)
        return (self.target,)

    def take_older(self):
        """The `read_thread` args for older entries, or None when there is nothing to ask.

        Only an idle chat whose last full read was full asks, so a reply that
        lands in the same read is never missed by `watch_step`.
        """
        if (
            not self.loaded
            or self.loading_older
            or not self.more_history
            or self.limit >= HISTORY_MAX
            or self.phase not in {"idle", "done"}
            or self.read_failures
        ):
            return None
        self.loading_older = True
        return (self.target, None, min(self.limit + HISTORY_STEP, HISTORY_MAX))

    def ingest_older(self, page, limit):
        """A larger full read. Returns how many entries it added at the top."""
        self.loading_older = False
        self.read_back()
        before = len([entry for entry in self.messages if not entry.get("local")])
        self.apply(page, replace=True)
        added = max(len(page["entries"]) - before, 0)
        self.limit = limit
        self.more_history = added > 0 and len(page["entries"]) >= limit
        return added

    def older_failed(self):
        self.loading_older = False

    def read_failed(self, message=""):
        """Note a failed read. Returns the seconds until the retry, backing off."""
        if self.replacing:
            self.replacing = False
            self.full_reload = True
        self.read_failures += 1
        self.read_error = message
        delay = RETRY_DELAYS[min(self.read_failures - 1, len(RETRY_DELAYS) - 1)]
        self.retry_at = activity.clock() + delay
        return delay

    def offline_label(self):
        """`offline · retrying in Ns` after a failed read, else ""."""
        if not self.read_failures:
            return ""
        left = math.ceil(max((self.retry_at or 0) - activity.clock(), 0))
        return f"offline · retrying in {left}s" if left else "offline · retrying…"

    def read_back(self):
        """A good read after failures: drop the read error and say so briefly."""
        if not self.read_failures:
            return False
        self.read_failures = 0
        self.retry_at = None
        if self.error == self.read_error:
            self.error = ""
        self.read_error = ""
        self.show_notice("reconnected")
        return True

    def footer_error(self):
        """The red footer text: the offline countdown, then the last error."""
        label = self.offline_label()
        if label and self.error:
            return f"{label} · {self.error}"
        return label or self.error

    def ingest(self, page, first=False):
        self.read_back()
        replace = first or not self.cursor or page["gap_reset"] or self.replacing
        self.replacing = False
        if replace:
            self.apply(page, replace=True)
            self.more_history = len(page["entries"]) >= self.limit
            fresh = []
            # An older answer is not this turn's reply. Counting it would
            # clear `working` after two quiet polls while the bot is still going.
            if (
                self.phase == "working"
                and not self.choice_wait
                and not activity.awaiting_reply(self.messages)
                and not activity.entry_in_progress(self.messages)
                and any(not transcript.is_user(entry) and transcript.entry_text(entry).strip() for entry in self.messages)
            ):
                self.saw_other = True
            self.choice_wait = False
        else:
            fresh = self.apply(page, replace=False)
        if fresh or replace:
            self.idle_polls = 0
        elif self.phase in {"idle", "done"}:
            self.idle_polls += 1
        if first:
            self.loaded = True
            icons.show_avatar_later(page)
            if transcript.pending_approval(self.messages):
                phase = "blocked"
            elif self.phase == "working" or activity.awaiting_reply(self.messages) or activity.entry_in_progress(self.messages):
                phase = "working"
            else:
                phase = "idle"
            phase = self.settle_working(phase)
            self.phase = phase
            herdr.publish(phase, self.title, self.session_id, self.resume)
            tabs.TAB.show(self.title)
            self.sync_activity()
            return
        phase, self.quiet, self.saw_other = transcript.watch_step(
            self.phase,
            fresh,
            self.quiet,
            self.saw_other,
            transcript.pending_approval(self.messages),
        )
        phase = self.settle_working(phase)
        self.note(phase)
        self.sync_activity()

    def edit(self, change, *args):
        """Apply a `draft` function to the draft and the cursor."""
        self.typed, self.pos = change(self.typed, self.pos, *args)

    def type_text(self, value):
        self.edit(draft.insert, value)

    def mention_matches(self):
        """The suggestions for the `@query` at the cursor: [(name, matched indexes)].

        The bot roster minus this chat's own bot. Empty when there is no query
        at the cursor.
        """
        names = [name for name in roster.ROSTER if name not in (self.target, self.title)]
        found = mentions.suggest(self.typed, self.pos, names)
        return found[2] if found else []

    def close_mention(self):
        self.mention_open = False
        self.mention_sel = 0
        self.mention_query = ""

    def sent_history(self):
        """Your earlier messages in this chat, oldest first, repeats folded.

        Read from the transcript, so it covers what the cache and the server
        have, and editing a recalled line cannot change it.
        """
        out = []
        for entry in self.messages:
            if not isinstance(entry, dict) or not transcript.is_user(entry):
                continue
            text = transcript.entry_text(entry).strip()
            if text and (not out or out[-1] != text):
                out.append(text)
        return out

    def recall(self, delta):
        """Up (-1) or Down (1) through the history. False when there is nothing to move to."""
        past = self.sent_history()
        if self.hist_at is None:
            if delta > 0 or not past:
                return False
            self.hist_draft = (self.typed, self.pos)
            at = len(past) - 1
        else:
            at = self.hist_at + delta
            if at < 0:
                return False
            if at >= len(past):
                self.hist_at = None
                self.typed, self.pos = self.hist_draft
                return True
        self.hist_at = at
        self.typed, self.pos = past[at], None
        return True

    def arm_send(self):
        text = self.typed.strip()
        if not text:
            return ""
        self.typed = ""
        self.pos = None
        self.hist_at = None
        self.error = ""
        self.saw_other = False
        self.quiet = 0
        self.follow = True
        self.messages.append({"role": "user", "text": text, "local": True})
        self.note_content()
        self.note("working")
        self.sync_activity()
        return text
